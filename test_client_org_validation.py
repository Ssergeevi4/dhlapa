import pytest
from types import SimpleNamespace
import uuid

from use_cases.client import CreateClientUseCase
from exceptions import OrganizationNotFoundError


class FakeClientDAO:
    def __init__(self):
        self._session = SimpleNamespace()
        self.created = False

    async def get_by_phone(self, phone, org_id):
        return None

    async def create(self, data, org_id, owner_user_id):
        self.created = True
        return SimpleNamespace(**data, id=uuid.uuid4())


@pytest.mark.asyncio
async def test_org_missing_raises():
    dao = FakeClientDAO()
    use_case = CreateClientUseCase(dao)

    payload = SimpleNamespace(phone='79991234567')
    payload.model_dump = lambda exclude_unset=False: {"phone": payload.phone}
    missing_org = uuid.uuid4()

    # Monkeypatch OrganizationDAO.get_by_id to return None
    from db.daos.organization import OrganizationDAO

    async def fake_get_by_id(self, org_id):
        return None

    OrganizationDAO.get_by_id = fake_get_by_id

    with pytest.raises(OrganizationNotFoundError):
        await use_case.execute(payload, missing_org, uuid.uuid4())


@pytest.mark.asyncio
async def test_org_exists_allows_create(monkeypatch):
    dao = FakeClientDAO()
    use_case = CreateClientUseCase(dao)

    payload = SimpleNamespace(phone='79991234567')
    payload.model_dump = lambda exclude_unset=False: {"phone": payload.phone}
    org_id = uuid.uuid4()

    # Monkeypatch OrganizationDAO.get_by_id to return a truthy object
    from db.daos.organization import OrganizationDAO

    async def fake_get_by_id(self, org_id):
        return SimpleNamespace(id=org_id)

    monkeypatch.setattr(OrganizationDAO, 'get_by_id', fake_get_by_id)

    res = await use_case.execute(payload, org_id, uuid.uuid4())
    assert dao.created is True


