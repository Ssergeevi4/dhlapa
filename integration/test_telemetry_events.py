"""Integration tests for ProcessTelemetryBatchUseCase with real DB.

Use case + real TelemetryEventDAO + real db_session.
Verifies that valid events are persisted, rejected events are not,
and DB-level deduplication (ON CONFLICT DO NOTHING) works end-to-end.
No HTTP layer involved — same pattern as test_finalize_flow.py.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.daos.telemetry_event import TelemetryEventDAO
from db.models.telemetry_event import TelemetryEventModel
from api.v1.schemas.telemetry_event import TelemetryEventRequestSchema
from exceptions.telemetry_event import TelemetryErrorCode
from use_cases.telemetry_event import ProcessTelemetryBatchUseCase

NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_use_case(db_session: AsyncSession) -> ProcessTelemetryBatchUseCase:
    return ProcessTelemetryBatchUseCase(
        _telemetry_event_dao=TelemetryEventDAO(db_session),
        _now_fn=lambda: NOW,
    )


def _event(**kwargs) -> TelemetryEventRequestSchema:
    defaults = {
        "event_id": uuid4(),
        "event_type": "session_start",
        "occurred_at": NOW - timedelta(hours=1),
    }
    defaults.update(kwargs)
    return TelemetryEventRequestSchema(**defaults)


async def _count_in_db(db_session: AsyncSession, user_id) -> int:
    result = await db_session.execute(
        select(func.count())
        .select_from(TelemetryEventModel)
        .where(TelemetryEventModel.user_id == user_id)
    )
    return result.scalar()


# ---------------------------------------------------------------------------
# Happy path — данные физически попадают в БД
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_batch_persists_all_events_to_db(db_session: AsyncSession):
    user_id, org_id = uuid4(), uuid4()
    use_case = _make_use_case(db_session)
    events = [_event(), _event(event_type="paywall_show"), _event(event_type="session_end")]

    result = await use_case.execute(events=events, user_id=user_id, org_id=org_id)

    assert result.accepted_count == 3
    assert result.rejected_count == 0
    assert await _count_in_db(db_session, user_id) == 3


@pytest.mark.asyncio
async def test_valid_event_stored_with_correct_org_and_user(db_session: AsyncSession):
    user_id, org_id = uuid4(), uuid4()
    use_case = _make_use_case(db_session)

    await use_case.execute(events=[_event()], user_id=user_id, org_id=org_id)

    row = (await db_session.execute(
        select(TelemetryEventModel).where(TelemetryEventModel.user_id == user_id)
    )).scalar_one()
    assert row.user_id == user_id
    assert row.org_id == org_id


# ---------------------------------------------------------------------------
# Partial reject — только валидные события попадают в БД
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pii_event_not_persisted_valid_ones_are(db_session: AsyncSession):
    """PII-событие отклоняется, но остальные два записываются в БД."""
    user_id, org_id = uuid4(), uuid4()
    use_case = _make_use_case(db_session)
    events = [
        _event(),                                       # ok
        _event(meta={"email": "leaked@example.com"}),  # PII → rejected
        _event(event_type="paywall_show"),              # ok
    ]

    result = await use_case.execute(events=events, user_id=user_id, org_id=org_id)

    assert result.accepted_count == 2
    assert result.rejected_count == 1
    assert result.errors[0].code == TelemetryErrorCode.PII_DETECTED
    assert result.errors[0].index == 1
    assert await _count_in_db(db_session, user_id) == 2


@pytest.mark.asyncio
async def test_old_timestamp_event_not_persisted(db_session: AsyncSession):
    user_id, org_id = uuid4(), uuid4()
    use_case = _make_use_case(db_session)
    events = [
        _event(),                                          # ok
        _event(occurred_at=NOW - timedelta(days=31)),      # too old
    ]

    result = await use_case.execute(events=events, user_id=user_id, org_id=org_id)

    assert result.accepted_count == 1
    assert result.rejected_count == 1
    assert await _count_in_db(db_session, user_id) == 1


@pytest.mark.asyncio
async def test_future_timestamp_event_not_persisted(db_session: AsyncSession):
    user_id, org_id = uuid4(), uuid4()
    use_case = _make_use_case(db_session)

    result = await use_case.execute(
        events=[_event(occurred_at=NOW + timedelta(minutes=10))],
        user_id=user_id,
        org_id=org_id,
    )

    assert result.rejected_count == 1
    assert await _count_in_db(db_session, user_id) == 0


@pytest.mark.asyncio
async def test_pii_error_does_not_contain_pii_value(db_session: AsyncSession):
    """Критерий приёмки: объект ошибки не содержит сырых значений payload."""
    use_case = _make_use_case(db_session)
    result = await use_case.execute(
        events=[_event(meta={"email": "secret@example.com"})],
        user_id=uuid4(),
        org_id=uuid4(),
    )

    from dataclasses import asdict
    error_dict = asdict(result.errors[0])
    for value in error_dict.values():
        assert "secret@example.com" not in str(value)


# ---------------------------------------------------------------------------
# Дубликаты на уровне БД (ON CONFLICT DO NOTHING)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_event_id_counted_as_accepted_not_error(db_session: AsyncSession):
    """Дубликат event_id для того же user_id — use case считает его accepted,
    в БД остаётся одна запись (ON CONFLICT DO NOTHING)."""
    user_id, org_id = uuid4(), uuid4()
    use_case = _make_use_case(db_session)
    dup_id = uuid4()
    event = _event(event_id=dup_id)

    # Первый вызов — вставляет
    r1 = await use_case.execute(events=[event], user_id=user_id, org_id=org_id)
    assert r1.accepted_count == 1

    # Второй вызов — дубликат: accepted (не rejected), в БД по-прежнему 1 запись
    r2 = await use_case.execute(events=[event], user_id=user_id, org_id=org_id)
    assert r2.accepted_count == 1
    assert r2.rejected_count == 0
    assert r2.errors == []
    assert await _count_in_db(db_session, user_id) == 1


@pytest.mark.asyncio
async def test_same_event_id_different_users_both_persisted(db_session: AsyncSession):
    """Уникальность (user_id, event_id): одинаковый event_id у двух пользователей
    создаёт две независимые записи."""
    org_id = uuid4()
    user_a, user_b = uuid4(), uuid4()
    shared_event_id = uuid4()

    use_case_a = _make_use_case(db_session)
    use_case_b = _make_use_case(db_session)

    await use_case_a.execute(
        events=[_event(event_id=shared_event_id)], user_id=user_a, org_id=org_id
    )
    await use_case_b.execute(
        events=[_event(event_id=shared_event_id)], user_id=user_b, org_id=org_id
    )

    assert await _count_in_db(db_session, user_a) == 1
    assert await _count_in_db(db_session, user_b) == 1
