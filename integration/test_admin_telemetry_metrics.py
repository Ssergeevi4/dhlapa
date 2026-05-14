"""Integration tests for GetTelemetryMetricsUseCase with real DB.

Use case + real TelemetryEventDAO + real db_session.
Verifies DAU/MAU/top-features SQL aggregations, date filtering,
and deduplication of user counts.
No HTTP layer — same pattern as test_telemetry_endpoint.py.
"""
from datetime import date, datetime, timezone, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from db.daos.telemetry_event import TelemetryEventDAO
from db.models.telemetry_event import TelemetryEventModel
from exceptions.admin_telemetry import AdminTelemetryDateRangeError, AdminTelemetryOverRangeError
from use_cases.admin_telemetry import GetTelemetryMetricsUseCase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_use_case(db_session: AsyncSession) -> GetTelemetryMetricsUseCase:
    return GetTelemetryMetricsUseCase(TelemetryEventDAO(db_session))


def _dt(d: date, hour: int = 12) -> datetime:
    return datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=timezone.utc)


async def _seed(db_session: AsyncSession, events: list[dict]) -> None:
    for e in events:
        db_session.add(TelemetryEventModel(
            event_id=e.get("event_id", uuid4()),
            user_id=e["user_id"],
            org_id=e.get("org_id", uuid4()),
            event_type=e.get("event_type", "session_start"),
            occurred_at=e["occurred_at"],
            feature_code=e.get("feature_code"),
        ))
    await db_session.flush()


APR_1  = date(2026, 4, 1)
APR_2  = date(2026, 4, 2)
APR_29 = date(2026, 4, 29)
MAY_1  = date(2026, 5, 1)


# ---------------------------------------------------------------------------
# DAU
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dau_counts_distinct_users_per_day(db_session: AsyncSession):
    user_a, user_b = uuid4(), uuid4()
    await _seed(db_session, [
        {"user_id": user_a, "occurred_at": _dt(APR_1)},
        {"user_id": user_a, "occurred_at": _dt(APR_1, hour=15)},  # тот же пользователь — не дублируется
        {"user_id": user_b, "occurred_at": _dt(APR_1)},
        {"user_id": user_a, "occurred_at": _dt(APR_2)},
    ])

    result = await _make_use_case(db_session).execute(date_from=APR_1, date_to=APR_2)

    by_day = {entry.day: entry.users for entry in result.dau}
    assert by_day[APR_1] == 2
    assert by_day[APR_2] == 1


@pytest.mark.asyncio
async def test_dau_excludes_events_outside_period(db_session: AsyncSession):
    user_id = uuid4()
    await _seed(db_session, [
        {"user_id": user_id, "occurred_at": _dt(APR_1)},
        {"user_id": user_id, "occurred_at": _dt(MAY_1)},  # за пределами диапазона
    ])

    result = await _make_use_case(db_session).execute(date_from=APR_1, date_to=APR_29)

    days = {entry.day for entry in result.dau}
    assert APR_1 in days
    assert MAY_1 not in days


@pytest.mark.asyncio
async def test_dau_empty_when_no_events_in_range(db_session: AsyncSession):
    result = await _make_use_case(db_session).execute(date_from=APR_1, date_to=APR_2)

    assert result.dau == []


# ---------------------------------------------------------------------------
# MAU
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mau_counts_distinct_users_per_month(db_session: AsyncSession):
    user_a, user_b, user_c = uuid4(), uuid4(), uuid4()
    await _seed(db_session, [
        {"user_id": user_a, "occurred_at": _dt(APR_1)},
        {"user_id": user_b, "occurred_at": _dt(APR_29)},
        {"user_id": user_c, "occurred_at": _dt(MAY_1)},
    ])

    result = await _make_use_case(db_session).execute(date_from=APR_1, date_to=MAY_1)

    by_month = {entry.month: entry.users for entry in result.mau}
    assert by_month["2026-04"] == 2
    assert by_month["2026-05"] == 1


@pytest.mark.asyncio
async def test_mau_same_user_multiple_events_counts_once(db_session: AsyncSession):
    user_id = uuid4()
    await _seed(db_session, [
        {"user_id": user_id, "occurred_at": _dt(APR_1)},
        {"user_id": user_id, "occurred_at": _dt(APR_2)},
        {"user_id": user_id, "occurred_at": _dt(APR_29)},
    ])

    result = await _make_use_case(db_session).execute(date_from=APR_1, date_to=APR_29)

    assert len(result.mau) == 1
    assert result.mau[0].month == "2026-04"
    assert result.mau[0].users == 1


# ---------------------------------------------------------------------------
# Top features
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_top_features_returns_correct_events_and_users_count(db_session: AsyncSession):
    user_a, user_b = uuid4(), uuid4()
    await _seed(db_session, [
        {"user_id": user_a, "event_type": "feature_use", "feature_code": "pdf_export", "occurred_at": _dt(APR_1)},
        {"user_id": user_b, "event_type": "feature_use", "feature_code": "pdf_export", "occurred_at": _dt(APR_1)},
        {"user_id": user_a, "event_type": "feature_use", "feature_code": "pdf_export", "occurred_at": _dt(APR_2)},
        {"user_id": user_a, "event_type": "feature_use", "feature_code": "sms_remind",  "occurred_at": _dt(APR_1)},
    ])

    result = await _make_use_case(db_session).execute(date_from=APR_1, date_to=APR_29)

    by_code = {f.feature_code: f for f in result.top_features}
    pdf = by_code["pdf_export"]
    assert pdf.events_count == 3
    assert pdf.users_count == 2

    sms = by_code["sms_remind"]
    assert sms.events_count == 1
    assert sms.users_count == 1


@pytest.mark.asyncio
async def test_top_features_sorted_by_events_count_desc(db_session: AsyncSession):
    await _seed(db_session, [
        {"user_id": uuid4(), "event_type": "feature_use", "feature_code": "rare_feat",    "occurred_at": _dt(APR_1)},
        {"user_id": uuid4(), "event_type": "feature_use", "feature_code": "popular_feat", "occurred_at": _dt(APR_1)},
        {"user_id": uuid4(), "event_type": "feature_use", "feature_code": "popular_feat", "occurred_at": _dt(APR_2)},
        {"user_id": uuid4(), "event_type": "feature_use", "feature_code": "popular_feat", "occurred_at": _dt(APR_29)},
    ])

    result = await _make_use_case(db_session).execute(date_from=APR_1, date_to=APR_29)

    assert result.top_features[0].feature_code == "popular_feat"


@pytest.mark.asyncio
async def test_top_features_excludes_non_feature_use_events(db_session: AsyncSession):
    user_id = uuid4()
    await _seed(db_session, [
        {"user_id": user_id, "event_type": "session_start", "occurred_at": _dt(APR_1)},
        {"user_id": user_id, "event_type": "screen_view",   "occurred_at": _dt(APR_1)},
    ])

    result = await _make_use_case(db_session).execute(date_from=APR_1, date_to=APR_29)

    assert result.top_features == []


@pytest.mark.asyncio
async def test_top_features_excludes_events_outside_period(db_session: AsyncSession):
    user_id = uuid4()
    await _seed(db_session, [
        {"user_id": user_id, "event_type": "feature_use", "feature_code": "pdf_export", "occurred_at": _dt(APR_1)},
        {"user_id": user_id, "event_type": "feature_use", "feature_code": "pdf_export", "occurred_at": _dt(MAY_1)},
    ])

    result = await _make_use_case(db_session).execute(date_from=APR_1, date_to=APR_29)

    assert result.top_features[0].events_count == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_raises_when_date_from_after_date_to(db_session: AsyncSession):
    with pytest.raises(AdminTelemetryDateRangeError):
        await _make_use_case(db_session).execute(date_from=APR_29, date_to=APR_1)


@pytest.mark.asyncio
async def test_raises_when_range_exceeds_180_days(db_session: AsyncSession):
    date_from = date(2026, 1, 1)
    date_to = date_from + timedelta(days=181)

    with pytest.raises(AdminTelemetryOverRangeError):
        await _make_use_case(db_session).execute(date_from=date_from, date_to=date_to)


@pytest.mark.asyncio
async def test_single_day_range_inclusive(db_session: AsyncSession):
    user_id = uuid4()
    await _seed(db_session, [
        {"user_id": user_id, "occurred_at": _dt(APR_1)},
    ])

    result = await _make_use_case(db_session).execute(date_from=APR_1, date_to=APR_1)

    assert result.dau[0].users == 1
