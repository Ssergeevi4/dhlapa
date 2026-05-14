from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dto.admin_masters import AdminMasterListDTO, ListMastersForSupportQueryDTO
from use_cases.admin_masters import ListMastersForSupportUseCase


class FakeUserDAO:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def list_for_support(self, **kwargs):
        self.calls.append(kwargs)
        return self.rows, len(self.rows)


def _user(**overrides):
    now = datetime.utcnow()
    values = {
        "id": uuid4(),
        "org_id": uuid4(),
        "full_name": "Support Master",
        "email": "master@example.com",
        "phone": "79000000000",
        "status": "active",
        "last_seen_at": now,
        "trial_ends_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _organization(org_id):
    return SimpleNamespace(id=org_id, name="Support Clinic")


@pytest.mark.asyncio
async def test_list_masters_use_case_forwards_filters_and_maps_trial_plan():
    now = datetime.utcnow()
    user = _user(trial_ends_at=now + timedelta(days=3))
    dao = FakeUserDAO([(user, _organization(user.org_id), None)])
    use_case = ListMastersForSupportUseCase(dao)

    result = await use_case.execute(
        ListMastersForSupportQueryDTO(
            email="master",
            phone="0000",
            org_id=user.org_id,
            org="Support",
            status="active",
            has_activity=True,
            page=2,
            size=5,
            sort_by="email",
            sort_order="asc",
        )
    )

    assert isinstance(result, AdminMasterListDTO)
    assert dao.calls[0]["email"] == "master"
    assert dao.calls[0]["org_id"] == user.org_id
    assert dao.calls[0]["has_activity"] is True
    assert result.total == 1
    assert result.page == 2
    assert result.size == 5
    assert result.items[0].subscription_plan == "trial"
    assert result.items[0].email == "master@example.com"


@pytest.mark.asyncio
async def test_list_masters_use_case_maps_active_and_limited_plans():
    now = datetime.utcnow()
    active_user = _user(email="active@example.com")
    limited_user = _user(email="limited@example.com")
    subscription = SimpleNamespace(
        status="active",
        current_period_end=now + timedelta(days=10),
        auto_renew=True,
    )
    expired_subscription = SimpleNamespace(
        status="expired",
        current_period_end=now - timedelta(days=1),
        auto_renew=False,
    )
    dao = FakeUserDAO(
        [
            (active_user, _organization(active_user.org_id), subscription),
            (limited_user, _organization(limited_user.org_id), expired_subscription),
        ]
    )
    use_case = ListMastersForSupportUseCase(dao)

    result = await use_case.execute(ListMastersForSupportQueryDTO())

    assert [item.subscription_plan for item in result.items] == ["active", "limited"]
    assert result.items[0].subscription_status == "active"
    assert result.items[0].subscription_auto_renew is True
