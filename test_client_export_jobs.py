import datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import use_cases.client_export_jobs as export_use_cases
from db.daos.client_export_job import ClientExportJobDAO
from dto.client_export_job import ClientExportJobDTO
from exceptions.media import (
    MediaArchiveJobFailedError,
    MediaArchiveJobNotReadyError,
    MediaArchiveResultExpiredError,
)
from use_cases.client_export_jobs import (
    CreateClientExportJobUseCase,
    GetClientExportJobDownloadLinkUseCase,
    GetClientExportJobStatusUseCase,
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


class FakeJobDAO(ClientExportJobDAO):
    def __init__(self, job: ClientExportJobDTO | None = None):
        self._session: Any = FakeCommitSession()
        self.job = job
        self.events = []

    async def create_job(self, *, org_id, requested_by, status="queued"):
        job = ClientExportJobDTO(
            id=uuid4(),
            org_id=org_id,
            requested_by=requested_by,
            status=status,
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
        self.job = ClientExportJobDTO(**data)
        return self.job

    async def log_event(self, **kwargs):
        self.events.append(kwargs)
        return SimpleNamespace(**kwargs)


def _export_job(org_id=None, status="done", expires_delta_seconds=120, result_key: str | None = "org/x/exports/job.csv", requested_by=None, error_message=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    return ClientExportJobDTO(
        id=uuid4(),
        org_id=org_id or uuid4(),
        requested_by=requested_by or uuid4(),
        status=status,
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
async def test_create_export_job_sets_queued_and_logs_audit(monkeypatch):
    org_id = uuid4()
    job_dao = FakeJobDAO()
    
    # Mock ARQ pool
    enqueued_jobs = []
    class FakeArqPool:
        async def enqueue_job(self, *args, **kwargs):
            enqueued_jobs.append((args, kwargs))
            return SimpleNamespace(job_id="fake-job-id")
    
    arq = FakeArqPool()
    use_case = CreateClientExportJobUseCase(None, job_dao, arq)

    job = await use_case.execute(org_id=org_id, requested_by=uuid4(), ip_address="127.0.0.1", user_agent="pytest")

    assert job.status == "queued"
    assert len(enqueued_jobs) == 1
    assert enqueued_jobs[0][0][0] == "process_client_export_job"
    assert [event["event_type"] for event in job_dao.events] == ["queued"]


@pytest.mark.asyncio
async def test_export_download_link_happy_and_negative_paths(monkeypatch):
    org_id = uuid4()
    job = _export_job(org_id=org_id, status="done", expires_delta_seconds=90)
    job_dao = FakeJobDAO(job)

    monkeypatch.setattr(
        export_use_cases,
        "generate_presigned_get_url",
        lambda *, key, expires_in: {"url": f"https://example.invalid/{key}", "expires_in": expires_in},
    )
    monkeypatch.setattr(export_use_cases.settings, "CLIENT_EXPORT_LINK_EXPIRES_SECONDS", 60, raising=False)

    status = await GetClientExportJobStatusUseCase(job_dao).execute(job_id=job.id, org_id=org_id)
    assert status.status == "done"

    # Correct user gets link
    link = await GetClientExportJobDownloadLinkUseCase(job_dao).execute(job_id=job.id, org_id=org_id, user_id=job.requested_by)
    assert link["status"] == "done"
    assert link["download_url"].startswith("https://example.invalid/")
    assert 0 < link["expires_in"] <= 60
    assert [event["event_type"] for event in job_dao.events] == ["link_issued"]

    # Not ready / failed / expired
    queued_job = _export_job(org_id=org_id, status="queued", expires_delta_seconds=90)
    job_dao.job = queued_job
    with pytest.raises(MediaArchiveJobNotReadyError):
        await GetClientExportJobDownloadLinkUseCase(job_dao).execute(job_id=queued_job.id, org_id=org_id, user_id=queued_job.requested_by)

    failed_job = _export_job(org_id=org_id, status="failed", error_message="boom")
    job_dao.job = failed_job
    with pytest.raises(MediaArchiveJobFailedError):
        await GetClientExportJobDownloadLinkUseCase(job_dao).execute(job_id=failed_job.id, org_id=org_id, user_id=failed_job.requested_by)

    expired_job = _export_job(org_id=org_id, status="done", expires_delta_seconds=-86400)
    job_dao.job = expired_job
    with pytest.raises(MediaArchiveResultExpiredError):
        await GetClientExportJobDownloadLinkUseCase(job_dao).execute(job_id=expired_job.id, org_id=org_id, user_id=expired_job.requested_by)
