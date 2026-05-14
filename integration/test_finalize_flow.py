"""Integration test for full media finalize flow with 7/7 photo limits and soft delete."""
import pytest
import uuid
import datetime
from unittest.mock import AsyncMock

from api.v1.schemas.media import MediaCreateSchema
from use_cases import media as media_use_cases
from use_cases.media import FinalizeMediaUseCase
from dto.media import MediaDTO
from exceptions.media import PhotoLimitExceededError, MediaNotFoundError
from db.daos import MediaDAO


class TestMediaFinalizeIntegration:
    """Integration tests for media finalize flow with limits and soft delete."""

    @pytest.fixture
    def media_dao(self):
        """Create a mock MediaDAO."""
        return AsyncMock(spec=MediaDAO)

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
    def creator_id(self):
        return uuid.UUID("44444444-4444-4444-4444-444444444444")

    @pytest.mark.asyncio
    async def test_full_before_photo_upload_sequence_7_photos(
        self, use_case, media_dao, org_id, appointment_id, creator_id
    ):
        """Test uploading exactly 7 before_photos (boundary test)."""
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.get_by_storage_key.return_value = None

        # Upload photos 1-7
        for i in range(7):
            media_dao.count_by_appointment_and_kind.return_value = i  # i photos exist, adding (i+1)th

            new_media = MediaDTO(
                id=uuid.uuid4(),
                kind="before_photo",
                org_id=org_id,
                appointment_id=appointment_id,
                creator_id=creator_id,
                storage_key=f"org/{org_id}/appointments/{appointment_id}/before/photo{i+1}.jpg",
                file_name=f"photo{i+1}.jpg",
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
                storage_key=f"org/{org_id}/appointments/{appointment_id}/before/photo{i+1}.jpg",
                file_name=f"photo{i+1}.jpg",
                mime_type="image/jpeg",
                size_bytes=1024000,
                appointment_id=appointment_id,
            )

            result = await use_case.execute(payload, org_id=org_id, creator_id=creator_id)
            assert result is not None
            assert result.kind == "before_photo"

        # Try to upload 8th photo - should fail
        media_dao.count_by_appointment_and_kind.return_value = 7  # 7 photos exist
        payload = MediaCreateSchema(
            kind="before_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/photo8.jpg",
            file_name="photo8.jpg",
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

    @pytest.mark.asyncio
    async def test_soft_deleted_photo_not_counted_in_limit(
        self, use_case, media_dao, org_id, appointment_id, creator_id
    ):
        """Test that soft-deleted photos don't count towards limit.
        
        Scenario:
        1. Upload 7 before_photos
        2. Delete one (soft delete)
        3. Should be able to upload 8th photo
        """
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.get_by_storage_key.return_value = None

        # After soft delete, only 6 active photos remain
        media_dao.count_by_appointment_and_kind.return_value = 6

        new_media = MediaDTO(
            id=uuid.uuid4(),
            kind="before_photo",
            org_id=org_id,
            appointment_id=appointment_id,
            creator_id=creator_id,
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/replacement.jpg",
            file_name="replacement.jpg",
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
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/replacement.jpg",
            file_name="replacement.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        # Should succeed because only 6 active photos
        result = await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

        assert result is not None
        assert result.deleted_at is None
        media_dao.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_separate_limits_before_and_after_photos(
        self, use_case, media_dao, org_id, appointment_id, creator_id
    ):
        """Test that before_photo and after_photo have separate limits (each 7)."""
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.get_by_storage_key.return_value = None

        # Upload 7 before_photos
        media_dao.count_by_appointment_and_kind.return_value = 7

        before_payload = MediaCreateSchema(
            kind="before_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/before/8th.jpg",
            file_name="8th.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        # Should fail for before_photo
        with pytest.raises(PhotoLimitExceededError):
            await use_case.execute(before_payload, org_id=org_id, creator_id=creator_id)

        # But after_photo should still have 0 photos, so should succeed
        media_dao.count_by_appointment_and_kind.return_value = 0

        new_media = MediaDTO(
            id=uuid.uuid4(),
            kind="after_photo",
            org_id=org_id,
            appointment_id=appointment_id,
            creator_id=creator_id,
            storage_key=f"org/{org_id}/appointments/{appointment_id}/after/1st.jpg",
            file_name="1st.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            expires_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
            created_at=datetime.datetime.now(datetime.timezone.utc),
            updated_at=datetime.datetime.now(datetime.timezone.utc),
            deleted_at=None,
        )
        media_dao.create.return_value = new_media

        after_payload = MediaCreateSchema(
            kind="after_photo",
            storage_key=f"org/{org_id}/appointments/{appointment_id}/after/1st.jpg",
            file_name="1st.jpg",
            mime_type="image/jpeg",
            size_bytes=1024000,
            appointment_id=appointment_id,
        )

        result = await use_case.execute(after_payload, org_id=org_id, creator_id=creator_id)

        assert result is not None
        assert result.kind == "after_photo"

    @pytest.mark.asyncio
    async def test_http_409_response_code_for_limit_exceeded(
        self, use_case, media_dao, org_id, appointment_id, creator_id
    ):
        """Test that PhotoLimitExceededError is raised with correct attributes for 409 HTTP response."""
        media_dao.appointment_belongs_to_org.return_value = True
        media_dao.count_by_appointment_and_kind.return_value = 7
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
        # Verify error has attributes for HTTP 409 response
        assert hasattr(error, "kind")
        assert hasattr(error, "limit")
        assert hasattr(error, "current_count")
        assert hasattr(error, "appointment_id")
        assert error.kind == "before_photo"
        assert error.limit == 7
        assert error.current_count == 7

    @pytest.mark.asyncio
    async def test_finalize_fails_when_storage_object_missing(
        self, use_case, media_dao, monkeypatch, org_id, appointment_id, creator_id
    ):
        """Finalize should not write to DB if the uploaded object is missing from storage."""
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

        with pytest.raises(MediaNotFoundError):
            await use_case.execute(payload, org_id=org_id, creator_id=creator_id)

        media_dao.get_by_storage_key.assert_not_called()
        media_dao.create.assert_not_called()

