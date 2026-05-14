import uuid
import datetime

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models.booking_request import BookingRequestModel
from db.models.booking_slot import BookingSlotModel
from db.models.appointment import AppointmentModel
from src.use_cases.booking_request import (
    CreatePublicBookingRequestUseCase,
    ProcessBookingRequestActionUseCase,
)
from db.daos import (
    BookingRequestDAO,
    BookingSlotDAO,
    AppointmentDAO,
    ClientDAO,
    PriceServiceDAO,
)

# ── ID из init_test_db.sql ────────────────────────────────────
MASTER_ID = uuid.UUID("93b3846d-3659-40f3-8914-af789e216917")
ANOTHER_MASTER_ID = uuid.UUID("93b3846d-3659-40f3-8914-af789e216918")
ORG_ID = uuid.UUID("06e938dc-fb68-40ee-9dce-0aa8fa8370ca")
SLOT_ID = uuid.UUID("58e938dc-fb68-41ee-9dce-5aa8fa8371ca")
ANOTHER_SLOT_ID = uuid.UUID("58e938dc-fb68-41ee-9dce-5aa8fa8370ca")
SERVICE_ID = uuid.UUID("01652e2b-dc33-4a74-8ecc-21d918145d37")


@pytest.fixture
async def booking_use_case_factory(engine):

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    created_sessions = []

    async def _create_use_case():
        session = session_factory()
        created_sessions.append(session)
        return CreatePublicBookingRequestUseCase(
            _booking_request_dao=BookingRequestDAO(session),
            _booking_slot_dao=BookingSlotDAO(session),
            _appointment_dao=AppointmentDAO(session),
            _price_service_dao=PriceServiceDAO(session),
            _client_dao=ClientDAO(session),
            _session=session,
        )

    yield _create_use_case

    for session in created_sessions:
        await session.close()


@pytest.fixture
async def process_action_use_case_factory(engine):
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    created_sessions = []

    async def _create_use_case():
        session = session_factory()
        created_sessions.append(session)
        inner = ProcessBookingRequestActionUseCase(
            _booking_request_dao=BookingRequestDAO(session),
            _booking_slot_dao=BookingSlotDAO(session),
            _appointment_dao=AppointmentDAO(session),
            _client_dao=ClientDAO(session),
            _session=session,
        )

        # Оборачиваем: после execute делаем commit,
        # чтобы проверочная db_session (другой connection) видела изменения.
        class _UCWithCommit:
            async def execute(self, *args, **kwargs):
                result = await inner.execute(*args, **kwargs)
                await session.commit()
                return result

        return _UCWithCommit()

    yield _create_use_case

    for session in created_sessions:
        await session.close()


@pytest.fixture
async def pending_booking_request(db_session: AsyncSession):
    """Создаёт pending-заявку + переводит слот в reserved. Возвращает модель."""
    request_id = uuid.uuid4()
    now = datetime.datetime.now(datetime.timezone.utc)

    booking_request = BookingRequestModel(
        id=request_id,
        org_id=ORG_ID,
        master_id=MASTER_ID,
        slot_id=SLOT_ID,
        service_id=SERVICE_ID,
        client_name="Test Client",
        client_phone="79001234567",
        status="pending",
        reserved_until=now + datetime.timedelta(minutes=15),
        public_token=str(uuid.uuid4()),
    )

    db_session.add(booking_request)
    await db_session.execute(
        update(BookingSlotModel)
        .where(BookingSlotModel.id == SLOT_ID)
        .values(status="reserved")
    )
    await db_session.commit()
    await db_session.refresh(booking_request)
    return booking_request


@pytest.fixture
async def approved_booking_request(db_session: AsyncSession):
    """Создаёт approved-заявку + appointment + слот booked. Возвращает модель."""
    now = datetime.datetime.now(datetime.timezone.utc)

    # Сначала нужен client для FK в appointments
    from db.daos import ClientDAO
    client_dao = ClientDAO(db_session)
    client_id = await client_dao.get_or_create_client(
        org_id=ORG_ID,
        user_id=MASTER_ID,
        name="Approved Client",
        phone="79009876543",
    )

    # Получаем слот чтобы взять visit_at
    slot = await db_session.get(BookingSlotModel, SLOT_ID)

    # Создаём appointment
    appointment = AppointmentModel(
        org_id=ORG_ID,
        user_id=MASTER_ID,
        client_id=client_id,
        visit_at=slot.starts_at,
        procedure_service_id=SERVICE_ID,
        status="scheduled",
    )
    db_session.add(appointment)
    await db_session.flush()

    # Создаём approved request
    request_id = uuid.uuid4()
    booking_request = BookingRequestModel(
        id=request_id,
        org_id=ORG_ID,
        master_id=MASTER_ID,
        slot_id=SLOT_ID,
        service_id=SERVICE_ID,
        appointment_id=appointment.id,
        client_name="Approved Client",
        client_phone="79009876543",
        status="approved",
        reserved_until=now,
        public_token=str(uuid.uuid4()),
    )

    db_session.add(booking_request)
    await db_session.execute(
        update(BookingSlotModel)
        .where(BookingSlotModel.id == SLOT_ID)
        .values(status="booked")
    )
    await db_session.commit()
    await db_session.refresh(booking_request)
    return booking_request