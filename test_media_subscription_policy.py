"""
Unit tests for media subscription policy:
- _can_download_media: access rules per Plan
- GetMediaDownloadIntentUseCase / GetMediaDownloadLinkUseCase: plan enforcement
"""
import datetime
from uuid import uuid4

import pytest

import use_cases.media as media_use_cases
from db.daos import MediaDAO, MediaDownloadDAO
from domain.entities.subscription import Plan
from dto.media import MediaDTO
from exceptions.media import MediaAccessDeniedError
from use_cases.media import (
    GetMediaDownloadIntentUseCase,
    GetMediaDownloadLinkUseCase,
    _can_download_media,
)

ORG_ID = uuid4()
USER_ID = uuid4()

_FUTURE = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)

pytestmark = pytest.mark.no_db


# ---------------------------------------------------------------------------
# Fake DAOs
# ---------------------------------------------------------------------------

class FakeMediaDAO(MediaDAO):
    def __init__(self, media):
        self.media = media

    async def get_by_id(self, media_id):
        return self.media


class FakeMediaDownloadDAO(MediaDownloadDAO):
    def __init__(self):
        self.downloads = []

    async def log_download(self, media_id, user_id, ip_address=None, user_agent=None):
        self.downloads.append({"media_id": media_id, "user_id": user_id})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_media(kind="before_photo", org_id=None, creator_id=None):
    if org_id is None:
        org_id = ORG_ID

    if kind == "article_image":
        return MediaDTO(
            id=uuid4(),
            kind="article_image",
            org_id=None,
            client_id=None,
            appointment_id=None,
            creator_id=creator_id,
            storage_key=f"article/{uuid4().hex}.jpg",
            preview_storage_key=None,
            file_name="article.jpg",
            size_bytes=1024,
            mime_type="image/jpeg",
            expires_at=_FUTURE,
            created_at=_FUTURE,
            updated_at=_FUTURE,
            deleted_at=None,
        )

    appointment_id = uuid4()
    folder = {"before_photo": "before", "after_photo": "after"}.get(kind, kind)
    return MediaDTO(
        id=uuid4(),
        kind=kind,
        org_id=org_id,
        client_id=None,
        appointment_id=appointment_id,
        creator_id=creator_id,
        storage_key=f"org/{org_id}/appointments/{appointment_id}/{folder}/file.jpg",
        preview_storage_key=None,
        file_name="file.jpg",
        size_bytes=1024,
        mime_type="image/jpeg",
        expires_at=_FUTURE,
        created_at=_FUTURE,
        updated_at=_FUTURE,
        deleted_at=None,
    )


def _fake_presigned(*, key, expires_in):
    return {"url": f"https://s3.example/{key}", "expires_in": expires_in}


# ---------------------------------------------------------------------------
# Tests: _can_download_media
# ---------------------------------------------------------------------------

def test_article_image_allowed_for_limited_plan():
    media = _make_media(kind="article_image")
    assert _can_download_media(media, org_id=ORG_ID, user_id=USER_ID, plan=Plan.LIMITED) is True


def test_article_image_allowed_for_trial_plan():
    media = _make_media(kind="article_image")
    assert _can_download_media(media, org_id=ORG_ID, user_id=USER_ID, plan=Plan.TRIAL) is True


def test_own_media_denied_for_limited_plan():
    # creator_id == user_id — bypass must be closed for limited users
    media = _make_media(kind="before_photo", creator_id=USER_ID)
    assert _can_download_media(media, org_id=ORG_ID, user_id=USER_ID, plan=Plan.LIMITED) is False


def test_org_media_denied_for_limited_plan():
    media = _make_media(kind="before_photo", creator_id=uuid4())
    assert _can_download_media(media, org_id=ORG_ID, user_id=USER_ID, plan=Plan.LIMITED) is False


def test_org_media_allowed_for_trial_plan():
    media = _make_media(kind="before_photo")
    assert _can_download_media(media, org_id=ORG_ID, user_id=USER_ID, plan=Plan.TRIAL) is True


def test_org_media_allowed_for_active_plan():
    media = _make_media(kind="before_photo")
    assert _can_download_media(media, org_id=ORG_ID, user_id=USER_ID, plan=Plan.ACTIVE) is True


def test_foreign_org_media_denied_for_trial_plan():
    foreign_org_id = uuid4()
    media = _make_media(kind="before_photo", org_id=foreign_org_id)
    assert _can_download_media(media, org_id=ORG_ID, user_id=USER_ID, plan=Plan.TRIAL) is False


# ---------------------------------------------------------------------------
# Tests: GetMediaDownloadIntentUseCase
# ---------------------------------------------------------------------------

async def test_intent_article_image_allowed_for_limited_plan(monkeypatch):
    media = _make_media(kind="article_image")
    monkeypatch.setattr(media_use_cases, "generate_presigned_get_url", _fake_presigned)

    use_case = GetMediaDownloadIntentUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())
    result = await use_case.execute(media_id=media.id, org_id=ORG_ID, plan=Plan.LIMITED, user_id=USER_ID)

    assert "get_url" in result
    assert result["get_url"].startswith("https://s3.example/article/")


async def test_intent_before_photo_denied_for_limited_plan():
    media = _make_media(kind="before_photo")
    use_case = GetMediaDownloadIntentUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())

    with pytest.raises(MediaAccessDeniedError):
        await use_case.execute(media_id=media.id, org_id=ORG_ID, plan=Plan.LIMITED, user_id=USER_ID)


async def test_intent_before_photo_allowed_for_trial_plan(monkeypatch):
    media = _make_media(kind="before_photo")
    monkeypatch.setattr(media_use_cases, "generate_presigned_get_url", _fake_presigned)

    use_case = GetMediaDownloadIntentUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())
    result = await use_case.execute(media_id=media.id, org_id=ORG_ID, plan=Plan.TRIAL, user_id=USER_ID)

    assert "get_url" in result


# ---------------------------------------------------------------------------
# Tests: GetMediaDownloadLinkUseCase
# ---------------------------------------------------------------------------

async def test_link_before_photo_denied_for_limited_plan():
    media = _make_media(kind="before_photo")
    use_case = GetMediaDownloadLinkUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())

    with pytest.raises(MediaAccessDeniedError):
        await use_case.execute(media_id=media.id, org_id=ORG_ID, plan=Plan.LIMITED, user_id=USER_ID)


async def test_link_before_photo_allowed_for_active_plan(monkeypatch):
    media = _make_media(kind="before_photo")
    monkeypatch.setattr(media_use_cases, "generate_presigned_get_url", _fake_presigned)

    use_case = GetMediaDownloadLinkUseCase(FakeMediaDAO(media), FakeMediaDownloadDAO())
    result = await use_case.execute(media_id=media.id, org_id=ORG_ID, plan=Plan.ACTIVE, user_id=USER_ID)

    assert "download_url" in result
    assert result["download_url"].startswith("https://s3.example/")
