"""Unit tests for ProcessTelemetryBatchUseCase.

Uses FakeDAO pattern (no DB, no HTTP) — same approach as test_media_use_cases.py.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from api.v1.schemas.telemetry_event import TelemetryEventRequestSchema
from exceptions.telemetry_event import TelemetryErrorCode
from use_cases.telemetry_event import ProcessTelemetryBatchUseCase

NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


class FakeTelemetryDAO:
    def __init__(self, *, duplicate_event_ids: set | None = None):
        self.saved: list[dict] = []
        self._duplicates = duplicate_event_ids or set()

    async def create(self, user_id, org_id, data: dict):
        if data.get("event_id") in self._duplicates:
            return None
        self.saved.append({"user_id": user_id, "org_id": org_id, **data})
        return object()  # non-None means accepted

    async def create_batch(self, events_data: list[dict]) -> None:
        for row in events_data:
            if row.get("event_id") not in self._duplicates:
                self.saved.append(row)


def _make_use_case(dao=None):
    return ProcessTelemetryBatchUseCase(
        _telemetry_event_dao=dao or FakeTelemetryDAO(),
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_events_accepted():
    dao = FakeTelemetryDAO()
    use_case = _make_use_case(dao)
    events = [_event(), _event(), _event()]
    user_id, org_id = uuid4(), uuid4()

    result = await use_case.execute(events=events, user_id=user_id, org_id=org_id)

    assert result.accepted_count == 3
    assert result.rejected_count == 0
    assert result.errors == []
    assert len(dao.saved) == 3


@pytest.mark.asyncio
async def test_valid_event_sets_user_and_org_from_context():
    """user_id и org_id берутся из параметров execute, не из payload."""
    dao = FakeTelemetryDAO()
    use_case = _make_use_case(dao)
    user_id, org_id = uuid4(), uuid4()

    await use_case.execute(events=[_event()], user_id=user_id, org_id=org_id)

    assert dao.saved[0]["user_id"] == user_id
    assert dao.saved[0]["org_id"] == org_id


# ---------------------------------------------------------------------------
# Timestamp validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timestamp_too_old_rejected():
    use_case = _make_use_case()
    old_event = _event(occurred_at=NOW - timedelta(days=31))

    result = await use_case.execute(events=[old_event], user_id=uuid4(), org_id=uuid4())

    assert result.rejected_count == 1
    assert result.accepted_count == 0
    assert result.errors[0].code == TelemetryErrorCode.TIMESTAMP_TOO_OLD
    assert result.errors[0].index == 0


@pytest.mark.asyncio
async def test_timestamp_in_future_rejected():
    use_case = _make_use_case()
    future_event = _event(occurred_at=NOW + timedelta(minutes=10))

    result = await use_case.execute(events=[future_event], user_id=uuid4(), org_id=uuid4())

    assert result.rejected_count == 1
    assert result.errors[0].code == TelemetryErrorCode.TIMESTAMP_IN_FUTURE


@pytest.mark.asyncio
async def test_timestamp_exactly_30_days_ago_accepted():
    use_case = _make_use_case()
    edge_event = _event(occurred_at=NOW - timedelta(days=30) + timedelta(seconds=1))

    result = await use_case.execute(events=[edge_event], user_id=uuid4(), org_id=uuid4())

    assert result.accepted_count == 1
    assert result.rejected_count == 0


@pytest.mark.asyncio
async def test_naive_datetime_treated_as_utc():
    """occurred_at без timezone не вызывает ошибку сравнения."""
    use_case = _make_use_case()
    naive_event = _event(occurred_at=(NOW - timedelta(hours=1)).replace(tzinfo=None))

    result = await use_case.execute(events=[naive_event], user_id=uuid4(), org_id=uuid4())

    assert result.accepted_count == 1


# ---------------------------------------------------------------------------
# PII detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pii_email_in_meta_rejected():
    use_case = _make_use_case()
    pii_event = _event(meta={"email": "user@example.com"})

    result = await use_case.execute(events=[pii_event], user_id=uuid4(), org_id=uuid4())

    assert result.rejected_count == 1
    assert result.errors[0].code == TelemetryErrorCode.PII_DETECTED
    assert result.errors[0].field == "meta.email"


@pytest.mark.asyncio
async def test_pii_phone_in_meta_rejected():
    use_case = _make_use_case()
    pii_event = _event(meta={"phone": "79001234567"})

    result = await use_case.execute(events=[pii_event], user_id=uuid4(), org_id=uuid4())

    assert result.errors[0].code == TelemetryErrorCode.PII_DETECTED
    assert result.errors[0].field == "meta.phone"


@pytest.mark.asyncio
async def test_pii_error_does_not_contain_value():
    """Критерий приёмки: ошибки не содержат сырые значения payload."""
    use_case = _make_use_case()
    pii_event = _event(meta={"email": "secret@example.com"})

    result = await use_case.execute(events=[pii_event], user_id=uuid4(), org_id=uuid4())

    error = result.errors[0]
    from dataclasses import asdict
    error_dict = asdict(error)
    for value in error_dict.values():
        assert "secret@example.com" not in str(value)


@pytest.mark.asyncio
async def test_pii_nested_in_meta_rejected():
    use_case = _make_use_case()
    nested_event = _event(meta={"context": {"email": "x@y.com"}})

    result = await use_case.execute(events=[nested_event], user_id=uuid4(), org_id=uuid4())

    assert result.errors[0].code == TelemetryErrorCode.PII_DETECTED
    assert "email" in result.errors[0].field


@pytest.mark.asyncio
async def test_clean_meta_accepted():
    use_case = _make_use_case()
    safe_event = _event(meta={"source": "push_notification", "version": "2.1.0"})

    result = await use_case.execute(events=[safe_event], user_id=uuid4(), org_id=uuid4())

    assert result.accepted_count == 1


# ---------------------------------------------------------------------------
# Meta size
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_meta_too_large_rejected():
    use_case = _make_use_case()
    large_meta = {"data": "x" * 2100}
    large_event = _event(meta=large_meta)

    result = await use_case.execute(events=[large_event], user_id=uuid4(), org_id=uuid4())

    assert result.rejected_count == 1
    assert result.errors[0].code == TelemetryErrorCode.META_TOO_LARGE


@pytest.mark.asyncio
async def test_meta_none_does_not_crash():
    use_case = _make_use_case()
    no_meta_event = _event(meta=None)

    result = await use_case.execute(events=[no_meta_event], user_id=uuid4(), org_id=uuid4())

    assert result.accepted_count == 1


# ---------------------------------------------------------------------------
# Required fields per event_type
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feature_use_without_feature_code_rejected():
    use_case = _make_use_case()
    bad_event = _event(event_type="feature_use", feature_code=None)

    result = await use_case.execute(events=[bad_event], user_id=uuid4(), org_id=uuid4())

    assert result.rejected_count == 1
    assert result.errors[0].code == TelemetryErrorCode.MISSING_REQUIRED_FIELD


@pytest.mark.asyncio
async def test_feature_use_with_feature_code_accepted():
    use_case = _make_use_case()
    ok_event = _event(event_type="feature_use", feature_code="export_pdf")

    result = await use_case.execute(events=[ok_event], user_id=uuid4(), org_id=uuid4())

    assert result.accepted_count == 1


@pytest.mark.asyncio
async def test_screen_view_without_screen_name_rejected():
    use_case = _make_use_case()
    bad_event = _event(event_type="screen_view", screen_name=None)

    result = await use_case.execute(events=[bad_event], user_id=uuid4(), org_id=uuid4())

    assert result.errors[0].code == TelemetryErrorCode.MISSING_REQUIRED_FIELD


@pytest.mark.asyncio
async def test_article_view_without_article_id_rejected():
    use_case = _make_use_case()
    bad_event = _event(event_type="article_view", article_id=None)

    result = await use_case.execute(events=[bad_event], user_id=uuid4(), org_id=uuid4())

    assert result.errors[0].code == TelemetryErrorCode.MISSING_REQUIRED_FIELD


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_event_counted_as_accepted():
    """Дубликат не ломает batch — считается accepted."""
    dup_id = uuid4()
    dao = FakeTelemetryDAO(duplicate_event_ids={dup_id})
    use_case = _make_use_case(dao)
    dup_event = _event(event_id=dup_id)

    result = await use_case.execute(events=[dup_event], user_id=uuid4(), org_id=uuid4())

    assert result.accepted_count == 1
    assert result.rejected_count == 0
    assert result.errors == []


# ---------------------------------------------------------------------------
# Partial batch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_batch_correct_indices():
    """5 событий: 3 валидных + 2 с ошибками. Проверяем индексы и счётчики."""
    use_case = _make_use_case()
    events = [
        _event(),                                          # index 0 — ok
        _event(occurred_at=NOW - timedelta(days=40)),      # index 1 — too old
        _event(),                                          # index 2 — ok
        _event(meta={"email": "x@y.com"}),                # index 3 — PII
        _event(),                                          # index 4 — ok
    ]

    result = await use_case.execute(events=events, user_id=uuid4(), org_id=uuid4())

    assert result.accepted_count == 3
    assert result.rejected_count == 2
    error_indices = {e.index for e in result.errors}
    assert error_indices == {1, 3}
