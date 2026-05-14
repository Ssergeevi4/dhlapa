import uuid

import pytest
from pydantic import ValidationError

from api.v1.schemas.media import (
    MediaCreateSchema,
    MediaUploadIntentRequest,
    ZipArchiveDownloadIntentResponse,
)


def test_upload_intent_requires_appointment_for_before_photo():
    with pytest.raises(ValidationError) as exc_info:
        MediaUploadIntentRequest.model_validate(
            {
                "kind": "before_photo",
                "mime_type": "image/jpeg",
                "size_bytes": 1024,
            }
        )

    assert "appointment_id" in str(exc_info.value).lower()


def test_upload_intent_rejects_client_binding_for_before_photo():
    with pytest.raises(ValidationError) as exc_info:
        MediaUploadIntentRequest.model_validate(
            {
                "kind": "before_photo",
                "mime_type": "image/jpeg",
                "size_bytes": 1024,
                "client_id": str(uuid.uuid4()),
                "appointment_id": str(uuid.uuid4()),
            }
        )

    assert "client_id must be omitted" in str(exc_info.value).lower()


def test_upload_intent_attachment_requires_exactly_one_binding():
    with pytest.raises(ValidationError) as exc_info:
        MediaUploadIntentRequest.model_validate(
            {
                "kind": "attachment",
                "mime_type": "application/pdf",
                "size_bytes": 1024,
                "client_id": str(uuid.uuid4()),
                "appointment_id": str(uuid.uuid4()),
            }
        )

    assert "attachment requires exactly one binding" in str(exc_info.value).lower()


def test_finalize_media_rejects_article_image_binding():
    with pytest.raises(ValidationError) as exc_info:
        MediaCreateSchema.model_validate(
            {
                "kind": "article_image",
                "storage_key": "article/example.jpg",
                "file_name": "example.jpg",
                "mime_type": "image/jpeg",
                "size_bytes": 1024,
                "appointment_id": str(uuid.uuid4()),
            }
        )

    assert "article_image must not be bound" in str(exc_info.value).lower()


def test_zip_archive_response_parses_appointment_id_as_uuid():
    appointment_id = uuid.uuid4()

    response = ZipArchiveDownloadIntentResponse.model_validate(
        {
            "appointment_id": str(appointment_id),
            "storage_key": f"org/example/appointments/{appointment_id}/archive_zip/abc.zip",
            "files_count": 2,
            "total_size_bytes": 4096,
            "expires_in": 900,
        }
    )

    assert response.appointment_id == appointment_id
    assert isinstance(response.appointment_id, uuid.UUID)

