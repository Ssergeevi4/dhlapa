import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

import services.media_tasks as media_tasks


class FakeScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class FakeSession:
    def __init__(self, media_items):
        self.media_items = media_items
        self.deleted_items = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, query):
        now = self.now
        expired = [
            media
            for media in self.media_items
            if media.deleted_at is None and media.expires_at <= now
        ]
        return FakeScalarResult(expired)

    async def delete(self, media):
        self.deleted_items.append(media)
        media.deleted_at = self.now

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


async def _single_session(session):
    yield session


@pytest.mark.asyncio
async def test_cleanup_expired_media_deletes_at_exact_boundary(monkeypatch):
    fixed_now = datetime.datetime(2026, 4, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)
    monkeypatch.setattr(media_tasks.datetime, "datetime", type(
        "FrozenDateTime",
        (datetime.datetime,),
        {"now": classmethod(lambda cls, tz=None: fixed_now if tz else fixed_now.replace(tzinfo=None))},
    ))

    expired_media = SimpleNamespace(
        id=uuid4(),
        storage_key="org/11111111-1111-1111-1111-111111111111/appointments/22222222-2222-2222-2222-222222222222/before/expired.jpg",
        preview_storage_key="org/11111111-1111-1111-1111-111111111111/appointments/22222222-2222-2222-2222-222222222222/before/expired-preview.jpg",
        expires_at=fixed_now,
        deleted_at=None,
    )
    active_media = SimpleNamespace(
        id=uuid4(),
        storage_key="org/11111111-1111-1111-1111-111111111111/appointments/22222222-2222-2222-2222-222222222222/before/future.jpg",
        preview_storage_key=None,
        expires_at=fixed_now + datetime.timedelta(seconds=1),
        deleted_at=None,
    )
    session = FakeSession([expired_media, active_media])
    session.now = fixed_now

    deleted_keys = []

    def fake_delete_object(key: str) -> bool:
        deleted_keys.append(key)
        return True

    async def fake_get_session():
        async for item in _single_session(session):
            yield item

    monkeypatch.setattr(media_tasks, "delete_object", fake_delete_object)
    monkeypatch.setattr(media_tasks, "get_session", fake_get_session)

    await media_tasks.cleanup_expired_media()

    assert deleted_keys == [expired_media.storage_key, expired_media.preview_storage_key]
    assert session.deleted_items == [expired_media]
    assert expired_media.deleted_at == fixed_now
    assert active_media.deleted_at is None
    assert session.commits == 1
    assert session.rollbacks == 0

