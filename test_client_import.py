"""
Tests for client import API endpoints.
"""

import pytest
import pytest_asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import status
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport

from dto.client_import_job import ClientImportJobDTO
from dto.auth import CurrentUserDTO
from api.v1.dependencies.auth import get_current_user
from api.v1.dependencies.subscription import require_active_subscription
from src.main import app


pytestmark = pytest.mark.no_db

_TEST_ORG_ID = uuid.uuid4()
_TEST_USER_ID = uuid.uuid4()


@pytest_asyncio.fixture
async def client():
    def _override_user():
        return CurrentUserDTO(
            id=_TEST_USER_ID,
            session_id=uuid.uuid4(),
            email="importtest@example.com",
            phone=None,
            full_name="Import Test User",
            org_id=_TEST_ORG_ID,
            status="active",
        )

    def _override_subscription():
        return None

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[require_active_subscription] = _override_subscription

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def auth_headers():
    return {}


@pytest.mark.asyncio
async def test_create_client_import_job(client, auth_headers):
    """Test creating a client import job."""
    
    # Mock data
    org_id = uuid.uuid4()
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    storage_key = "s3://bucket/import-file.csv"
    
    # Create mock DTO
    mock_job_dto = ClientImportJobDTO(
        id=job_id,
        org_id=org_id,
        status="queued",
        requested_by=user_id,
        input_storage_key=storage_key,
        result_storage_key=None,
        error_message=None,
        total_rows=None,
        processed_rows=None,
        success_count=None,
        failed_count=None,
        skipped_count=None,
        result_expires_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        started_at=None,
        completed_at=None,
        failed_at=None,
    )
    
    # Patch the use case
    with patch(
        "api.v1.dependencies.import_jobs.use_cases.get_create_client_import_job_use_case"
    ) as mock_get_use_case:
        mock_use_case = AsyncMock()
        mock_use_case.execute.return_value = mock_job_dto
        mock_get_use_case.return_value = mock_use_case
        
        # Make request
        response = await client.post(
            f"/api/v1/clients/imports?input_storage_key={storage_key}&update_existing=true",
            headers=auth_headers,
        )
        
        # Assertions
        assert response.status_code == status.HTTP_202_ACCEPTED
        data = response.json()
        assert data["job_id"] == str(job_id)
        assert data["status"] == "queued"


@pytest.mark.asyncio
async def test_get_client_import_status(client, auth_headers):
    """Test getting client import job status."""
    
    job_id = uuid.uuid4()
    org_id = uuid.uuid4()
    
    mock_job_dto = ClientImportJobDTO(
        id=job_id,
        org_id=org_id,
        status="processing",
        requested_by=None,
        input_storage_key="s3://bucket/file.csv",
        result_storage_key=None,
        error_message=None,
        total_rows=100,
        processed_rows=50,
        success_count=45,
        failed_count=5,
        skipped_count=0,
        result_expires_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        completed_at=None,
        failed_at=None,
    )
    
    with patch(
        "api.v1.dependencies.import_jobs.use_cases.get_client_import_job_status_use_case"
    ) as mock_get_use_case:
        mock_use_case = AsyncMock()
        mock_use_case.execute.return_value = mock_job_dto
        mock_get_use_case.return_value = mock_use_case
        
        response = await client.get(
            f"/api/v1/clients/imports/{job_id}",
            headers=auth_headers,
        )
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["job_id"] == str(job_id)
        assert data["status"] == "processing"
        assert data["processed_rows"] == 50


@pytest.mark.asyncio
async def test_get_client_import_report_link(client, auth_headers):
    """Test getting client import report download link."""
    
    job_id = uuid.uuid4()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    
    with patch(
        "api.v1.dependencies.import_jobs.use_cases.get_client_import_job_report_link_use_case"
    ) as mock_get_use_case:
        mock_use_case = AsyncMock()
        mock_use_case.execute.return_value = {
            "job_id": str(job_id),
            "status": "done",
            "download_url": "https://presigned-url.example.com/report.csv",
            "expires_in": 900,
            "result_expires_at": datetime.now(timezone.utc),
        }
        mock_get_use_case.return_value = mock_use_case
        
        response = await client.get(
            f"/api/v1/clients/imports/{job_id}/report-link",
            headers=auth_headers,
        )
        
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["job_id"] == str(job_id)
        assert data["status"] == "done"
        assert "download_url" in data
        assert data["expires_in"] == 900

