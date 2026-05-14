"""Integration-тесты для ProcessBookingRequestActionUseCase и ListBookingRequestsUseCase.

Покрывает:
  1. pending → approve  (happy path)
  2. pending → decline  (happy path)
  3. approved → cancel  (happy path)
  4. tenant isolation   (чужой мастер → NotFound)
  5. повторное действие (финальный статус → InvalidTransition)
  6. slot conflict      (слот в неожиданном статусе → SlotConflictError)
  7. GET list фильтруется по текущему мастеру
"""
import uuid
import datetime

import pytest
from sqlalchemy import select, update

from db.models.booking_request import BookingRequestModel
from db.models.booking_slot import BookingSlotModel
from db.models.appointment import AppointmentModel
from db.daos import BookingRequestDAO
from use_cases.booking_request import ListBookingRequestsUseCase
from exceptions.booking_request import (
    BookingRequestInvalidTransitionError,
    BookingRequestNotFoundError,
)
from exceptions.booking_slot import BookingSlotConflictError
from tests.fixtures.usecase.booking_request import (
    MASTER_ID,
    ANOTHER_MASTER_ID,
    ORG_ID,
    SLOT_ID,
    ANOTHER_SLOT_ID,
    SERVICE_ID,
)


# ── 1. pending → approve ─────────────────────────────────────

@pytest.mark.asyncio
async def test_approve_pending_request(
    process_action_use_case_factory,
    pending_booking_request,
    db_session,
):
    uc = await process_action_use_case_factory()
    request = pending_booking_request

    result = await uc.execute(
        request_id=request.id,
        action="approve",
        org_id=ORG_ID,
        master_id=MASTER_ID,
    )

    assert result.status == "approved"
    assert result.appointment_id is not None

    # Проверяем состояние в БД
    db_session.expire_all()

    slot = await db_session.get(BookingSlotModel, SLOT_ID)
    assert slot.status == "booked"

    appointment = await db_session.get(AppointmentModel, result.appointment_id)
    assert appointment is not None
    assert appointment.status == "scheduled"
    assert appointment.visit_at == slot.starts_at
    assert appointment.procedure_service_id == SERVICE_ID


# ── 2. pending → decline ─────────────────────────────────────

@pytest.mark.asyncio
async def test_decline_pending_request(
    process_action_use_case_factory,
    pending_booking_request,
    db_session,
):
    uc = await process_action_use_case_factory()
    request = pending_booking_request
    reason = "Мастер в отпуске"

    result = await uc.execute(
        request_id=request.id,
        action="decline",
        org_id=ORG_ID,
        master_id=MASTER_ID,
        reason=reason,
    )

    assert result.status == "declined"
    assert result.appointment_id is None

    # Проверяем состояние в БД
    request_id = request.id
    db_session.expire_all()

    slot = await db_session.get(BookingSlotModel, SLOT_ID)
    assert slot.status == "free"
    assert slot.reserved_until is None

    db_request = await db_session.get(BookingRequestModel, request_id)
    assert db_request.status == "declined"
    assert db_request.decline_reason == reason


# ── 3. approved → cancel ─────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_approved_request(
    process_action_use_case_factory,
    approved_booking_request,
    db_session,
):
    uc = await process_action_use_case_factory()
    request = approved_booking_request

    result = await uc.execute(
        request_id=request.id,
        action="cancel",
        org_id=ORG_ID,
        master_id=MASTER_ID,
    )

    assert result.status == "cancelled"

    request_id = request.id
    appointment_id = request.appointment_id
    # Проверяем состояние в БД
    db_session.expire_all()

    slot = await db_session.get(BookingSlotModel, SLOT_ID)
    assert slot.status == "free"

    appointment = await db_session.get(AppointmentModel, appointment_id)
    assert appointment.status == "canceled"

    db_request = await db_session.get(BookingRequestModel, request_id)
    assert db_request.status == "cancelled"


# ── 4. Tenant isolation ──────────────────────────────────────

@pytest.mark.asyncio
async def test_foreign_master_gets_not_found(
    process_action_use_case_factory,
    pending_booking_request,
):
    uc = await process_action_use_case_factory()
    request = pending_booking_request
    foreign_master = uuid.uuid4()

    with pytest.raises(BookingRequestNotFoundError):
        await uc.execute(
            request_id=request.id,
            action="approve",
            org_id=ORG_ID,
            master_id=foreign_master,
        )


@pytest.mark.asyncio
async def test_foreign_org_gets_not_found(
    process_action_use_case_factory,
    pending_booking_request,
):
    uc = await process_action_use_case_factory()
    request = pending_booking_request
    foreign_org = uuid.uuid4()

    with pytest.raises(BookingRequestNotFoundError):
        await uc.execute(
            request_id=request.id,
            action="approve",
            org_id=foreign_org,
            master_id=MASTER_ID,
        )


# ── 5. Повторное действие на финальном статусе ───────────────

@pytest.mark.asyncio
async def test_double_approve_raises_invalid_transition(
    process_action_use_case_factory,
    pending_booking_request,
):
    uc = await process_action_use_case_factory()
    request = pending_booking_request

    # Первый approve
    await uc.execute(
        request_id=request.id,
        action="approve",
        org_id=ORG_ID,
        master_id=MASTER_ID,
    )

    # Второй approve — уже approved, переход невалиден
    uc2 = await process_action_use_case_factory()
    with pytest.raises(BookingRequestInvalidTransitionError):
        await uc2.execute(
            request_id=request.id,
            action="approve",
            org_id=ORG_ID,
            master_id=MASTER_ID,
        )


@pytest.mark.asyncio
async def test_decline_after_decline_raises(
    process_action_use_case_factory,
    pending_booking_request,
):
    uc = await process_action_use_case_factory()
    request = pending_booking_request

    await uc.execute(
        request_id=request.id,
        action="decline",
        org_id=ORG_ID,
        master_id=MASTER_ID,
    )

    uc2 = await process_action_use_case_factory()
    with pytest.raises(BookingRequestInvalidTransitionError):
        await uc2.execute(
            request_id=request.id,
            action="decline",
            org_id=ORG_ID,
            master_id=MASTER_ID,
        )


# ── 6. Slot conflict ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_approve_with_wrong_slot_status_raises_conflict(
    process_action_use_case_factory,
    pending_booking_request,
    db_session,
):
    """Если слот уже free (а ожидается reserved), approve падает с SlotConflictError."""
    from sqlalchemy import update

    # Искусственно ставим слот в free (нарушая инвариант)
    await db_session.execute(
        update(BookingSlotModel)
        .where(BookingSlotModel.id == SLOT_ID)
        .values(status="free")
    )
    await db_session.commit()

    uc = await process_action_use_case_factory()
    request = pending_booking_request

    with pytest.raises(BookingSlotConflictError):
        await uc.execute(
            request_id=request.id,
            action="approve",
            org_id=ORG_ID,
            master_id=MASTER_ID,
        )


# ── 7. GET list фильтруется по текущему мастеру ──────────────

@pytest.mark.asyncio
async def test_list_returns_only_own_requests(engine, db_session):
    """Два pending-запроса на разных слотах, один от MASTER_ID — list возвращает только свои."""
    now = datetime.datetime.now(datetime.timezone.utc)
    foreign_master = ANOTHER_MASTER_ID

    # Заявка нашего мастера
    own_request = BookingRequestModel(
        id=uuid.uuid4(),
        org_id=ORG_ID,
        master_id=MASTER_ID,
        slot_id=SLOT_ID,
        service_id=SERVICE_ID,
        client_name="Own Client",
        client_phone="79001111111",
        status="pending",
        reserved_until=now + datetime.timedelta(minutes=15),
        public_token=str(uuid.uuid4()),
    )
    # Заявка чужого мастера (ссылается на ANOTHER_SLOT, тоже принадлежит MASTER_ID в дампе,
    # но booking_request.master_id = foreign — list_for_master фильтрует по request.master_id)
    foreign_request = BookingRequestModel(
        id=uuid.uuid4(),
        org_id=ORG_ID,
        master_id=foreign_master,
        slot_id=ANOTHER_SLOT_ID,
        service_id=SERVICE_ID,
        client_name="Foreign Client",
        client_phone="79002222222",
        status="pending",
        reserved_until=now + datetime.timedelta(minutes=15),
        public_token=str(uuid.uuid4()),
    )

    db_session.add_all([own_request, foreign_request])
    await db_session.commit()

    # Вызываем use case
    from sqlalchemy.ext.asyncio import async_sessionmaker
    uc_session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        uc = ListBookingRequestsUseCase(
            _booking_request_dao=BookingRequestDAO(uc_session),
        )
        items, total = await uc.execute(org_id=ORG_ID, master_id=MASTER_ID)

        assert total == 1
        assert items[0].id == own_request.id

        # Чужой мастер видит только свою
        items_foreign, total_foreign = await uc.execute(org_id=ORG_ID, master_id=foreign_master)
        assert total_foreign == 1
        assert items_foreign[0].id == foreign_request.id
    finally:
        await uc_session.close()


@pytest.mark.asyncio
async def test_list_empty_for_unknown_master(engine, db_session):
    """Неизвестный мастер получает пустой список."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    uc_session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        uc = ListBookingRequestsUseCase(
            _booking_request_dao=BookingRequestDAO(uc_session),
        )
        items, total = await uc.execute(org_id=ORG_ID, master_id=uuid.uuid4())
        assert total == 0
        assert items == []
    finally:
        await uc_session.close()


@pytest.mark.asyncio
async def test_list_filters_by_status(engine, pending_booking_request, db_session):
    """Фильтр по status=pending возвращает только pending заявки."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    uc_session = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        uc = ListBookingRequestsUseCase(
            _booking_request_dao=BookingRequestDAO(uc_session),
        )
        items, total = await uc.execute(
            org_id=ORG_ID, master_id=MASTER_ID, status="pending"
        )
        assert total >= 1
        assert all(item.status == "pending" for item in items)

        # declined — ничего (pending_booking_request в статусе pending)
        items_d, total_d = await uc.execute(
            org_id=ORG_ID, master_id=MASTER_ID, status="declined"
        )
        assert total_d == 0
    finally:
        await uc_session.close()
