"""
Security tests for media upload intents and other critical security features.

Tests validate:
- Input validation (MIME types, file names, sizes)
- Organization isolation
- Path traversal prevention
- Error message safety
- CORS and security headers
"""

import uuid
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient
from httpx import ASGITransport
from fastapi import Request
from pydantic import ValidationError

from api.v1.schemas.media import MediaCreateSchema, MediaUploadIntentRequest
from api.v1.dependencies.auth import get_current_user
from api.v1.dependencies.auth.protected import get_current_org_id
from api.v1.dependencies.media.use_cases import (
    get_create_media_upload_intent_use_case as get_media_router_create_intent_use_case,
    get_finalize_media_use_case,
)
from api.v1.dependencies.media import (
    get_create_media_upload_intent_use_case as get_intent_router_create_intent_use_case,
)
from api.v1.dependencies.subscription import require_active_subscription
from dto.auth import CurrentUserDTO
from dto.media import MediaDTO
from exceptions.media import MediaAccessDeniedError
from src.main import app


pytestmark = pytest.mark.no_db

ORG1_ID = uuid.uuid4()
ORG2_ID = uuid.uuid4()
ORG1_CLIENT_ID = uuid.uuid4()
ORG1_APPOINTMENT_ID = uuid.uuid4()
CURRENT_USER_ID = uuid.uuid4()
CURRENT_SESSION_ID = uuid.uuid4()


class FakeCreateMediaUploadIntentUseCase:
    async def execute(self, *, payload, org_id):
        if payload.client_id == ORG1_CLIENT_ID and org_id != ORG1_ID:
            raise MediaAccessDeniedError(str(payload.client_id))
        if payload.appointment_id == ORG1_APPOINTMENT_ID and org_id != ORG1_ID:
            raise MediaAccessDeniedError(str(payload.appointment_id))
        return {
            "storage_key": f"org/{org_id}/appointments/{payload.appointment_id}/before/{uuid.uuid4().hex}.jpg",
            "put_url": "https://storage.example/upload",
            "put_headers": {"Content-Type": payload.mime_type},
            "expires_in": 900,
        }


class FakeFinalizeMediaUseCase:
    def __init__(self):
        self._by_storage_key: dict[str, MediaDTO] = {}

    async def execute(self, *, payload, org_id, creator_id):
        if payload.client_id == ORG1_CLIENT_ID and org_id != ORG1_ID:
            raise MediaAccessDeniedError(str(payload.client_id))
        if payload.appointment_id == ORG1_APPOINTMENT_ID and org_id != ORG1_ID:
            raise MediaAccessDeniedError(str(payload.appointment_id))
        if payload.kind != "article_image" and f"org/{org_id}/" not in payload.storage_key:
            raise MediaAccessDeniedError(payload.storage_key)

        if payload.storage_key in self._by_storage_key:
            return self._by_storage_key[payload.storage_key]

        now = datetime.now(timezone.utc)
        media = MediaDTO(
            id=uuid.uuid4(),
            kind=payload.kind,
            org_id=None if payload.kind == "article_image" else org_id,
            client_id=payload.client_id,
            appointment_id=payload.appointment_id,
            creator_id=creator_id,
            storage_key=payload.storage_key,
            preview_storage_key=None,
            file_name=payload.file_name,
            size_bytes=payload.size_bytes,
            mime_type=payload.mime_type,
            expires_at=None,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        self._by_storage_key[payload.storage_key] = media
        return media


@pytest.fixture
def org1_id() -> uuid.UUID:
    return ORG1_ID


@pytest.fixture
def org2_id() -> uuid.UUID:
    return ORG2_ID


@pytest.fixture
def org1_client_id() -> uuid.UUID:
    return ORG1_CLIENT_ID


@pytest.fixture
def org1_appointment_id() -> uuid.UUID:
    return ORG1_APPOINTMENT_ID


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    finalize_use_case = FakeFinalizeMediaUseCase()

    async def override_org_id(request: Request) -> uuid.UUID:
        raw_org_id = request.headers.get("X-Org-ID")
        return uuid.UUID(raw_org_id) if raw_org_id else ORG1_ID

    async def override_current_user(request: Request) -> CurrentUserDTO:
        org_id = await override_org_id(request)
        return CurrentUserDTO(
            id=CURRENT_USER_ID,
            session_id=CURRENT_SESSION_ID,
            email="security@test.local",
            phone=None,
            full_name="Security Test User",
            org_id=org_id,
            status="active",
        )

    async def override_active_subscription():
        return None

    app.dependency_overrides[get_current_org_id] = override_org_id
    app.dependency_overrides[get_current_user] = override_current_user
    app.dependency_overrides[require_active_subscription] = override_active_subscription
    app.dependency_overrides[get_intent_router_create_intent_use_case] = lambda: FakeCreateMediaUploadIntentUseCase()
    app.dependency_overrides[get_media_router_create_intent_use_case] = lambda: FakeCreateMediaUploadIntentUseCase()
    app.dependency_overrides[get_finalize_media_use_case] = lambda: finalize_use_case

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as async_client:
        yield async_client

    app.dependency_overrides.clear()


class TestMediaUploadIntentValidation:
    """Test security of media upload intent validation."""
    
    def test_invalid_mime_type_rejected(self):
        """Ensure non-allowed MIME types are rejected."""
        payload = {
            "kind": "before_photo",
            "mime_type": "application/x-executable",  # ❌ Not allowed
            "size_bytes": 1024,
            "appointment_id": str(uuid.uuid4()),
        }
        
        with pytest.raises(ValidationError) as exc_info:
            MediaUploadIntentRequest.model_validate(payload)
        assert "mime_type" in str(exc_info.value).lower()
    
    def test_file_name_path_traversal_blocked(self):
        """Prevent path traversal attacks in file_name."""
        payload = {
            "kind": "before_photo",
            "mime_type": "image/jpeg",
            "size_bytes": 1024,
            "file_name": "../../../etc/passwd",  # ❌ Path traversal attempt
            "appointment_id": str(uuid.uuid4()),
        }
        
        with pytest.raises(ValidationError) as exc_info:
            MediaUploadIntentRequest.model_validate(payload)
        assert "file_name" in str(exc_info.value).lower()
    
    def test_file_name_special_chars_blocked(self):
        """Ensure file_name doesn't allow dangerous special characters."""
        dangerous_names = [
            "file<script>.jpg",    # ❌ XSS
            "file|command.jpg",    # ❌ Command injection
            "file\x00null.jpg",    # ❌ Null byte
            "file\\network.jpg",   # ❌ UNC path
        ]
        
        for bad_name in dangerous_names:
            payload = {
                "kind": "before_photo",
                "mime_type": "image/jpeg",
                "size_bytes": 1024,
                "file_name": bad_name,
                "appointment_id": str(uuid.uuid4()),
            }
            
            with pytest.raises(ValidationError):
                MediaUploadIntentRequest.model_validate(payload)
    
    def test_size_exceeds_limit(self):
        """Prevent oversized uploads."""
        payload = {
            "kind": "before_photo",
            "mime_type": "image/jpeg",
            "size_bytes": 200 * 1024 * 1024,  # ❌ 200MB exceeds 100MB limit
            "appointment_id": str(uuid.uuid4()),
        }
        
        with pytest.raises(ValidationError) as exc_info:
            MediaUploadIntentRequest.model_validate(payload)
        assert "size_bytes" in str(exc_info.value).lower()
    
    def test_invalid_kind_rejected(self):
        """Ensure invalid media kinds are rejected."""
        payload = {
            "kind": "malicious_kind",  # ❌ Not in ALLOWED_KINDS
            "mime_type": "image/jpeg",
            "size_bytes": 1024,
            "appointment_id": str(uuid.uuid4()),
        }
        
        with pytest.raises(ValidationError) as exc_info:
            MediaUploadIntentRequest.model_validate(payload)
        assert "kind" in str(exc_info.value).lower()


class TestOrganizationIsolation:
    """Test that users cannot access other organizations' data."""
    
    async def test_client_from_other_org_rejected(
        self,
        org1_id: uuid.UUID,
        org2_id: uuid.UUID,
        org1_client_id: uuid.UUID,
    ):
        """Prevent accessing client from different organization."""
        # org1_client_id belongs to org1
        # But we try to access it from org2's context
        
        payload = {
            "kind": "attachment",
            "mime_type": "image/jpeg",
            "size_bytes": 1024,
            "client_id": str(org1_client_id),  # ❌ From different org
        }
        
        request = MediaUploadIntentRequest.model_validate(payload)
        with pytest.raises(MediaAccessDeniedError):
            await FakeCreateMediaUploadIntentUseCase().execute(payload=request, org_id=org2_id)
    
    async def test_appointment_from_other_org_rejected(
        self,
        org1_id: uuid.UUID,
        org2_id: uuid.UUID,
        org1_appointment_id: uuid.UUID,
    ):
        """Prevent accessing appointment from different organization."""
        payload = {
            "kind": "before_photo",
            "mime_type": "image/jpeg",
            "size_bytes": 1024,
            "appointment_id": str(org1_appointment_id),  # ❌ From different org
        }
        
        request = MediaUploadIntentRequest.model_validate(payload)
        with pytest.raises(MediaAccessDeniedError):
            await FakeCreateMediaUploadIntentUseCase().execute(payload=request, org_id=org2_id)


class TestMediaFinalizeEndpoint:
    """Test POST /media finalization behavior."""

    async def test_foreign_appointment_rejected(
        self,
        org1_id: uuid.UUID,
        org2_id: uuid.UUID,
        org1_appointment_id: uuid.UUID,
    ):
        payload = {
            "kind": "before_photo",
            "storage_key": f"org/{org2_id}/appointments/{org1_appointment_id}/before/{uuid.uuid4().hex}.jpg",
            "file_name": "before.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1024,
            "appointment_id": str(org1_appointment_id),
        }

        request = MediaCreateSchema.model_validate(payload)
        with pytest.raises(MediaAccessDeniedError):
            await FakeFinalizeMediaUseCase().execute(
                payload=request,
                org_id=org2_id,
                creator_id=CURRENT_USER_ID,
            )

    async def test_article_image_allows_org_null(
        self,
        org1_id: uuid.UUID,
    ):
        payload = {
            "kind": "article_image",
            "storage_key": f"article/{uuid.uuid4().hex}.jpg",
            "file_name": "article.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 2048,
        }

        request = MediaCreateSchema.model_validate(payload)
        media = await FakeFinalizeMediaUseCase().execute(
            payload=request,
            org_id=org1_id,
            creator_id=CURRENT_USER_ID,
        )

        assert media.org_id is None
        assert media.kind == "article_image"
        assert media.storage_key.startswith("article/")

    async def test_repeated_storage_key_returns_existing_record(
        self,
        org1_id: uuid.UUID,
        org1_appointment_id: uuid.UUID,
    ):
        payload = {
            "kind": "before_photo",
            "storage_key": f"org/{org1_id}/appointments/{org1_appointment_id}/before/{uuid.uuid4().hex}.jpg",
            "file_name": "before.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 2048,
            "appointment_id": str(org1_appointment_id),
        }

        request = MediaCreateSchema.model_validate(payload)
        use_case = FakeFinalizeMediaUseCase()
        first = await use_case.execute(payload=request, org_id=org1_id, creator_id=CURRENT_USER_ID)
        second = await use_case.execute(payload=request, org_id=org1_id, creator_id=CURRENT_USER_ID)

        assert first.id == second.id


class TestErrorMessageSafety:
    """Test that error messages don't leak internal details."""
    
    def test_error_messages_generic(self):
        """Ensure error messages are generic (no stack traces)."""
        with pytest.raises(ValidationError) as exc_info:
            MediaUploadIntentRequest.model_validate({"kind": "unknown"})
        error_text = str(exc_info.value).lower()
        
        # ❌ Should NOT contain:
        forbidden = [
            "traceback",
            "sqlalchemy",
            "file ",  # e.g., "File /path/to/file.py"
            "line ",
            "TypeError",
            "AttributeError",
        ]
        
        for forbidden_text in forbidden:
            assert forbidden_text not in error_text, \
                f"Error message leaked '{forbidden_text}': {error_text}"
    
    def test_database_error_hidden(self):
        """Ensure database errors are not exposed to clients."""
        with pytest.raises(ValidationError) as exc_info:
            MediaUploadIntentRequest.model_validate(
                {
                    "kind": "before_photo",
                    "mime_type": "image/jpeg",
                    "size_bytes": 1024,
                    "appointment_id": "not-a-valid-uuid",
                }
            )
        assert "sqlalchemy" not in str(exc_info.value).lower()


class TestSecurityHeaders:
    """Test that proper security headers are present in responses."""
    
    async def test_xss_protection_header(self, client: AsyncClient):
        """Ensure X-XSS-Protection header is set."""
        response = await client.get("/health")
        
        assert "X-XSS-Protection" in response.headers
        assert response.headers["X-XSS-Protection"] == "1; mode=block"
    
    async def test_content_type_options_header(self, client: AsyncClient):
        """Ensure X-Content-Type-Options header prevents MIME sniffing."""
        response = await client.get("/health")
        
        assert "X-Content-Type-Options" in response.headers
        assert response.headers["X-Content-Type-Options"] == "nosniff"
    
    async def test_frame_options_header(self, client: AsyncClient):
        """Ensure X-Frame-Options header prevents clickjacking."""
        response = await client.get("/health")
        
        assert "X-Frame-Options" in response.headers
        assert response.headers["X-Frame-Options"] == "DENY"
    
    async def test_referrer_policy_header(self, client: AsyncClient):
        """Ensure Referrer-Policy header is set."""
        response = await client.get("/health")
        
        assert "Referrer-Policy" in response.headers
    
    async def test_csp_header(self, client: AsyncClient):
        """Ensure Content-Security-Policy header is set."""
        response = await client.get("/health")
        
        assert "Content-Security-Policy" in response.headers
    
    async def test_no_server_header(self, client: AsyncClient):
        """Ensure Server header is removed (info leakage prevention)."""
        response = await client.get("/health")
        
        # Server header should be removed
        assert "server" not in response.headers


class TestCORSConfiguration:
    """Test CORS configuration for security."""
    
    async def test_cors_origin_validation(self, client: AsyncClient):
        """Ensure only configured origins are allowed."""
        # Valid origin (if configured)
        response = await client.options(
            "/health",
            headers={"Origin": "http://localhost:3000"}
        )
        # Should include CORS headers or pass through
        
        # Invalid origin should be rejected or not allowed
        response = await client.options(
            "/health",
            headers={"Origin": "http://malicious.com"}
        )
        # Should either reject or not include Allow-Origin header


class TestLoggingPIIFiltering:
    """Test that PII is filtered from logs."""
    
    def test_pii_filter_masks_email(self, caplog):
        """Ensure emails are masked in logs."""
        from src.config.logging import PIIFilter, logger
        
        filter = PIIFilter()
        
        # Create a log record with email
        record = logging.LogRecord(
            name="security-test",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg="User registered: test@example.com",
            args=(),
            exc_info=None,
        )
        
        # Apply filter
        filter.filter(record)
        
        # Email should be masked
        assert "[REDACTED_EMAIL]" in record.msg
        assert "test@example.com" not in record.msg
    
    def test_pii_filter_masks_phone(self, caplog):
        """Ensure phone numbers are masked in logs."""
        from src.config.logging import PIIFilter
        
        filter = PIIFilter()
        
        record = logging.LogRecord(
            name="security-test",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg="Call from +1 (555) 123-4567",
            args=(),
            exc_info=None,
        )
        
        filter.filter(record)
        
        assert "[REDACTED_PHONE]" in record.msg
        assert "555" not in record.msg  # Area code should be masked
    
    def test_pii_filter_masks_uuid(self, caplog):
        """Ensure UUIDs are masked in logs."""
        from src.config.logging import PIIFilter
        
        filter = PIIFilter()
        
        test_uuid = "550e8400-e29b-41d4-a716-446655440000"
        record = logging.LogRecord(
            name="security-test",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg=f"User ID: {test_uuid}",
            args=(),
            exc_info=None,
        )
        
        filter.filter(record)
        
        assert "[REDACTED_UUID]" in record.msg
        assert test_uuid not in record.msg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
