import pytest
from types import SimpleNamespace
from uuid import uuid4
import datetime

from use_cases.media import CreateMediaUseCase
from exceptions.media import InvalidBindingError, MediaAccessDeniedError
from dto.media import MediaDTO


class FakeMediaDAO:
    def __init__(self, *, existing=None, client_ok=True, appointment_ok=True):
        self.existing = existing
        self.client_ok = client_ok
        self.appointment_ok = appointment_ok
        self.created = []

    async def get_by_storage_key(self, storage_key: str):
        return self.existing

    async def create(self, data: dict):
        m = SimpleNamespace(**{**data, "id": uuid4(), "created_at": datetime.datetime.now(datetime.timezone.utc), "updated_at": datetime.datetime.now(datetime.timezone.utc)})
        self.created.append(m)
        return MediaDTO.model_validate(m)

    async def client_belongs_to_org(self, client_id, org_id):
        return self.client_ok

    async def appointment_belongs_to_org(self, appointment_id, org_id):
        return self.appointment_ok


@pytest.mark.asyncio
async def test_article_image_allows_org_null():
    dao = FakeMediaDAO()
    use_case = CreateMediaUseCase(dao)
    payload = SimpleNamespace(
        kind="article_image",
        storage_key="article/abcd.jpg",
        file_name="img.jpg",
        mime_type="image/jpeg",
        size_bytes=123,
        client_id=None,
        appointment_id=None,
    )
    media = await use_case.execute(payload=payload, org_id=None)
    assert media.org_id is None
    assert media.storage_key == "article/abcd.jpg"


@pytest.mark.asyncio
async def test_foreign_appointment_rejected():
    dao = FakeMediaDAO(appointment_ok=False)
    use_case = CreateMediaUseCase(dao)
    org_id = uuid4()
    appointment_id = uuid4()
    payload = SimpleNamespace(
        kind="before_photo",
        storage_key=f"org/{org_id}/appointments/{appointment_id}/before/{uuid4().hex}.jpg",
        file_name="img.jpg",
        mime_type="image/jpeg",
        size_bytes=10,
        client_id=None,
        appointment_id=appointment_id,
    )
    with pytest.raises(MediaAccessDeniedError):
        await use_case.execute(payload=payload, org_id=org_id)


@pytest.mark.asyncio
async def test_idempotent_returns_existing():
    org_id = uuid4()
    appointment_id = uuid4()
    existing = SimpleNamespace(
        id=uuid4(),
        kind="before_photo",
        org_id=org_id,
        client_id=None,
        appointment_id=appointment_id,
        storage_key=f"org/{org_id}/appointments/{appointment_id}/before/{uuid4().hex}.jpg",
        file_name="img.jpg",
        size_bytes=10,
        mime_type="image/jpeg",
        expires_at=datetime.datetime.now(datetime.timezone.utc),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
    )
    dao = FakeMediaDAO(existing=MediaDTO.model_validate(existing))
    use_case = CreateMediaUseCase(dao)
    payload = SimpleNamespace(
        kind="before_photo",
        storage_key=existing.storage_key,
        file_name=existing.file_name,
        mime_type=existing.mime_type,
        size_bytes=existing.size_bytes,
        client_id=None,
        appointment_id=existing.appointment_id,
    )
    media = await use_case.execute(payload=payload, org_id=existing.org_id)
    assert media.id == existing.id


@pytest.mark.asyncio
async def test_attachment_with_client_binding_accepted():
    dao = FakeMediaDAO()
    use_case = CreateMediaUseCase(dao)
    org_id = uuid4()
    client_id = uuid4()
    payload = SimpleNamespace(
        kind="attachment",
        storage_key=f"org/{org_id}/clients/{client_id}/attachment/{uuid4().hex}.pdf",
        file_name="attachment.pdf",
        mime_type="application/pdf",
        size_bytes=321,
        client_id=client_id,
        appointment_id=None,
    )

    media = await use_case.execute(payload=payload, org_id=org_id)

    assert media.org_id == org_id
    assert media.client_id == client_id
