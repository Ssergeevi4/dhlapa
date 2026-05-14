from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.daos.telemetry_event import TelemetryEventDAO
from db.models.telemetry_event import TelemetryEventModel
from dto.telemetry_event import TelemetryEventDTO


@pytest.mark.asyncio
async def test_telemetry_insert_creates_record(db_session: AsyncSession):
    user_id = uuid4()
    org_id = uuid4()
    event_id = uuid4()
    data = {
        "event_id": event_id,
        "event_type": "session_start",
        "occurred_at": datetime.now(timezone.utc),
    }

    dao = TelemetryEventDAO(db_session)
    result = await dao.create(user_id=user_id, org_id=org_id, data=data)

    assert result is not None
    assert isinstance(result, TelemetryEventDTO)
    assert result.event_id == event_id
    assert result.user_id == user_id
    assert result.org_id == org_id
    assert result.event_type == "session_start"
    assert result.id is not None
    assert result.created_at is not None


@pytest.mark.asyncio
async def test_telemetry_insert_duplicate_is_idempotent(db_session: AsyncSession):
    user_id = uuid4()
    org_id = uuid4()
    event_id = uuid4()
    data = {
        "event_id": event_id,
        "event_type": "screen_view",
        "occurred_at": datetime.now(timezone.utc),
        "screen_name": "HomeScreen",
    }

    dao = TelemetryEventDAO(db_session)
    await dao.create(user_id=user_id, org_id=org_id, data=data)
    await dao.create(user_id=user_id, org_id=org_id, data=data)

    count_result = await db_session.execute(
        select(func.count())
        .select_from(TelemetryEventModel)
        .where(
            TelemetryEventModel.user_id == user_id,
            TelemetryEventModel.event_id == event_id,
        )
    )
    assert count_result.scalar() == 1


@pytest.mark.asyncio
async def test_telemetry_insert_duplicate_returns_none(db_session: AsyncSession):
    user_id = uuid4()
    org_id = uuid4()
    event_id = uuid4()
    data = {
        "event_id": event_id,
        "event_type": "feature_use",
        "occurred_at": datetime.now(timezone.utc),
        "feature_code": "export_pdf",
    }

    dao = TelemetryEventDAO(db_session)
    first = await dao.create(user_id=user_id, org_id=org_id, data=data)
    second = await dao.create(user_id=user_id, org_id=org_id, data=data)

    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_telemetry_same_event_id_different_users_are_independent(db_session: AsyncSession):
    """Уникальность (user_id, event_id), а не просто event_id — два разных пользователя
    могут иметь одинаковый event_id без конфликта."""
    org_id = uuid4()
    event_id = uuid4()
    user_id_a = uuid4()
    user_id_b = uuid4()
    occurred_at = datetime.now(timezone.utc)

    dao = TelemetryEventDAO(db_session)
    result_a = await dao.create(
        user_id=user_id_a,
        org_id=org_id,
        data={"event_id": event_id, "event_type": "session_start", "occurred_at": occurred_at},
    )
    result_b = await dao.create(
        user_id=user_id_b,
        org_id=org_id,
        data={"event_id": event_id, "event_type": "session_start", "occurred_at": occurred_at},
    )

    assert result_a is not None
    assert result_b is not None
    assert result_a.id != result_b.id

    count_result = await db_session.execute(
        select(func.count())
        .select_from(TelemetryEventModel)
        .where(TelemetryEventModel.event_id == event_id)
    )
    assert count_result.scalar() == 2
