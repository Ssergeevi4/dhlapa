"""
Интеграционный тест: полный цикл загрузки и скачивания медиа.

Тест демонстрирует:
1. Создание upload intent (получить pre-signed PUT URL)
2. Загрузку файла в S3 через PUT URL
3. Финализацию загрузки в БД
4. Получение download intent (получить pre-signed GET URL)
5. Скачивание файла из S3
"""

import datetime
import pytest
from uuid import uuid4
from types import SimpleNamespace
import use_cases.media as media_use_cases
import services.s3 as s3_service
from db.daos import MediaDAO, MediaDownloadDAO
from domain.entities.subscription import Plan
from use_cases.media import (
    CreateMediaUploadIntentUseCase,
    CreateMediaUseCase,
    GetMediaDownloadIntentUseCase,
)
from exceptions.media import MediaAccessDeniedError, MediaNotFoundError

pytestmark = pytest.mark.no_db


class FakeMediaDAO(MediaDAO):
    """Mock DAO для интеграционного тестирования."""
    
    def __init__(self, *, client_ok=True, appointment_ok=True):
        self.client_ok = client_ok
        self.appointment_ok = appointment_ok
        self.stored_media = {}  # Симуляция хранилища в памяти
    
    async def client_belongs_to_org(self, client_id, org_id):
        return self.client_ok
    
    async def appointment_belongs_to_org(self, appointment_id, org_id):
        return self.appointment_ok
    
    async def get_by_storage_key(self, storage_key: str):
        return self.stored_media.get(storage_key)
    
    async def get_by_id(self, media_id):
        """Получить медиа по ID для скачивания."""
        for media in self.stored_media.values():
            if media.id == media_id:
                return media
        return None
    
    async def create(self, data: dict):
        """Создать запись о медиа в БД."""
        from uuid import uuid4
        media = SimpleNamespace(
            id=uuid4(),
            kind=data['kind'],
            org_id=data['org_id'],
            client_id=data.get('client_id'),
            appointment_id=data.get('appointment_id'),
            creator_id=data.get('creator_id'),
            storage_key=data['storage_key'],
            preview_storage_key=data.get('preview_storage_key'),
            file_name=data.get('file_name'),
            size_bytes=data.get('size_bytes'),
            mime_type=data.get('mime_type'),
            expires_at=data.get('expires_at'),
            created_at=None,
            updated_at=None,
            deleted_at=None,
        )
        self.stored_media[data['storage_key']] = media
        return media


class FakeMediaDownloadDAO(MediaDownloadDAO):
    """Mock DAO для логирования скачиваний."""
    
    def __init__(self):
        self.downloads = []
    
    async def log_download(self, media_id, user_id, ip_address=None, user_agent=None):
        from uuid import uuid4
        download = SimpleNamespace(
            id=uuid4(),
            media_id=media_id,
            user_id=user_id,
            ip_address=ip_address,
            user_agent=user_agent,
            timestamp=None,
        )
        self.downloads.append(download)
        return download


# ============ Тесты ============

@pytest.mark.asyncio
async def test_complete_media_upload_flow(monkeypatch):
    """
    Полный цикл загрузки:
    1. Create upload intent → get PUT URL + storage_key
    2. Finalize upload → create DB record
    3. Get download intent → get GET URL
    """
    # Setup
    org_id = uuid4()
    appointment_id = uuid4()
    user_id = uuid4()
    
    media_dao = FakeMediaDAO()
    media_download_dao = FakeMediaDownloadDAO()
    
    # Шаг 1: Создать upload intent
    create_intent_use_case = CreateMediaUploadIntentUseCase(media_dao)
    
    captured_s3_calls = []
    
    def fake_generate_presigned_put_url(*, key, content_type, expires_in):
        captured_s3_calls.append({
            'action': 'generate_presigned_put_url',
            'key': key,
            'content_type': content_type,
            'expires_in': expires_in,
        })
        return {
            "url": f"https://s3.example.com/bucket/{key}",
            "headers": {"Content-Type": content_type},
            "expires_in": expires_in,
        }
    
    monkeypatch.setattr(media_use_cases, "generate_presigned_put_url", fake_generate_presigned_put_url)
    
    upload_intent_payload = SimpleNamespace(
        kind="before_photo",
        file_name="patient_photo.jpg",
        mime_type="image/jpeg",
        size_bytes=1048576,  # 1 MB
        client_id=None,
        appointment_id=appointment_id,
    )
    
    upload_intent = await create_intent_use_case.execute(
        payload=upload_intent_payload,
        org_id=org_id,
    )
    
    # Проверка upload intent
    assert "storage_key" in upload_intent
    assert upload_intent["storage_key"].startswith(f"org/{org_id}/appointments/{appointment_id}/before/")
    assert "put_url" in upload_intent
    assert upload_intent["put_url"].startswith("https://s3.example.com")
    assert upload_intent["expires_in"] == 900
    storage_key = upload_intent["storage_key"]
    
    # Шаг 2: Финализировать загрузку (подтвердить в БД)
    create_media_use_case = CreateMediaUseCase(media_dao)
    
    finalize_payload = SimpleNamespace(
        kind="before_photo",
        storage_key=storage_key,
        file_name="patient_photo.jpg",
        mime_type="image/jpeg",
        size_bytes=1048576,
        client_id=None,
        appointment_id=appointment_id,
    )
    
    created_media = await create_media_use_case.execute(
        payload=finalize_payload,
        org_id=org_id,
        creator_id=user_id,
    )
    
    # Проверка созданного медиа
    assert created_media.kind == "before_photo"
    assert created_media.org_id == org_id
    assert created_media.appointment_id == appointment_id
    assert created_media.creator_id == user_id
    assert created_media.storage_key == storage_key
    assert created_media.mime_type == "image/jpeg"
    assert created_media.size_bytes == 1048576
    media_id = created_media.id
    
    # Шаг 3: Получить download intent
    get_download_intent_use_case = GetMediaDownloadIntentUseCase(media_dao, media_download_dao)
    
    def fake_generate_presigned_get_url(*, key, expires_in):
        captured_s3_calls.append({
            'action': 'generate_presigned_get_url',
            'key': key,
            'expires_in': expires_in,
        })
        return {
            "url": f"https://s3.example.com/bucket/{key}?X-Amz-Signature=...",
            "expires_in": expires_in,
        }
    
    monkeypatch.setattr(media_use_cases, "generate_presigned_get_url", fake_generate_presigned_get_url)
    
    download_intent = await get_download_intent_use_case.execute(
        media_id=media_id,
        org_id=org_id,
        plan=Plan.ACTIVE,
        user_id=user_id,
        ip_address="192.168.1.1",
        user_agent="Mozilla/5.0",
    )
    
    # Проверка download intent
    assert download_intent["media_id"] == str(media_id)
    assert "get_url" in download_intent
    assert download_intent["get_url"].startswith("https://s3.example.com")
    assert download_intent["expires_in"] == 900
    assert download_intent["file_name"] == "patient_photo.jpg"
    assert download_intent["mime_type"] == "image/jpeg"
    
    # Проверка логирования скачивания
    assert len(media_download_dao.downloads) == 1
    download_log = media_download_dao.downloads[0]
    assert download_log.media_id == media_id
    assert download_log.user_id == user_id
    assert download_log.ip_address == "192.168.1.1"
    assert download_log.user_agent == "Mozilla/5.0"
    
    # Проверка S3 вызовов
    assert len(captured_s3_calls) == 2
    assert captured_s3_calls[0]['action'] == 'generate_presigned_put_url'
    assert captured_s3_calls[0]['content_type'] == 'image/jpeg'
    assert captured_s3_calls[1]['action'] == 'generate_presigned_get_url'


@pytest.mark.asyncio
async def test_upload_attachment_to_client(monkeypatch):
    """Загрузить вложение к клиенту вместо приёма."""
    org_id = uuid4()
    client_id = uuid4()
    user_id = uuid4()
    
    media_dao = FakeMediaDAO()
    create_intent_use_case = CreateMediaUploadIntentUseCase(media_dao)
    
    def fake_generate_presigned_put_url(**kwargs):
        return {
            "url": f"https://s3.example.com/bucket/{kwargs['key']}",
            "headers": {"Content-Type": kwargs["content_type"]},
            "expires_in": kwargs["expires_in"],
        }
    
    monkeypatch.setattr(media_use_cases, "generate_presigned_put_url", fake_generate_presigned_put_url)
    
    payload = SimpleNamespace(
        kind="attachment",
        file_name="contract.pdf",
        mime_type="application/pdf",
        size_bytes=512000,
        client_id=client_id,
        appointment_id=None,
    )
    
    result = await create_intent_use_case.execute(payload=payload, org_id=org_id)
    
    # Проверка, что storage_key содержит client_id
    assert f"org/{org_id}/clients/{client_id}/attachment/" in result["storage_key"]
    assert result["storage_key"].endswith(".pdf")


@pytest.mark.asyncio
async def test_upload_article_image_no_binding(monkeypatch):
    """Загрузить публичное изображение статьи (без привязки)."""
    org_id = uuid4()
    
    media_dao = FakeMediaDAO()
    create_intent_use_case = CreateMediaUploadIntentUseCase(media_dao)
    
    def fake_generate_presigned_put_url(**kwargs):
        return {
            "url": f"https://s3.example.com/bucket/{kwargs['key']}",
            "headers": {"Content-Type": kwargs["content_type"]},
            "expires_in": kwargs["expires_in"],
        }
    
    monkeypatch.setattr(media_use_cases, "generate_presigned_put_url", fake_generate_presigned_put_url)
    
    payload = SimpleNamespace(
        kind="article_image",
        file_name="article.png",
        mime_type="image/png",
        size_bytes=256000,
        client_id=None,
        appointment_id=None,
    )
    
    result = await create_intent_use_case.execute(payload=payload, org_id=org_id)
    
    # Проверка, что storage_key содержит article/ (без org_id)
    assert result["storage_key"].startswith("article/")
    assert result["storage_key"].endswith(".png")


@pytest.mark.asyncio
async def test_download_access_denied_for_foreign_org(monkeypatch):
    """Попытка скачать медиа из другой организации должна быть запрещена при non-matching org."""
    org_id_1 = uuid4()
    org_id_2 = uuid4()
    user_id = uuid4()
    appointment_id = uuid4()
    
    media_dao = FakeMediaDAO()
    media_download_dao = FakeMediaDownloadDAO()
    
    # Создать медиа в org_id_1
    create_media_use_case = CreateMediaUseCase(media_dao)
    
    finalize_payload = SimpleNamespace(
        kind="before_photo",
        storage_key=f"org/{org_id_1}/appointments/{appointment_id}/before/test.jpg",
        file_name="photo.jpg",
        mime_type="image/jpeg",
        size_bytes=1048576,
        client_id=None,
        appointment_id=appointment_id,
    )
    
    created_media = await create_media_use_case.execute(
        payload=finalize_payload,
        org_id=org_id_1,
        creator_id=user_id,
    )
    media_id = created_media.id
    
    # Попытка скачать из org_id_2 должна быть запрещена
    # (логика: если медиа в org_id_1, а user_id не создатель и org_id != org_id_1, доступ запрещен)
    get_download_intent_use_case = GetMediaDownloadIntentUseCase(media_dao, media_download_dao)
    
    def fake_generate_presigned_get_url(**kwargs):
        return {
            "url": f"https://s3.example.com/bucket/{kwargs['key']}",
            "expires_in": kwargs["expires_in"],
        }
    
    monkeypatch.setattr(s3_service, "generate_presigned_get_url", fake_generate_presigned_get_url)
    
    # Попытка скачать как другой пользователь из другой org - должна быть запрещена
    other_user_id = uuid4()
    with pytest.raises(MediaAccessDeniedError):
        await get_download_intent_use_case.execute(
            media_id=media_id,
            org_id=org_id_2,  # Другая организация
            plan=Plan.ACTIVE,
            user_id=other_user_id,  # Другой пользователь (не создатель)
        )
    assert len(media_download_dao.downloads) == 0


@pytest.mark.asyncio
async def test_download_rejected_for_soft_deleted_media(monkeypatch):
    """Soft-deleted media should not expose a download intent."""
    org_id = uuid4()
    user_id = uuid4()
    appointment_id = uuid4()

    media_dao = FakeMediaDAO()
    media_download_dao = FakeMediaDownloadDAO()

    create_media_use_case = CreateMediaUseCase(media_dao)
    finalize_payload = SimpleNamespace(
        kind="before_photo",
        storage_key=f"org/{org_id}/appointments/{appointment_id}/before/deleted.jpg",
        file_name="photo.jpg",
        mime_type="image/jpeg",
        size_bytes=1048576,
        client_id=None,
        appointment_id=appointment_id,
    )
    created_media = await create_media_use_case.execute(
        payload=finalize_payload,
        org_id=org_id,
        creator_id=user_id,
    )
    created_media.deleted_at = datetime.datetime.now(datetime.timezone.utc)

    get_download_intent_use_case = GetMediaDownloadIntentUseCase(media_dao, media_download_dao)

    monkeypatch.setattr(
        media_use_cases,
        "generate_presigned_get_url",
        lambda **kwargs: {"url": f"https://s3.example.com/bucket/{kwargs['key']}", "expires_in": kwargs["expires_in"]},
    )

    with pytest.raises(MediaNotFoundError):
        await get_download_intent_use_case.execute(
            media_id=created_media.id,
            org_id=org_id,
            plan=Plan.ACTIVE,
            user_id=user_id,
        )
    assert len(media_download_dao.downloads) == 0


@pytest.mark.asyncio
async def test_download_creator_can_always_access(monkeypatch):
    """Создатель может скачивать своё медиа из любой организации."""
    org_id = uuid4()
    creator_id = uuid4()
    appointment_id = uuid4()
    
    media_dao = FakeMediaDAO()
    media_download_dao = FakeMediaDownloadDAO()
    
    # Создать медиа
    create_media_use_case = CreateMediaUseCase(media_dao)
    
    finalize_payload = SimpleNamespace(
        kind="before_photo",
        storage_key=f"org/{org_id}/appointments/{appointment_id}/before/test.jpg",
        file_name="photo.jpg",
        mime_type="image/jpeg",
        size_bytes=1048576,
        client_id=None,
        appointment_id=appointment_id,
    )
    
    created_media = await create_media_use_case.execute(
        payload=finalize_payload,
        org_id=org_id,
        creator_id=creator_id,  # Создатель
    )
    media_id = created_media.id
    
    # Создатель может скачать
    get_download_intent_use_case = GetMediaDownloadIntentUseCase(media_dao, media_download_dao)
    
    def fake_generate_presigned_get_url(**kwargs):
        return {
            "url": f"https://s3.example.com/bucket/{kwargs['key']}",
            "expires_in": kwargs["expires_in"],
        }
    
    monkeypatch.setattr(media_use_cases, "generate_presigned_get_url", fake_generate_presigned_get_url)
    
    download_intent = await get_download_intent_use_case.execute(
        media_id=media_id,
        org_id=org_id,
        plan=Plan.ACTIVE,
        user_id=creator_id,  # Тот же пользователь
    )
    
    assert download_intent["media_id"] == str(media_id)
    assert "get_url" in download_intent
    assert len(media_download_dao.downloads) == 1


@pytest.mark.asyncio
async def test_article_image_public_access(monkeypatch):
    """Публичное изображение статьи доступно для всех в org."""
    org_id = uuid4()
    creator_id = uuid4()
    other_user_id = uuid4()
    
    media_dao = FakeMediaDAO()
    media_download_dao = FakeMediaDownloadDAO()
    
    # Создать публичное изображение
    create_media_use_case = CreateMediaUseCase(media_dao)
    
    finalize_payload = SimpleNamespace(
        kind="article_image",
        storage_key="article/test.jpg",
        file_name="article.jpg",
        mime_type="image/jpeg",
        size_bytes=512000,
        client_id=None,
        appointment_id=None,
    )
    
    created_media = await create_media_use_case.execute(
        payload=finalize_payload,
        org_id=None,  # Публичное
        creator_id=creator_id,
    )
    media_id = created_media.id
    
    # Другой пользователь может скачать публичное изображение
    get_download_intent_use_case = GetMediaDownloadIntentUseCase(media_dao, media_download_dao)
    
    def fake_generate_presigned_get_url(**kwargs):
        return {
            "url": f"https://s3.example.com/bucket/{kwargs['key']}",
            "expires_in": kwargs["expires_in"],
        }
    
    monkeypatch.setattr(media_use_cases, "generate_presigned_get_url", fake_generate_presigned_get_url)
    
    download_intent = await get_download_intent_use_case.execute(
        media_id=media_id,
        org_id=org_id,  # Любая организация
        plan=Plan.LIMITED,
        user_id=other_user_id,  # Другой пользователь
    )
    
    assert download_intent["media_id"] == str(media_id)
    assert len(media_download_dao.downloads) == 1


@pytest.mark.asyncio
async def test_limited_mode_blocks_org_bound_media(monkeypatch):
    """В limited-режиме обычные org-bound медиа недоступны, если пользователь не создатель."""
    org_id = uuid4()
    creator_id = uuid4()
    limited_user_id = uuid4()
    appointment_id = uuid4()

    media_dao = FakeMediaDAO()
    media_download_dao = FakeMediaDownloadDAO()

    create_media_use_case = CreateMediaUseCase(media_dao)
    finalize_payload = SimpleNamespace(
        kind="before_photo",
        storage_key=f"org/{org_id}/appointments/{appointment_id}/before/limited.jpg",
        file_name="photo.jpg",
        mime_type="image/jpeg",
        size_bytes=1048576,
        client_id=None,
        appointment_id=appointment_id,
    )

    created_media = await create_media_use_case.execute(
        payload=finalize_payload,
        org_id=org_id,
        creator_id=creator_id,
    )

    get_download_intent_use_case = GetMediaDownloadIntentUseCase(media_dao, media_download_dao)

    monkeypatch.setattr(
        media_use_cases,
        "generate_presigned_get_url",
        lambda **kwargs: {"url": f"https://s3.example.com/bucket/{kwargs['key']}", "expires_in": kwargs["expires_in"]},
    )

    with pytest.raises(MediaAccessDeniedError):
        await get_download_intent_use_case.execute(
            media_id=created_media.id,
            org_id=org_id,
            plan=Plan.LIMITED,
            user_id=limited_user_id,
        )

    assert len(media_download_dao.downloads) == 0






