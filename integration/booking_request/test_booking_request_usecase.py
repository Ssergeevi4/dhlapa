import asyncio
from uuid import UUID
import pytest
from api.v1.schemas import BookingRequestCreateSchema
from db.models.appointment import AppointmentModel
from db.models.booking_request import BookingRequestModel
from db.models.booking_slot import BookingSlotModel
from exceptions import BookingSlotAlreadyBookedError, BookingRequestAlreadyExistsError

# Эти ID должны реально существовать в дампе (init_test_db.sql)
MASTER_ID = "93b3846d-3659-40f3-8914-af789e216917"
SLOT_ID = "58e938dc-fb68-41ee-9dce-5aa8fa8371ca"
ANOTHER_SLOT_ID = "58e938dc-fb68-41ee-9dce-5aa8fa8370ca"
SERVICE_ID = "01652e2b-dc33-4a74-8ecc-21d918145d37"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_race_condition(booking_use_case_factory):
    uc1 = await booking_use_case_factory()
    uc2 = await booking_use_case_factory()

    payload = BookingRequestCreateSchema(
        slot_id=UUID(SLOT_ID),
        service_id=SERVICE_ID,
        client_name="Race User",
        client_phone="79991112233",
    )

    results = await asyncio.gather(
        uc1.execute(payload, MASTER_ID),
        uc2.execute(payload, MASTER_ID),
        return_exceptions=True,
    )

    assert any(not isinstance(r, Exception) for r in results)
    assert any(isinstance(r, BookingSlotAlreadyBookedError) for r in results)


@pytest.mark.asyncio
async def test_anti_double_booking_same_day(booking_use_case_factory, db_session):
    booking_use_case = await booking_use_case_factory()

    phone = "79005556677"
    s1 = UUID(SLOT_ID)
    s2 = UUID(ANOTHER_SLOT_ID)

    payload = BookingRequestCreateSchema(
        slot_id=s1, service_id=SERVICE_ID, client_name="Ivan", client_phone=phone
    )
    await booking_use_case.execute(payload, MASTER_ID)

    payload_two = BookingRequestCreateSchema(
        slot_id=s2, service_id=SERVICE_ID, client_name="Ivan", client_phone=phone
    )

    with pytest.raises(BookingRequestAlreadyExistsError):
        await booking_use_case.execute(payload_two, MASTER_ID)


@pytest.mark.asyncio
async def test_db_integrity_after_success(booking_use_case_factory, db_session):
    booking_use_case = await booking_use_case_factory()
    payload = BookingRequestCreateSchema(
        slot_id=SLOT_ID,
        service_id=SERVICE_ID,
        client_name="Integrity Check",
        client_phone="79112223344",
    )

    result = await booking_use_case.execute(payload, MASTER_ID)

    db_session.expire_all()

    slot = await db_session.get(BookingSlotModel, UUID(SLOT_ID))
    assert slot.status == "booked"

    appointment = await db_session.get(AppointmentModel, result.appointment_id)
    assert appointment.status == "scheduled"
    assert appointment.visit_at == slot.starts_at

    request = await db_session.get(BookingRequestModel, result.request_id)
    assert request.appointment_id == appointment.id
