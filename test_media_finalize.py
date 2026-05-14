"""Unit tests for FinalizeMediaUseCase with photo limits, soft delete, and binding validation."""
import pytest
import uuid
import datetime
from unittest.mock import AsyncMock

from api.v1.schemas.media import MediaCreateSchema
from use_cases import media as media_use_cases
from use_cases.media import FinalizeMediaUseCase
from dto.media import MediaDTO
from exceptions.media import (
    PhotoLimitExceededError,
    MediaAccessDeniedError,
    MediaNotFoundError,
)


class TestFinalizeMediaUseCase:
    """Test finalize media use case with photo limits and validation."""

    @pytest.fixture
    def media_dao(self):
        """Create a mock MediaDAO."""
        return AsyncMock()

    @pytest.fixture
    def use_case(self, media_dao):
        """Create FinalizeMediaUseCase instance."""
        return FinalizeMediaUseCase(media_dao)

    @pytest.fixture(autouse=True)
    def storage_object_exists(self, monkeypatch):
        monkeypatch.setattr(media_use_cases, "object_exists", lambda *_args, **_kwargs: True)

    @pytest.fixture
    def org_id(self):
        return uuid.UUID("11111111-1111-1111-1111-111111111111")

    @pytest.fixture
    def appointment_id(self):
        return uuid.UUID("22222222-2222-2222-2222-222222222222")

    @pytest.fixture
    def client_id(self):
        return uuid.UUID("33333333-3333-3333-3333-333333333333")

    @pytest.fixture
    def creator_id(self):
        return uuid.UUID("44444444-4444-4444-4444-444444444444")

    # ============ Tests for valid finalization ============

    @pytest.mark.asyncio
    async def test_finalize_before_photo_within_limit(self, use_case, media_dao, org_id, appointment_id, creator_id):
        """Test successful before_photo finalization when limit not reached."""
        # Setup: 5 existing before_photos, limit is 7
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.count_by_appointment_and_kind.return_value = 5  # Current count
        media_dao.get_by_storage_key.return_value = None  # Not yet created

        new_media = MediaDTO(
            id=uuid.uuid4(),
            kind="before_photo",
            org_id=org_id,
            appointment_id=appointment_id,
            creator_id=creator_id,
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/abc123.jpg",
            preview_storage_key=None,
            file_name="photo.jpg",
            size_bytes=1024000,
            mime_type="image/jpeg",
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
            created_at=datetime.datetime.now(datetime.timezone.utc),
            updated_at=datetime.datetime.now(datetime.timezone.utc),
            deleted_at=None,
        )
        media_dao.create.return_value = new_media

        payload = MediaCreateSchema(
            kind="before_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/abc123.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        result = await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

        assert result.id == new_media.id
        assert result.kind == "before_photo"
        assert result.appointment_id == appointment_id
        assert result.deleted_at is None
        media_dao.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_finalize_after_photo_within_limit(self, use_case, media_dao, org_id, appointment_id, creator_id):
        """Test successful after_photo finalization when limit not reached."""
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.count_by_appointment_and_kind.return_value = 2  # Current count < 5

        new_media = MediaDTO(
            id=uuid.uuid4(),
            kind="after_photo",
            org_id=org_id,
            appointment_id=appointment_id,
            creator_id=creator_id,
            storage_key=f"org/{org_id}/appointments/{appointment_id}/after/xyz789.jpg",
            file_name="after.jpg",
            mime_type="image/jpeg",
            size_bytes=2048000,
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
            created_at=datetime.datetime.now(datetime.timezone.utc),
            updated_at=datetime.datetime.now(datetime.timezone.utc),
            deleted_at=None,
        )
        media_dao.create.return_value = new_media

        payload = MediaCreateSchema(
            kind="after_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/after/xyz789.jpg",
            file_name="after.jpg",
            mime_type="image/jpeg",
            size_bytes=2048000,
            appointment_id=appointment_id,
        )

        result = await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

        assert result.kind == "after_photo"
        media_dao.count_by_appointment_and_kind.assert_called_with(appointment_id, "after_photo")

    @pytest.mark.asyncio
    async def test_finalize_attachment_no_limit(self, use_case, media_dao, org_id, appointment_id, creator_id):
        """Test that attachment media has no photo limit."""
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.get_by_storage_key.return_value = None

        new_media = MediaDTO(
            id=uuid.uuid4(),
            kind="attachment",
            org_id=org_id,
            appointment_id=appointment_id,
            creator_id=creator_id,
            storage_key=f"org/{org_id}/appointments/{appointment_id}/attachment/doc.pdf",
            file_name="document.pdf",
            mime_type="application/pdf",
            size_bytes=5000000,
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
            created_at=datetime.datetime.now(datetime.timezone.utc),
            updated_at=datetime.datetime.now(datetime.timezone.utc),
            deleted_at=None,
        )
        media_dao.create.return_value = new_media

        payload = MediaCreateSchema(
            kind="attachment",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/attachment/doc.pdf",
            file_name="document.pdf",
            mime_type="application/pdf",
            size_bytes=5000000,
            appointment_id=appointment_id,
        )

        result = await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

        assert result.kind == "attachment"
        # count_by_appointment_and_kind should NOT be called for attachment
        media_dao.count_by_appointment_and_kind.assert_not_called()

    @pytest.mark.asyncio
    async def test_finalize_idempotent_existing_not_deleted(self, use_case, media_dao, org_id, appointment_id, creator_id):
        """Test that finalizing same storage_key twice returns existing media."""
        existing_media = MediaDTO(
            id=uuid.uuid4(),
            kind="before_photo",
            org_id=org_id,
            appointment_id=appointment_id,
            creator_id=creator_id,
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/dup.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
            created_at=datetime.datetime.now(datetime.timezone.utc),
            updated_at=datetime.datetime.now(datetime.timezone.utc),
            deleted_at=None,  # Not deleted
        )
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.get_by_storage_key.return_value = existing_media

        payload = MediaCreateSchema(
            kind="before_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/dup.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        result = await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

        # Should return existing media without calling create
        assert result.id == existing_media.id
        media_dao.create.assert_not_called()

    # ============ Tests for photo limit violations ============

    @pytest.mark.asyncio
    async def test_finalize_before_photo_limit_exceeded(self, use_case, media_dao, org_id, appointment_id, creator_id):
        """Test that PhotoLimitExceededError raised when before_photo limit reached."""
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.count_by_appointment_and_kind.return_value = 7  # Already at limit
        media_dao.get_by_storage_key.return_value = None

        payload = MediaCreateSchema(
            kind="before_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/too_many.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        with pytest.raises(PhotoLimitExceededError) as exc_info:
            await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

        error = exc_info.value
        assert error.kind == "before_photo"
        assert error.limit == 7
        assert error.current_count == 7
        media_dao.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_finalize_after_photo_limit_exceeded(self, use_case, media_dao, org_id, appointment_id, creator_id):
        """Test that PhotoLimitExceededError raised when after_photo limit reached."""
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.count_by_appointment_and_kind.return_value = 7  # At limit
        media_dao.get_by_storage_key.return_value = None

        payload = MediaCreateSchema(
            kind="after_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/after/too_many.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        with pytest.raises(PhotoLimitExceededError) as exc_info:
            await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

        error = exc_info.value
        assert error.kind == "after_photo"
        assert error.limit == 7

    # ============ Tests for binding validation ============

    @pytest.mark.asyncio
    async def test_finalize_invalid_binding_appointment_not_in_org(self, use_case, media_dao, org_id, appointment_id, creator_id):
        """Test that MediaAccessDeniedError raised when appointment not in org."""
        media_dao.appointment_belongs_to_org.return_value = False  # Appointment not in org
        media_dao.get_by_storage_key.return_value = None

        payload = MediaCreateSchema(
            kind="before_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/bad_binding.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        with pytest.raises(MediaAccessDeniedError):
            await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

    @pytest.mark.asyncio
    async def test_finalize_rejects_missing_storage_object(self, use_case, media_dao, monkeypatch, org_id, appointment_id, creator_id):
        """Test that finalize fails before DB write when uploaded object is missing from storage."""
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.get_by_storage_key.return_value = None
        monkeypatch.setattr(media_use_cases, "object_exists", lambda *_args, **_kwargs: False)

        payload = MediaCreateSchema(
            kind="before_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/missing.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        with pytest.raises(MediaNotFoundError) as exc_info:
            await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

        assert payload.storage_key in str(exc_info.value)
        media_dao.get_by_storage_key.assert_not_called()
        media_dao.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_finalize_article_image_no_binding(self, use_case, media_dao, creator_id):
        """Test article_image with no org_id binding."""
        media_dao.get_by_storage_key.return_value = None

        new_media = MediaDTO(
            id=uuid.uuid4(),
            kind="article_image",
            org_id=None,  # article_image has no org
            appointment_id=None,
            creator_id=creator_id,
            storage_key="article/abc123.jpg",
            file_name="article.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
            created_at=datetime.datetime.now(datetime.timezone.utc),
            updated_at=datetime.datetime.now(datetime.timezone.utc),
            deleted_at=None,
        )
        media_dao.create.return_value = new_media

        payload = MediaCreateSchema(
            kind="article_image",
            storage_key="article/abc123.jpg",
            file_name="article.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
        )

        result = await use_case.execute(payload, org_id=None, creator_id=creator_id)

        assert result.kind == "article_image"
        assert result.org_id is None

    # ============ Tests for soft delete ============

    @pytest.mark.asyncio
    async def test_finalize_soft_deleted_media_can_be_reused(self, use_case, media_dao, org_id, appointment_id, creator_id):
        """Test that soft-deleted media can be recreated (deleted_at allows reuse)."""
        # Existing deleted media
        deleted_media = MediaDTO(
            id=uuid.uuid4(),
            kind="before_photo",
            org_id=org_id,
            appointment_id=appointment_id,
            creator_id=creator_id,
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/reuse.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
            created_at=datetime.datetime.now(datetime.timezone.utc),
            updated_at=datetime.datetime.now(datetime.timezone.utc),
            deleted_at=datetime.datetime.now(datetime.timezone.utc),  # DELETED
        )
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.count_by_appointment_and_kind.return_value = 2  # Within limit
        media_dao.get_by_storage_key.return_value = deleted_media

        new_media = MediaDTO(
            id=uuid.uuid4(),
            kind="before_photo",
            org_id=org_id,
            appointment_id=appointment_id,
            creator_id=creator_id,
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/reuse.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
            created_at=datetime.datetime.now(datetime.timezone.utc),
            updated_at=datetime.datetime.now(datetime.timezone.utc),
            deleted_at=None,  # NOW NOT DELETED
        )
        media_dao.create.return_value = new_media

        payload = MediaCreateSchema(
            kind="before_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/reuse.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        result = await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

        # Should create new record (not return deleted one)
        assert result.deleted_at is None
        media_dao.create.assert_called_once()

    # ============ Tests for edge cases ============

    @pytest.mark.asyncio
    async def test_finalize_photo_exactly_at_limit_still_fails(self, use_case, media_dao, org_id, appointment_id, creator_id):
        """Test that 7th photo (at limit) is rejected."""
        # If limit is 7 and count is 7, new photo should be rejected
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.count_by_appointment_and_kind.return_value = 7  # Exactly at limit
        media_dao.get_by_storage_key.return_value = None

        payload = MediaCreateSchema(
            kind="before_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/at_limit.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        with pytest.raises(PhotoLimitExceededError):
            await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

    @pytest.mark.asyncio
    async def test_finalize_photo_below_limit_succeeds(self, use_case, media_dao, org_id, appointment_id, creator_id):
        """Test that 7th photo (below limit) is accepted."""
        # If limit is 7 and count is 6, new photo should be accepted
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.count_by_appointment_and_kind.return_value = 6  # Below limit
        media_dao.get_by_storage_key.return_value = None

        new_media = MediaDTO(
            id=uuid.uuid4(),
            kind="before_photo",
            org_id=org_id,
            appointment_id=appointment_id,
            creator_id=creator_id,
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/below_limit.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
            created_at=datetime.datetime.now(datetime.timezone.utc),
            updated_at=datetime.datetime.now(datetime.timezone.utc),
            deleted_at=None,
        )
        media_dao.create.return_value = new_media

        payload = MediaCreateSchema(
            kind="before_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/below_limit.jpg",
            file_name="photo.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        result = await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

        assert result is not None
        media_dao.create.assert_called_once()



