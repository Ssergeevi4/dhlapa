from types import SimpleNamespace
from uuid import uuid4

import pytest

from dto.admin_master_account import (
    UnblockMasterAccountCommandDTO,
    UnblockMasterAccountResultDTO,
)
from exceptions.user import UserNotFoundError
from use_cases.admin_master_account import UnblockMasterAccountUseCase


class FakeUserDAO:
    def __init__(self, user=None):
        self.user = user
        self.status_updates = []
        self.revoked_user_ids = []

    async def get_by_id(self, user_id):
        if self.user and self.user.id == user_id:
            return self.user
        return None

    async def update_status(self, user_id, status):
        self.status_updates.append({"user_id": user_id, "status": status})
        self.user.status = status

    async def revoke_all_sessions(self, user_id):
        self.revoked_user_ids.append(user_id)


@pytest.mark.asyncio
async def test_unblock_master_account_changes_blocked_user_and_revokes_sessions():
    user = SimpleNamespace(id=uuid4(), status="blocked")
    dao = FakeUserDAO(user)
    use_case = UnblockMasterAccountUseCase(dao)

    result = await use_case.execute(UnblockMasterAccountCommandDTO(user_id=user.id))

    assert isinstance(result, UnblockMasterAccountResultDTO)
    assert result.previous_status == "blocked"
    assert result.status == "active"
    assert result.changed is True
    assert result.sessions_revoked is True
    assert dao.status_updates == [{"user_id": user.id, "status": "active"}]
    assert dao.revoked_user_ids == [user.id]


@pytest.mark.asyncio
async def test_unblock_master_account_is_noop_for_active_user():
    user = SimpleNamespace(id=uuid4(), status="active")
    dao = FakeUserDAO(user)
    use_case = UnblockMasterAccountUseCase(dao)

    result = await use_case.execute(UnblockMasterAccountCommandDTO(user_id=user.id))

    assert result.previous_status == "active"
    assert result.status == "active"
    assert result.changed is False
    assert result.sessions_revoked is False
    assert dao.status_updates == []
    assert dao.revoked_user_ids == []


@pytest.mark.asyncio
async def test_unblock_master_account_raises_for_missing_user():
    use_case = UnblockMasterAccountUseCase(FakeUserDAO())

    with pytest.raises(UserNotFoundError):
        await use_case.execute(UnblockMasterAccountCommandDTO(user_id=uuid4()))
