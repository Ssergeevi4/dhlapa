import asyncio
import datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import NullPool, delete, update
from db.models import AdminSessionModel, BookingRequestModel, AppointmentModel, ClientModel
from db.models.notification_outbox import NotificationOutboxModel
from db.models.booking_slot import BookingSlotModel
from db.models.user_device import UserDeviceModel
from db.models.telemetry_event import TelemetryEventModel
from config import settings


@pytest.fixture(scope="session")
def engine():
    db_url = (
        f"postgresql+asyncpg://{settings.DB_USER}:{settings.DB_PASS}@"
        f"{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )
    return create_async_engine(db_url, poolclass=NullPool)


@pytest.fixture
async def db_session(engine):
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture(scope="session", autouse=True)
def seed_booking_test_data(request, setup_db):
    """
    Seed static FK-required entities once per session (after setup_db wipes schema).
    Uses same pattern as setup_db (sync fixture + asyncio.run) to avoid
    pytest-asyncio session-scope issues.
    Uses different UUIDs from slot_generation tests — no conflict.
    """
    if getattr(request.config, "_skip_db_setup", False):
        return

    from tests.fixtures.usecase.booking_request import (
    MASTER_ID, ANOTHER_MASTER_ID, ORG_ID, SLOT_ID, ANOTHER_SLOT_ID, SERVICE_ID,
)
    from db.models.organization import OrganizationModel
    from db.models.user import UserModel
    from db.models.price_service import PriceServiceModel

    db_url = (
        f"postgresql+asyncpg://{settings.DB_USER}:{settings.DB_PASS}@"
        f"{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )

    async def _seed():
        engine = create_async_engine(db_url, poolclass=NullPool)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            session.add(OrganizationModel(id=ORG_ID, name="Test Org", timezone="Europe/Moscow"))
            await session.flush()

            session.add(UserModel(id=MASTER_ID, org_id=ORG_ID, full_name="Test Master", status="active"))
            await session.flush()

            session.add(UserModel(id=ANOTHER_MASTER_ID, org_id=ORG_ID, full_name="Another Master", status="active"))
            await session.flush()

            session.add(PriceServiceModel(
                id=SERVICE_ID, org_id=ORG_ID,
                title_services="Medical pedicure",
                duration_services=60,
                price_services=5000,
            ))
            await session.flush()

            session.add_all([
                BookingSlotModel(
                    id=SLOT_ID, org_id=ORG_ID, master_id=MASTER_ID,
                    starts_at=datetime.datetime(2026, 3, 19, 11, 0, tzinfo=datetime.timezone.utc),
                    ends_at=datetime.datetime(2026, 3, 19, 12, 0, tzinfo=datetime.timezone.utc),
                    status="free",
                ),
                BookingSlotModel(
                    id=ANOTHER_SLOT_ID, org_id=ORG_ID, master_id=MASTER_ID,
                    starts_at=datetime.datetime(2026, 3, 19, 12, 0, tzinfo=datetime.timezone.utc),
                    ends_at=datetime.datetime(2026, 3, 19, 13, 0, tzinfo=datetime.timezone.utc),
                    status="free",
                ),
            ])
            await session.commit()
        await engine.dispose()

    asyncio.run(_seed())


@pytest.fixture(autouse=True)
async def clear_data(request, db_session: AsyncSession):
    """Reset dynamic data before each test. Static seed rows (org/user/service/slots) are NOT deleted."""
    if getattr(request.config, "_skip_db_setup", False):
        yield
        return

    await db_session.execute(delete(BookingRequestModel))
    await db_session.execute(delete(AppointmentModel))
    await db_session.execute(delete(ClientModel))
    await db_session.execute(delete(NotificationOutboxModel))
    await db_session.execute(delete(UserDeviceModel))
    await db_session.execute(delete(TelemetryEventModel))
    await db_session.execute(delete(AdminSessionModel))
    await db_session.execute(update(BookingSlotModel).values(status="free"))
    await db_session.commit()
    db_session.expire_all()
    yield
