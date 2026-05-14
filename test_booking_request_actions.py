import uuid
import datetime
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from domain.entities.booking_request import (
    BookingRequestAction,
    BookingRequestStatus,
    resolve_transition,
)
from exceptions.booking_request import (
    BookingRequestInvalidTransitionError,
    BookingRequestNotFoundError,
)
from exceptions.booking_slot import BookingSlotConflictError, BookingSlotNotFoundError
from use_cases.booking_request import ProcessBookingRequestActionUseCase


# ── Happy-path transitions ────────────────────────────────────

def test_pending_approve():
    result = resolve_transition("pending", "approve")
    assert result.new_request_status == BookingRequestStatus.APPROVED
    assert result.slot_rules.expected == "reserved"
    assert result.slot_rules.new == "booked"


def test_pending_decline():
    result = resolve_transition("pending", "decline")
    assert result.new_request_status == BookingRequestStatus.DECLINED
    assert result.slot_rules.expected == "reserved"
    assert result.slot_rules.new == "free"


def test_approved_cancel():
    result = resolve_transition("approved", "cancel")
    assert result.new_request_status == BookingRequestStatus.CANCELLED
    assert result.slot_rules.expected == "booked"
    assert result.slot_rules.new == "free"


# ── Invalid transitions ──────────────────────────────────────

@pytest.mark.parametrize(
    "current_status, action",
    [
        ("approved", "approve"),
        ("declined", "approve"),
        ("declined", "decline"),
        ("cancelled", "cancel"),
        ("cancelled", "approve"),
        ("expired", "approve"),
        ("expired", "decline"),
        ("expired", "cancel"),
        ("pending", "cancel"),
    ],
)
def test_invalid_transition_raises(current_status, action):
    with pytest.raises(BookingRequestInvalidTransitionError):
        resolve_transition(current_status, action)


# ── Fake objects for use-case level unit tests ────────────────

@dataclass
class FakeRequest:
    id: uuid.UUID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    org_id: uuid.UUID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")
    master_id: uuid.UUID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000003")
    slot_id: uuid.UUID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000004")
    service_id: uuid.UUID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000005")
    client_name: str = "Fake Client"
    client_phone: str = "79001234567"
    status: str = "pending"
    appointment_id: uuid.UUID | None = None
    decline_reason: str | None = None


@dataclass
class FakeSlot:
    id: uuid.UUID = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000004")
    status: str = "reserved"
    starts_at: datetime.datetime = datetime.datetime(2026, 4, 20, 10, 0, tzinfo=datetime.timezone.utc)
    reserved_until: datetime.datetime | None = None


def _build_use_case(
    request_dao_return=None,
    slot_dao_return=None,
) -> ProcessBookingRequestActionUseCase:
    booking_request_dao = AsyncMock()
    booking_request_dao.get_and_lock.return_value = request_dao_return

    booking_slot_dao = AsyncMock()
    booking_slot_dao.get_and_lock.return_value = slot_dao_return

    return ProcessBookingRequestActionUseCase(
        _booking_request_dao=booking_request_dao,
        _booking_slot_dao=booking_slot_dao,
        _appointment_dao=AsyncMock(),
        _client_dao=AsyncMock(),
    )


# ── Unit: not found ──────────────────────────────────────────

async def test_request_not_found_raises():
    uc = _build_use_case(request_dao_return=None)

    with pytest.raises(BookingRequestNotFoundError):
        await uc.execute(
            request_id=uuid.uuid4(),
            action="approve",
            org_id=uuid.uuid4(),
            master_id=uuid.uuid4(),
        )


# ── Unit: slot not found ─────────────────────────────────────

async def test_slot_not_found_raises():
    uc = _build_use_case(
        request_dao_return=FakeRequest(),
        slot_dao_return=None,
    )

    with pytest.raises(BookingSlotNotFoundError):
        await uc.execute(
            request_id=FakeRequest.id,
            action="approve",
            org_id=FakeRequest.org_id,
            master_id=FakeRequest.master_id,
        )


# ── Unit: slot conflict ──────────────────────────────────────

async def test_slot_conflict_raises():
    """Слот в status=free, а approve ожидает reserved → SlotConflictError."""
    uc = _build_use_case(
        request_dao_return=FakeRequest(),
        slot_dao_return=FakeSlot(status="free"),
    )

    with pytest.raises(BookingSlotConflictError):
        await uc.execute(
            request_id=FakeRequest.id,
            action="approve",
            org_id=FakeRequest.org_id,
            master_id=FakeRequest.master_id,
        )


async def test_cancel_slot_conflict_raises():
    """Cancel ожидает booked, а слот free → SlotConflictError."""
    request = FakeRequest(status="approved", appointment_id=uuid.uuid4())
    uc = _build_use_case(
        request_dao_return=request,
        slot_dao_return=FakeSlot(status="free"),
    )

    with pytest.raises(BookingSlotConflictError):
        await uc.execute(
            request_id=request.id,
            action="cancel",
            org_id=request.org_id,
            master_id=request.master_id,
        )


# ── Unit: invalid action string ──────────────────────────────

async def test_invalid_action_string_raises():
    uc = _build_use_case(request_dao_return=FakeRequest())

    with pytest.raises(BookingRequestInvalidTransitionError):
        await uc.execute(
            request_id=FakeRequest.id,
            action="destroy",
            org_id=FakeRequest.org_id,
            master_id=FakeRequest.master_id,
        )            