import datetime
from uuid import uuid4

import pytest

import use_cases.media as media_use_cases
from db.daos import MediaDAO, MediaDownloadDAO
from domain.entities.subscription import Plan
from dto.media import MediaDTO
from exceptions.media import MediaAccessDeniedError, MediaExpiredError, MediaNotFoundError, InvalidBindingError
from use_cases.media import DeleteMediaUseCase, GetMediaDownloadIntentUseCase, GetMediaDownloadLinkUseCase


class FakeMediaDAO(MediaDAO):
    def __init__(self, media):
        self.media = media
        self.soft_delete_calls = 0

    async def get_by_id(self, media_id):
        return self.media

    async def soft_delete(self, media_id):
        self.soft_delete_calls += 1
        self.media.deleted_at = datetime.datetime.now(datetime.timezone.utc)
        return True


class FakeMediaDownloadDAO(MediaDownloadDAO):
    def __init__(self):
        self.downloads = []

    async def log_download(self, media_id, user_id, ip_address=None, user_agent=None):
        record = {
            "media_id": media_id,
            "user_id": user_id,
            "ip_address": ip_address,
            "user_agent": user_agent,
        }
        self.downloads.append(record)
        return record


def _freeze_media_datetime(monkeypatch, fixed_now: datetime.datetime) -> None:
    class FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr(media_use_cases.datetime, "datetime", FrozenDateTime)


@pytest.mark.asyncio
async def test_download_link_for_foreign_org_returns_404():
    org_id = uuid4()
    media = MediaDTO(
        id=uuid4(),
        kind="before_photo",
        org_id=uuid4(),
        client_id=None,
        appointment_id=uuid4(),
        creator_id=None,
        storage_key="org/other/appointments/other/before/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        deleted_at=None,
    )
    use_case = GetMediaDownloadLinkUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())

    with pytest.raises(MediaNotFoundError):
        await use_case.execute(media_id=media.id, org_id=org_id, plan=Plan.ACTIVE)


@pytest.mark.asyncio
async def test_download_link_for_soft_deleted_media_returns_404():
    org_id = uuid4()
    media = MediaDTO(
        id=uuid4(),
        kind="before_photo",
        org_id=org_id,
        client_id=None,
        appointment_id=uuid4(),
        creator_id=None,
        storage_key=f"org/{org_id}/appointments/{uuid4()}/before/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        deleted_at=datetime.datetime.now(datetime.timezone.utc),
    )
    use_case = GetMediaDownloadLinkUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())

    with pytest.raises(MediaNotFoundError):
        await use_case.execute(media_id=media.id, org_id=org_id, plan=Plan.ACTIVE)


@pytest.mark.asyncio
async def test_download_link_for_expired_media_returns_410(monkeypatch):
    org_id = uuid4()
    media = MediaDTO(
        id=uuid4(),
        kind="before_photo",
        org_id=org_id,
        client_id=None,
        appointment_id=uuid4(),
        creator_id=None,
        storage_key=f"org/{org_id}/appointments/{uuid4()}/before/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        deleted_at=None,
    )
    use_case = GetMediaDownloadLinkUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())

    monkeypatch.setattr(
        media_use_cases,
        "generate_presigned_get_url",
        lambda *, key, expires_in: {"url": f"https://example.invalid/{key}", "expires_in": expires_in},
    )

    with pytest.raises(MediaExpiredError):
        await use_case.execute(media_id=media.id, org_id=org_id, plan=Plan.ACTIVE)


@pytest.mark.asyncio
async def test_download_intent_for_media_at_exact_expiry_returns_410(monkeypatch):
    org_id = uuid4()
    fixed_now = datetime.datetime(2026, 4, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)
    _freeze_media_datetime(monkeypatch, fixed_now)
    media = MediaDTO(
        id=uuid4(),
        kind="before_photo",
        org_id=org_id,
        client_id=None,
        appointment_id=uuid4(),
        creator_id=uuid4(),
        storage_key=f"org/{org_id}/appointments/{uuid4()}/before/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=fixed_now,
        created_at=fixed_now,
        updated_at=fixed_now,
        deleted_at=None,
    )
    use_case = GetMediaDownloadIntentUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())

    with pytest.raises(MediaExpiredError):
        await use_case.execute(media_id=media.id, org_id=org_id, plan=Plan.ACTIVE, user_id=uuid4())


@pytest.mark.asyncio
async def test_download_link_for_media_at_exact_expiry_returns_410(monkeypatch):
    org_id = uuid4()
    fixed_now = datetime.datetime(2026, 4, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)
    _freeze_media_datetime(monkeypatch, fixed_now)
    media = MediaDTO(
        id=uuid4(),
        kind="before_photo",
        org_id=org_id,
        client_id=None,
        appointment_id=uuid4(),
        creator_id=uuid4(),
        storage_key=f"org/{org_id}/appointments/{uuid4()}/before/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=fixed_now,
        created_at=fixed_now,
        updated_at=fixed_now,
        deleted_at=None,
    )
    use_case = GetMediaDownloadLinkUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())

    with pytest.raises(MediaExpiredError):
        await use_case.execute(media_id=media.id, org_id=org_id, plan=Plan.ACTIVE, user_id=uuid4())


@pytest.mark.asyncio
async def test_download_link_returns_short_url(monkeypatch):
    org_id = uuid4()
    media = MediaDTO(
        id=uuid4(),
        kind="before_photo",
        org_id=org_id,
        client_id=None,
        appointment_id=uuid4(),
        creator_id=None,
        storage_key=f"org/{org_id}/appointments/{uuid4()}/before/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        deleted_at=None,
    )
    use_case = GetMediaDownloadLinkUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())

    monkeypatch.setattr(
        media_use_cases,
        "generate_presigned_get_url",
        lambda *, key, expires_in: {"url": f"https://example.invalid/{key}?sig=short", "expires_in": expires_in},
    )

    result = await use_case.execute(media_id=media.id, org_id=org_id, plan=Plan.ACTIVE)

    assert result["download_url"].startswith("https://example.invalid/")
    assert result["expires_in"] > 0


@pytest.mark.asyncio
async def test_download_link_logs_access_and_respects_ttl(monkeypatch):
    org_id = uuid4()
    user_id = uuid4()
    media = MediaDTO(
        id=uuid4(),
        kind="before_photo",
        org_id=org_id,
        client_id=None,
        appointment_id=uuid4(),
        creator_id=uuid4(),
        storage_key=f"org/{org_id}/appointments/{uuid4()}/before/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        deleted_at=None,
    )
    download_dao = FakeMediaDownloadDAO()
    use_case = GetMediaDownloadLinkUseCase(FakeMediaDAO(media), download_dao)

    monkeypatch.setattr(media_use_cases.settings, "MEDIA_DOWNLOAD_LINK_EXPIRES_SECONDS", 120, raising=False)
    monkeypatch.setattr(
        media_use_cases,
        "generate_presigned_get_url",
        lambda *, key, expires_in: {"url": f"https://example.invalid/{key}?sig=short", "expires_in": expires_in},
    )

    result = await use_case.execute(
        media_id=media.id,
        org_id=org_id,
        plan=Plan.ACTIVE,
        user_id=user_id,
        ip_address="127.0.0.1",
        user_agent="pytest/1.0",
    )

    assert result["expires_in"] == 120
    assert len(download_dao.downloads) == 1
    assert download_dao.downloads[0]["ip_address"] == "127.0.0.1"
    assert download_dao.downloads[0]["user_agent"] == "pytest/1.0"


@pytest.mark.asyncio
async def test_download_link_denied_in_limited_mode_for_org_media():
    org_id = uuid4()
    media = MediaDTO(
        id=uuid4(),
        kind="before_photo",
        org_id=org_id,
        client_id=None,
        appointment_id=uuid4(),
        creator_id=uuid4(),
        storage_key=f"org/{org_id}/appointments/{uuid4()}/before/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        deleted_at=None,
    )
    use_case = GetMediaDownloadLinkUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())

    with pytest.raises(MediaAccessDeniedError):
        await use_case.execute(media_id=media.id, org_id=org_id, plan=Plan.LIMITED, user_id=uuid4())


@pytest.mark.asyncio
async def test_download_link_allows_article_image_in_limited_mode(monkeypatch):
    media = MediaDTO(
        id=uuid4(),
        kind="article_image",
        org_id=None,
        client_id=None,
        appointment_id=None,
        creator_id=uuid4(),
        storage_key=f"article/{uuid4().hex}.jpg",
        preview_storage_key=None,
        file_name="article.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        deleted_at=None,
    )
    download_dao = FakeMediaDownloadDAO()
    use_case = GetMediaDownloadLinkUseCase(FakeMediaDAO(media), download_dao)

    monkeypatch.setattr(media_use_cases.settings, "MEDIA_DOWNLOAD_LIMITED_LINK_EXPIRES_SECONDS", 90, raising=False)
    monkeypatch.setattr(
        media_use_cases,
        "generate_presigned_get_url",
        lambda *, key, expires_in: {"url": f"https://example.invalid/{key}?sig=short", "expires_in": expires_in},
    )

    result = await use_case.execute(
        media_id=media.id,
        org_id=uuid4(),
        plan=Plan.LIMITED,
        user_id=uuid4(),
    )

    assert result["download_url"].startswith("https://example.invalid/article/")
    assert result["expires_in"] == 90
    assert len(download_dao.downloads) == 1


@pytest.mark.asyncio
async def test_download_link_rejects_invalid_ttl_config(monkeypatch):
    org_id = uuid4()
    media = MediaDTO(
        id=uuid4(),
        kind="before_photo",
        org_id=org_id,
        client_id=None,
        appointment_id=uuid4(),
        creator_id=uuid4(),
        storage_key=f"org/{org_id}/appointments/{uuid4()}/before/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        deleted_at=None,
    )
    use_case = GetMediaDownloadLinkUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())

    monkeypatch.setattr(media_use_cases.settings, "MEDIA_DOWNLOAD_LINK_EXPIRES_SECONDS", 0, raising=False)

    with pytest.raises(InvalidBindingError):
        await use_case.execute(media_id=media.id, org_id=org_id, plan=Plan.ACTIVE, user_id=uuid4())


@pytest.mark.asyncio
async def test_delete_media_is_idempotent():
    org_id = uuid4()
    media = MediaDTO(
        id=uuid4(),
        kind="before_photo",
        org_id=org_id,
        client_id=None,
        appointment_id=uuid4(),
        creator_id=None,
        storage_key=f"org/{org_id}/appointments/{uuid4()}/before/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        deleted_at=None,
    )
    dao = FakeMediaDAO(media)
    use_case = DeleteMediaUseCase(dao)

    await use_case.execute(media_id=media.id, org_id=org_id)
    first_deleted_at = media.deleted_at
    assert first_deleted_at is not None
    assert dao.soft_delete_calls == 1

    await use_case.execute(media_id=media.id, org_id=org_id)

    assert media.deleted_at == first_deleted_at
    assert dao.soft_delete_calls == 1


@pytest.mark.asyncio
async def test_delete_media_for_foreign_org_returns_404():
    org_id = uuid4()
    media = MediaDTO(
        id=uuid4(),
        kind="before_photo",
        org_id=uuid4(),
        client_id=None,
        appointment_id=uuid4(),
        creator_id=None,
        storage_key=f"org/{uuid4()}/appointments/{uuid4()}/before/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=123,
        mime_type="image/jpeg",
        expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1),
        created_at=datetime.datetime.now(datetime.timezone.utc),
        updated_at=datetime.datetime.now(datetime.timezone.utc),
        deleted_at=None,
    )
    use_case = DeleteMediaUseCase(FakeMediaDAO(media))

    with pytest.raises(MediaNotFoundError):
        await use_case.execute(media_id=media.id, org_id=org_id)


