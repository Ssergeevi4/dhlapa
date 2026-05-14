import pytest
from types import SimpleNamespace
from uuid import uuid4

import use_cases.media as media_use_cases
from db.daos import MediaDAO
from use_cases.media import CreateMediaUploadIntentUseCase
from exceptions.media import InvalidBindingError, MediaAccessDeniedError


class FakeMediaDAO(MediaDAO):
    def __init__(self, *, client_ok=True, appointment_ok=True):
        self.client_ok = client_ok
        self.appointment_ok = appointment_ok

    async def client_belongs_to_org(self, client_id, org_id):
        return self.client_ok

    async def appointment_belongs_to_org(self, appointment_id, org_id):
        return self.appointment_ok


@pytest.mark.asyncio
async def test_invalid_mime_type_rejected():
    dao = FakeMediaDAO()
    use_case = CreateMediaUploadIntentUseCase(dao)
    payload = SimpleNamespace(
        kind="before_photo",
        file_name="img.jpg",
        mime_type="application/octet-stream",
        size_bytes=123,
        client_id=None,
        appointment_id=uuid4(),
    )
    with pytest.raises(InvalidBindingError):
        await use_case.execute(payload=payload, org_id=uuid4())


@pytest.mark.asyncio
async def test_size_exceeds_rejected():
    dao = FakeMediaDAO()
    use_case = CreateMediaUploadIntentUseCase(dao)
    payload = SimpleNamespace(
        kind="before_photo",
        file_name="img.jpg",
        mime_type="image/jpeg",
        size_bytes=50 * 1024 * 1024,  # 50MB
        client_id=None,
        appointment_id=uuid4(),
    )
    with pytest.raises(InvalidBindingError):
        await use_case.execute(payload=payload, org_id=uuid4())


@pytest.mark.asyncio
async def test_foreign_appointment_rejected():
    dao = FakeMediaDAO(appointment_ok=False)
    use_case = CreateMediaUploadIntentUseCase(dao)
    payload = SimpleNamespace(
        kind="before_photo",
        file_name="img.jpg",
        mime_type="image/jpeg",
        size_bytes=123,
        client_id=None,
        appointment_id=uuid4(),
    )
    with pytest.raises(MediaAccessDeniedError):
        await use_case.execute(payload=payload, org_id=uuid4())


@pytest.mark.asyncio
async def test_successful_intent_returns_structure(monkeypatch):
    dao = FakeMediaDAO()
    use_case = CreateMediaUploadIntentUseCase(dao)
    org_id = uuid4()
    appt = uuid4()
    captured = {}

    def fake_generate_presigned_put_url(*, key, content_type, expires_in):
        captured.update(key=key, content_type=content_type, expires_in=expires_in)
        return {
            "url": f"https://example.invalid/{key}",
            "headers": {"Content-Type": content_type},
            "expires_in": expires_in,
        }

    monkeypatch.setattr(media_use_cases, "generate_presigned_put_url", fake_generate_presigned_put_url)
    payload = SimpleNamespace(
        kind="before_photo",
        file_name="IMG_1.jpg",
        mime_type="image/jpeg",
        size_bytes=12345,
        client_id=None,
        appointment_id=appt,
    )
    res = await use_case.execute(payload=payload, org_id=org_id)
    assert "storage_key" in res
    assert res["storage_key"].startswith(f"org/{org_id}/appointments/{appt}/before/")
    assert res["put_url"] == f"https://example.invalid/{res['storage_key']}"
    assert res["put_headers"]["Content-Type"] == "image/jpeg"
    assert res["expires_in"] == 900
    assert captured == {
        "key": res["storage_key"],
        "content_type": "image/jpeg",
        "expires_in": 900,
    }


@pytest.mark.asyncio
async def test_article_image_intent_uses_global_prefix(monkeypatch):
    dao = FakeMediaDAO()
    use_case = CreateMediaUploadIntentUseCase(dao)
    org_id = uuid4()
    monkeypatch.setattr(
        media_use_cases,
        "generate_presigned_put_url",
        lambda **kwargs: {
            "url": f"https://example.invalid/{kwargs['key']}",
            "headers": {"Content-Type": kwargs["content_type"]},
            "expires_in": kwargs["expires_in"],
        },
    )
    payload = SimpleNamespace(
        kind="article_image",
        file_name="article.jpg",
        mime_type="image/jpeg",
        size_bytes=12345,
        client_id=None,
        appointment_id=None,
    )
    res = await use_case.execute(payload=payload, org_id=org_id)
    assert res["storage_key"].startswith("article/")


