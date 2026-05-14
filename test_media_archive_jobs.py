import datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import services.media_archive_jobs as archive_service
import use_cases.media_archive_jobs as archive_use_cases
from api.v1.schemas.media import MediaArchiveJobCreateRequest
from db.daos.media import MediaDAO
from db.daos.media_archive_job import MediaArchiveJobDAO
from dto.media_archive_job import MediaArchiveJobDTO
from exceptions.media import (
	MediaArchiveJobFailedError,
	MediaArchiveJobNotReadyError,
	MediaArchiveResultExpiredError,
)
from use_cases.media_archive_jobs import (
	CreateMediaArchiveJobUseCase,
	GetMediaArchiveJobDownloadLinkUseCase,
	GetMediaArchiveJobStatusUseCase,
)


pytestmark = pytest.mark.no_db


class FakeCommitSession:
	def __init__(self):
		self.commit_calls = 0
		self.rollback_calls = 0

	async def commit(self):
		self.commit_calls += 1

	async def rollback(self):
		self.rollback_calls += 1


class FakeJobDAO(MediaArchiveJobDAO):
	def __init__(self, job: MediaArchiveJobDTO | None = None):
		self._session: Any = FakeCommitSession()
		self.job = job
		self.events = []

	async def create_job(self, *, org_id, client_id, appointment_id, requested_by, requested_kinds=None, status="queued"):
		job = MediaArchiveJobDTO(
			id=uuid4(),
			org_id=org_id,
			client_id=client_id,
			appointment_id=appointment_id,
			requested_by=requested_by,
			status=status,
			requested_kinds=requested_kinds,
			result_storage_key=None,
			result_expires_at=None,
			error_message=None,
			created_at=datetime.datetime.now(datetime.timezone.utc),
			updated_at=datetime.datetime.now(datetime.timezone.utc),
			started_at=None,
			completed_at=None,
			failed_at=None,
		)
		self.job = job
		return job

	async def get_by_id(self, job_id):
		return self.job

	async def get_by_id_for_org(self, job_id, org_id):
		if self.job is None or self.job.org_id != org_id:
			return None
		return self.job

	async def update_status(self, job_id, **kwargs):
		assert self.job is not None
		data = self.job.model_dump()
		data.update(kwargs)
		data["updated_at"] = datetime.datetime.now(datetime.timezone.utc)
		self.job = MediaArchiveJobDTO(**data)
		return self.job

	async def log_event(self, **kwargs):
		self.events.append(kwargs)
		return SimpleNamespace(**kwargs)


class FakeMediaDAO(MediaDAO):
	def __init__(self, media_items):
		self.media_items = media_items

	async def client_belongs_to_org(self, client_id, org_id):
		return True

	async def appointment_belongs_to_org(self, appointment_id, org_id):
		return True

	async def get_archive_media_for_client(self, client_id):
		return list(self.media_items)

	async def get_by_appointment_id(self, appointment_id):
		return list(self.media_items)


def _archive_job(org_id=None, client_id=None, appointment_id=None, status="done", expires_delta_seconds=120, result_key: str | None = "org/x/archives/job.zip", error_message=None):
	now = datetime.datetime.now(datetime.timezone.utc)
	return MediaArchiveJobDTO(
		id=uuid4(),
		org_id=org_id or uuid4(),
		client_id=client_id,
		appointment_id=appointment_id,
		requested_by=uuid4(),
		status=status,
		requested_kinds="before_photo,attachment",
		result_storage_key=result_key,
		result_expires_at=(now + datetime.timedelta(seconds=expires_delta_seconds)) if status == "done" else None,
		error_message=error_message,
		created_at=now,
		updated_at=now,
		started_at=now if status in {"running", "done", "failed"} else None,
		completed_at=now if status == "done" else None,
		failed_at=now if status == "failed" else None,
	)


@pytest.mark.asyncio
async def test_create_archive_job_sets_queued_and_logs_audit(monkeypatch):
	org_id = uuid4()
	client_id = uuid4()
	payload = MediaArchiveJobCreateRequest(client_id=client_id, appointment_id=None, kinds=["before_photo"])
	job_dao = FakeJobDAO()
	media_dao = FakeMediaDAO([])
	
	# Mock ARQ pool
	enqueued_jobs = []
	class FakeArqPool:
		async def enqueue_job(self, *args, **kwargs):
			enqueued_jobs.append((args, kwargs))
			return SimpleNamespace(job_id="fake-job-id")
	
	arq = FakeArqPool()
	use_case = CreateMediaArchiveJobUseCase(media_dao, job_dao, arq)

	job = await use_case.execute(payload=payload, org_id=org_id, requested_by=uuid4(), ip_address="127.0.0.1", user_agent="pytest")

	assert job.status == "queued"
	assert job.requested_kinds == "before_photo"
	assert len(enqueued_jobs) == 1
	assert enqueued_jobs[0][0][0] == "process_media_archive_job"
	assert [event["event_type"] for event in job_dao.events] == ["queued"]
	assert job_dao._session.commit_calls == 1


@pytest.mark.asyncio
async def test_job_status_and_download_link_happy_path(monkeypatch):
	org_id = uuid4()
	job = _archive_job(org_id=org_id, status="done", expires_delta_seconds=90)
	job_dao = FakeJobDAO(job)

	monkeypatch.setattr(
		archive_use_cases,
		"generate_presigned_get_url",
		lambda *, key, expires_in: {"url": f"https://example.invalid/{key}", "expires_in": expires_in},
	)
	monkeypatch.setattr(archive_use_cases.settings, "MEDIA_ARCHIVE_LINK_EXPIRES_SECONDS", 60, raising=False)

	status = await GetMediaArchiveJobStatusUseCase(job_dao).execute(job_id=job.id, org_id=org_id)
	assert status.status == "done"

	link = await GetMediaArchiveJobDownloadLinkUseCase(job_dao).execute(job_id=job.id, org_id=org_id, user_id=uuid4())
	assert link["status"] == "done"
	assert link["download_url"].startswith("https://example.invalid/")
	assert 0 < link["expires_in"] <= 60
	assert [event["event_type"] for event in job_dao.events] == ["link_issued"]


@pytest.mark.asyncio
async def test_job_download_link_rejects_not_ready_and_expired():
	org_id = uuid4()
	queued_job = _archive_job(org_id=org_id, status="queued", expires_delta_seconds=90)
	job_dao = FakeJobDAO(queued_job)

	with pytest.raises(MediaArchiveJobNotReadyError):
		await GetMediaArchiveJobDownloadLinkUseCase(job_dao).execute(job_id=queued_job.id, org_id=org_id)

	failed_job = _archive_job(org_id=org_id, status="failed", error_message="boom")
	job_dao.job = failed_job
	with pytest.raises(MediaArchiveJobFailedError):
		await GetMediaArchiveJobDownloadLinkUseCase(job_dao).execute(job_id=failed_job.id, org_id=org_id)

	expired_job = _archive_job(org_id=org_id, status="done", expires_delta_seconds=-86400)
	job_dao.job = expired_job
	with pytest.raises(MediaArchiveResultExpiredError):
		await GetMediaArchiveJobDownloadLinkUseCase(job_dao).execute(job_id=expired_job.id, org_id=org_id)


@pytest.mark.asyncio
async def test_worker_processes_done_and_failed_paths(monkeypatch):
	org_id = uuid4()
	appointment_id = uuid4()
	now = datetime.datetime(2026, 4, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)
	media_items = [
		SimpleNamespace(
			id=uuid4(),
			kind="before_photo",
			org_id=org_id,
			client_id=None,
			appointment_id=appointment_id,
			creator_id=uuid4(),
			storage_key="org/x/appointments/y/before/a.jpg",
			file_name="a.jpg",
			size_bytes=10,
			mime_type="image/jpeg",
			expires_at=now + datetime.timedelta(days=1),
			deleted_at=None,
		)
	]
	job_dao = FakeJobDAO(_archive_job(org_id=org_id, appointment_id=appointment_id, client_id=None, status="queued", result_key=None))
	media_dao = FakeMediaDAO(media_items)
	session = FakeCommitSession()
	storage = {}

	async def fake_get_session():
		yield session

	monkeypatch.setattr(archive_service, "get_session", fake_get_session)
	monkeypatch.setattr(archive_service, "get_object_bytes", lambda key: b"photo-bytes")
	monkeypatch.setattr(archive_service, "put_object_bytes", lambda key, data, content_type="application/octet-stream": storage.update({key: data}))

	await archive_service.process_media_archive_job(job_dao.job.id, job_dao=job_dao, media_dao=media_dao, now=now)

	assert job_dao.job.status == "done"
	assert job_dao.job.result_storage_key in storage
	assert [event["event_type"] for event in job_dao.events] == ["running", "done"]
	assert session.commit_calls == 1

	# Failure path: no media available.
	job_dao.job = _archive_job(org_id=org_id, appointment_id=appointment_id, status="queued", result_key=None)
	media_dao = FakeMediaDAO([])
	job_dao.events.clear()

	await archive_service.process_media_archive_job(job_dao.job.id, job_dao=job_dao, media_dao=media_dao, now=now)

	assert job_dao.job.status == "failed"
	assert [event["event_type"] for event in job_dao.events][-1] == "failed"


@pytest.mark.asyncio
async def test_cleanup_expired_archive_results(monkeypatch):
	org_id = uuid4()
	now = datetime.datetime(2026, 4, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)
	expired_job = _archive_job(org_id=org_id, status="done", expires_delta_seconds=-1, result_key="org/x/archives/expired.zip")

	class FakeResult:
		def __init__(self, items):
			self._items = items

		def scalars(self):
			return self

		def all(self):
			return list(self._items)

	class FakeSession:
		def __init__(self):
			self.commits = 0
			self.rollbacks = 0

		async def execute(self, stmt):
			return FakeResult([expired_job])

		async def commit(self):
			self.commits += 1

		async def rollback(self):
			self.rollbacks += 1

	deleted = []
	logged_events = []
	session = FakeSession()

	class CleanupJobDAO:
		def __init__(self, _session):
			self._session = _session

		async def log_event(self, **kwargs):
			logged_events.append(kwargs)
			return SimpleNamespace(**kwargs)

	async def fake_get_session():
		yield session

	monkeypatch.setattr(archive_service, "get_session", fake_get_session)
	monkeypatch.setattr(archive_service, "delete_object", lambda key: deleted.append(key))
	monkeypatch.setattr(archive_service, "MediaArchiveJobDAO", CleanupJobDAO)

	await archive_service.cleanup_expired_archive_results(now=now)

	assert deleted == ["org/x/archives/expired.zip"]
	assert session.commits == 1
	assert [event["event_type"] for event in logged_events] == ["result_expired"]





