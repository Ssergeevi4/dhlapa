"""Unit-тесты для subscription policy.

Паттерн: Fake DAO (in-memory), без обращения к БД.
Покрывает:
  Unit-1: apply_mask — поля nulled, masked_keys корректны.
  Unit-2: Subscription.get_plan — TRIAL / ACTIVE / LIMITED.
  Unit-3: Subscription.get_entitlements — can_write и limits.
  Unit-4: require_active_subscription — trial/active проходят, limited → 403.
  Unit-5: require_client_write_allowed — логика cap.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from domain.entities.subscription import Entitlements, Plan, Subscription
from dto.auth import CurrentUserDTO
from services.subscription_policy import (
    APPOINTMENT_MASK_FIELDS,
    CLIENT_MASK_FIELDS,
    apply_mask,
)


# ─── константы ───────────────────────────────────────────────
USER_ID = uuid.uuid4()
ORG_ID = uuid.uuid4()
SESSION_ID = uuid.uuid4()


# ─── Fake DAO-и ──────────────────────────────────────────────
class FakeClientDAO:
    def __init__(self, count: int = 0):
        self._count = count

    async def count_active(self, org_id):
        return self._count


# ─── простой fake для Request ─────────────────────────────────
class FakeRequest:
    class _URL:
        def __init__(self, path):
            self.path = path

    def __init__(self, path: str = "/api/v1/clients/"):
        self.url = self._URL(path)


# ─── хелперы ─────────────────────────────────────────────────
def make_current_user() -> CurrentUserDTO:
    return CurrentUserDTO(
        id=USER_ID,
        session_id=SESSION_ID,
        email="test@example.com",
        phone=None,
        full_name="Test Master",
        org_id=ORG_ID,
        status="active",
    )


def make_subscription(*, trial_ends_at=None, current_period_end=None) -> Subscription:
    now = datetime.utcnow()
    return Subscription(
        user_id=USER_ID,
        current_period_start=now - timedelta(days=1),
        current_period_end=current_period_end or (now - timedelta(seconds=1)),
        trial_ends_at=trial_ends_at,
    )


def trial_entitlements() -> Entitlements:
    return Entitlements(plan=Plan.TRIAL, can_write=True)


def active_entitlements() -> Entitlements:
    return Entitlements(plan=Plan.ACTIVE, can_write=True)


def limited_entitlements() -> Entitlements:
    return Entitlements(
        plan=Plan.LIMITED,
        can_write=False,
        limits={"max_clients": 5, "max_media_per_client": 2},
    )


# ─── Unit-1: apply_mask ──────────────────────────────────────
def test_apply_mask_listed_fields_nulled():
    data = {"diagnoses": "something", "full_name": "Ivan", "allergies": "none"}
    result, keys = apply_mask(data, ["diagnoses", "allergies"])

    assert result["diagnoses"] is None
    assert result["allergies"] is None
    assert result["full_name"] == "Ivan"
    assert set(keys) == {"diagnoses", "allergies"}


def test_apply_mask_does_not_mutate_original():
    data = {"diagnoses": "original"}
    apply_mask(data, ["diagnoses"])
    assert data["diagnoses"] == "original"


def test_apply_mask_absent_field_skipped():
    data = {"full_name": "Ivan"}
    result, keys = apply_mask(data, ["diagnoses"])
    assert "diagnoses" not in result
    assert keys == []


def test_apply_mask_empty_mask_list():
    data = {"diagnoses": "value", "full_name": "Ivan"}
    result, keys = apply_mask(data, [])
    assert result == data
    assert keys == []


def test_apply_mask_all_client_fields():
    data = {f: "value" for f in CLIENT_MASK_FIELDS}
    data["full_name"] = "Ivan"
    result, keys = apply_mask(data, CLIENT_MASK_FIELDS)

    for field in CLIENT_MASK_FIELDS:
        assert result[field] is None
    assert result["full_name"] == "Ivan"
    assert set(keys) == set(CLIENT_MASK_FIELDS)


def test_apply_mask_all_appointment_fields():
    data = {f: "value" for f in APPOINTMENT_MASK_FIELDS}
    result, keys = apply_mask(data, APPOINTMENT_MASK_FIELDS)

    for field in APPOINTMENT_MASK_FIELDS:
        assert result[field] is None
    assert set(keys) == set(APPOINTMENT_MASK_FIELDS)


# ─── Unit-2: Subscription.get_plan ───────────────────────────
def test_get_plan_trial_when_trial_in_future():
    sub = make_subscription(trial_ends_at=datetime.utcnow() + timedelta(days=29))
    assert sub.get_plan(datetime.utcnow()) == Plan.TRIAL


def test_get_plan_active_when_trial_expired_period_alive():
    sub = make_subscription(
        trial_ends_at=datetime.utcnow() - timedelta(days=1),
        current_period_end=datetime.utcnow() + timedelta(days=30),
    )
    assert sub.get_plan(datetime.utcnow()) == Plan.ACTIVE


def test_get_plan_limited_when_both_expired():
    sub = make_subscription(
        trial_ends_at=datetime.utcnow() - timedelta(days=5),
        current_period_end=datetime.utcnow() - timedelta(days=1),
    )
    assert sub.get_plan(datetime.utcnow()) == Plan.LIMITED


def test_get_plan_limited_no_trial_period_expired():
    sub = make_subscription(
        trial_ends_at=None,
        current_period_end=datetime.utcnow() - timedelta(days=1),
    )
    assert sub.get_plan(datetime.utcnow()) == Plan.LIMITED


# ─── Unit-3: Subscription.get_entitlements ───────────────────
def test_entitlements_trial_can_write_no_limits():
    sub = make_subscription(trial_ends_at=datetime.utcnow() + timedelta(days=29))
    ent = sub.get_entitlements(datetime.utcnow())

    assert ent.plan == Plan.TRIAL
    assert ent.can_write is True
    assert ent.limits is None


def test_entitlements_active_can_write_no_limits():
    sub = make_subscription(
        trial_ends_at=datetime.utcnow() - timedelta(days=1),
        current_period_end=datetime.utcnow() + timedelta(days=30),
    )
    ent = sub.get_entitlements(datetime.utcnow())

    assert ent.plan == Plan.ACTIVE
    assert ent.can_write is True
    assert ent.limits is None


def test_entitlements_limited_cannot_write_has_limits():
    sub = make_subscription(
        trial_ends_at=datetime.utcnow() - timedelta(days=5),
        current_period_end=datetime.utcnow() - timedelta(days=1),
    )
    ent = sub.get_entitlements(datetime.utcnow())

    assert ent.plan == Plan.LIMITED
    assert ent.can_write is False
    assert ent.limits == {"max_clients": 5, "max_media_per_client": 2}


# ─── Unit-4: require_active_subscription ─────────────────────
@pytest.mark.asyncio
async def test_require_active_subscription_trial_passes():
    from api.v1.dependencies.subscription.guards import require_active_subscription

    await require_active_subscription(
        request=FakeRequest(),
        current_user=make_current_user(),
        entitlements=trial_entitlements(),
    )  # не должно бросать исключение


@pytest.mark.asyncio
async def test_require_active_subscription_active_passes():
    from api.v1.dependencies.subscription.guards import require_active_subscription

    await require_active_subscription(
        request=FakeRequest(),
        current_user=make_current_user(),
        entitlements=active_entitlements(),
    )


@pytest.mark.asyncio
async def test_require_active_subscription_limited_raises_403():
    from api.v1.dependencies.subscription.guards import require_active_subscription

    with pytest.raises(HTTPException) as exc_info:
        await require_active_subscription(
            request=FakeRequest(),
            current_user=make_current_user(),
            entitlements=limited_entitlements(),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error_code"] == "SUBSCRIPTION_REQUIRED"


# ─── Unit-5: require_client_write_allowed ────────────────────
@pytest.mark.asyncio
async def test_require_client_write_trial_passes():
    from api.v1.dependencies.subscription.guards import require_client_write_allowed

    await require_client_write_allowed(
        request=FakeRequest(),
        entitlements=trial_entitlements(),
        current_user=make_current_user(),
        client_dao=FakeClientDAO(count=100),
    )


@pytest.mark.asyncio
async def test_require_client_write_active_passes():
    from api.v1.dependencies.subscription.guards import require_client_write_allowed

    await require_client_write_allowed(
        request=FakeRequest(),
        entitlements=active_entitlements(),
        current_user=make_current_user(),
        client_dao=FakeClientDAO(count=100),
    )


@pytest.mark.asyncio
async def test_require_client_write_limited_below_cap_passes():
    from api.v1.dependencies.subscription.guards import require_client_write_allowed

    await require_client_write_allowed(
        request=FakeRequest(),
        entitlements=limited_entitlements(),
        current_user=make_current_user(),
        client_dao=FakeClientDAO(count=4),
    )


@pytest.mark.asyncio
async def test_require_client_write_limited_at_cap_raises():
    from api.v1.dependencies.subscription.guards import require_client_write_allowed

    with pytest.raises(HTTPException) as exc_info:
        await require_client_write_allowed(
            request=FakeRequest(),
            entitlements=limited_entitlements(),
            current_user=make_current_user(),
            client_dao=FakeClientDAO(count=5),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error_code"] == "CLIENT_LIMIT_REACHED"


@pytest.mark.asyncio
async def test_require_client_write_limited_above_cap_raises():
    from api.v1.dependencies.subscription.guards import require_client_write_allowed

    with pytest.raises(HTTPException) as exc_info:
        await require_client_write_allowed(
            request=FakeRequest(),
            entitlements=limited_entitlements(),
            current_user=make_current_user(),
            client_dao=FakeClientDAO(count=10),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error_code"] == "CLIENT_LIMIT_REACHED"
