from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dto.admin_clients import AdminClientListDTO, ListAdminClientsQueryDTO
from exceptions import (
    ClientNotFoundError,
    ClientRestoreNotAllowedError,
    ClientRestoreWindowExpiredError,
)
from use_cases.admin_clients import (
    GetAdminClientCardUseCase,
    ListAdminClientsUseCase,
    RestoreAdminClientUseCase,
    SetAdminClientBadFlagUseCase,
)


pytestmark = pytest.mark.no_db


class FakeClientDAO:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    async def list_for_admin(self, **kwargs):
        self.calls.append(kwargs)
        return ([self.row] if self.row else []), 1 if self.row else 0

    async def get_for_admin(self, client_id, *, include_deleted=False):
        self.calls.append({"client_id": client_id, "include_deleted": include_deleted})
        return self.row

    async def restore_for_admin(self, client_id, *, restored_at):
        self.calls.append({"client_id": client_id, "restored_at": restored_at})
        if self.row is None:
            return None
        client, _, _ = self.row
        if client.deleted_at is None:
            return None
        client.deleted_at = None
        client.updated_at = restored_at
        return self.row

    async def set_bad_flag_for_admin(
        self,
        client_id,
        *,
        is_flagged_bad,
        flag_comment,
        updated_at,
    ):
        self.calls.append(
            {
                "client_id": client_id,
                "is_flagged_bad": is_flagged_bad,
                "flag_comment": flag_comment,
                "updated_at": updated_at,
            }
        )
        if self.row is None:
            return None
        client, _, _ = self.row
        client.is_flagged_bad = is_flagged_bad
        client.flag_comment = flag_comment
        client.updated_at = updated_at
        return self.row


def _row(deleted_at=None):
    org_id = uuid4()
    owner_id = uuid4()
    now = datetime.now(timezone.utc)
    client = SimpleNamespace(
        id=uuid4(),
        org_id=org_id,
        owner_user_id=owner_id,
        full_name="Sensitive Client",
        phone="79001234567",
        birth_date=date(1990, 1, 2),
        diagnoses="Diagnosis",
        allergies="Allergy",
        contraindications="Contra",
        notes="Private notes",
        is_flagged_bad=True,
        flag_comment="Private flag",
        created_at=now,
        updated_at=now,
        deleted_at=deleted_at,
    )
    org = SimpleNamespace(id=org_id, name="Global Clinic")
    owner = SimpleNamespace(id=owner_id, full_name="Owner Master", email="owner@example.com")
    return client, org, owner


@pytest.mark.asyncio
async def test_admin_client_list_masks_sensitive_fields_for_support():
    dao = FakeClientDAO(_row())
    use_case = ListAdminClientsUseCase(dao)

    result = await use_case.execute(
        ListAdminClientsQueryDTO(search="Sensitive", page=2, size=5),
        role="TechSupport",
    )

    assert isinstance(result, AdminClientListDTO)
    assert dao.calls[0]["search"] == "Sensitive"
    assert result.page == 2
    assert result.size == 5

    item = result.items[0]
    assert item.phone == "*******4567"
    assert item.birth_date is None
    assert item.diagnoses is None
    assert item.flag_comment is None
    assert item.is_masked is True
    assert set(item.masked_fields) >= {"phone", "diagnoses", "flag_comment"}


@pytest.mark.asyncio
async def test_admin_client_card_masks_sensitive_fields_for_superadmin():
    row = _row()
    client = row[0]
    dao = FakeClientDAO(row)
    use_case = GetAdminClientCardUseCase(dao)

    result = await use_case.execute(client.id, role="SuperAdmin")

    assert result.phone == "*******4567"
    assert result.birth_date is None
    assert result.diagnoses is None
    assert result.flag_comment is None
    assert result.is_masked is True
    assert set(result.masked_fields) >= {"phone", "diagnoses", "flag_comment"}


@pytest.mark.asyncio
async def test_set_admin_client_bad_flag_updates_and_masks_response():
    row = _row()
    client = row[0]
    dao = FakeClientDAO(row)
    use_case = SetAdminClientBadFlagUseCase(dao)

    result = await use_case.execute(
        client.id,
        is_flagged_bad=False,
        flag_comment=None,
        role="TechSupport",
    )

    assert result.is_flagged_bad is False
    assert result.flag_comment is None
    assert dao.calls[-1]["is_flagged_bad"] is False


@pytest.mark.asyncio
async def test_admin_client_card_raises_for_missing_client():
    use_case = GetAdminClientCardUseCase(FakeClientDAO())

    with pytest.raises(ClientNotFoundError):
        await use_case.execute(uuid4(), role="SuperAdmin")


@pytest.mark.asyncio
async def test_restore_admin_client_restores_deleted_client_inside_retention():
    deleted_at = datetime.now(timezone.utc) - timedelta(days=10)
    row = _row(deleted_at=deleted_at)
    client = row[0]
    dao = FakeClientDAO(row)
    use_case = RestoreAdminClientUseCase(dao, retention_days=90)

    result = await use_case.execute(client.id)

    assert result.client.id == client.id
    assert result.client.deleted_at is None
    assert result.deleted_at == deleted_at
    assert result.retention_days == 90
    assert result.retention_expires_at == deleted_at + timedelta(days=90)
    assert dao.calls[0] == {"client_id": client.id, "include_deleted": True}
    assert dao.calls[1]["client_id"] == client.id


@pytest.mark.asyncio
async def test_restore_admin_client_rejects_expired_retention_window():
    deleted_at = datetime.now(timezone.utc) - timedelta(days=91)
    row = _row(deleted_at=deleted_at)
    client = row[0]
    dao = FakeClientDAO(row)
    use_case = RestoreAdminClientUseCase(dao, retention_days=90)

    with pytest.raises(ClientRestoreWindowExpiredError):
        await use_case.execute(client.id)

    assert client.deleted_at == deleted_at
    assert len(dao.calls) == 1


@pytest.mark.asyncio
async def test_restore_admin_client_rejects_active_client():
    row = _row()
    client = row[0]
    use_case = RestoreAdminClientUseCase(FakeClientDAO(row), retention_days=90)

    with pytest.raises(ClientRestoreNotAllowedError):
        await use_case.execute(client.id)
