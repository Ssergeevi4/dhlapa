from datetime import date, datetime, timezone
from uuid import uuid4
import pytest
from dto.booking_slot_generation_job import BookingSlotGenerationJobDTO
from use_cases.booking_slot_generation_job import CreateGenerationJobUseCase
from db.daos.booking_slot_generation_job import BookingSlotGenerationJobDAO


pytestmark = pytest.mark.no_db


class FakeCommitSession:
    def __init__(self):
        self.commit_calls = 0

    async def commit(self):
        self.commit_calls += 1


class FakeJobDAO(BookingSlotGenerationJobDAO):
    def __init__(self):
        self._session = FakeCommitSession()
        self.job = None
        self.events = []

    async def create(self, org_id, master_id, range_from, range_to, reason):
        now = datetime.now(timezone.utc)
        job = BookingSlotGenerationJobDTO(
            id=uuid4(),
            org_id=org_id,
            master_id=master_id,
            range_from=range_from,
            range_to=range_to,
            reason=reason,
            status="queued",
            error_text=None,
            attempts=0,
            created_at=now,
            started_at=None,
            finished_at=None,
        )
        self.job = job
        return job

    async def log_event(self, **kwargs):
        self.events.append(kwargs)
        return None


@pytest.mark.asyncio
async def test_create_generation_job_logs_queued_and_commits(monkeypatch):
    dao = FakeJobDAO()
    use_case = CreateGenerationJobUseCase(dao, None)
    today = date.today()

    job = await use_case.execute(
        payload=type("P", (), {"range_from": today, "range_to": today, "reason": "manual"})(),
        org_id=uuid4(),
        master_id=uuid4(),
    )

    assert job.status == "queued"
    assert [e["event_type"] for e in dao.events] == ["queued"]
