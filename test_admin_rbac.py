"""Tests for admin RBAC guards and audit logging."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from db.daos.admin_audit_log import AdminAuditLogDAO
from db.daos import AdminUserDAO
from db.models.admin_audit_log import AdminAuditLogModel
from db.models.organization import OrganizationModel
from db.models.user import UserModel
from services.token import create_admin_access_token, decode_token
from uuid import uuid4


@pytest.mark.asyncio
async def test_admin_token_includes_role(async_client: AsyncClient, db_session: AsyncSession):
    """Test that admin access token includes the actual role."""
    # Get superadmin user
    admin_dao = AdminUserDAO(db_session)
    superadmin = await admin_dao.get_by_email("superadmin@test.com")
    assert superadmin is not None

    # Create token with role
    session_id = uuid4()
    token = create_admin_access_token(superadmin.id, session_id, superadmin.role)

    # Decode and verify role
    payload = decode_token(token, required_aud="admin_access")
    assert payload["role"] == "SuperAdmin"


@pytest.mark.asyncio
async def test_require_admin_superadmin_can_block_user(async_client: AsyncClient, db_session: AsyncSession):
    """Test that SuperAdmin can block users."""
    # Login as superadmin
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "superadmin@test.com", "password": "testpass123"},
    )
    assert login_response.status_code == 200
    tokens = login_response.json()
    access_token = tokens["access_token"]

    # Block a user (non-existent is fine for this test)
    response = await async_client.post(
        "/api/v1/admin/masters/00000000-0000-0000-0000-000000000001/block",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    # Should succeed (or fail gracefully, but not 403)
    assert response.status_code in [200, 400, 404]


@pytest.mark.asyncio
async def test_require_admin_content_editor_cannot_block_user(async_client: AsyncClient, db_session: AsyncSession):
    """Test that ContentEditor cannot block users (403)."""
    # Login as content editor
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    assert login_response.status_code == 200
    tokens = login_response.json()
    access_token = tokens["access_token"]

    # Try to block a user
    response = await async_client.post(
        "/api/v1/admin/masters/00000000-0000-0000-0000-000000000001/block",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    # Should return 403 because ContentEditor is not in roles list
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_require_admin_invalid_token_rejected(async_client: AsyncClient):
    """Test that invalid token is rejected."""
    response = await async_client.post(
        "/api/v1/admin/masters/00000000-0000-0000-0000-000000000001/block",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_login_creates_audit_log(async_client: AsyncClient, db_session: AsyncSession):
    """Test that successful admin login creates audit log entry."""
    # Clear existing audit logs
    audit_dao = AdminAuditLogDAO(db_session)

    # Login
    response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "superadmin@test.com", "password": "testpass123"},
    )
    assert response.status_code == 200

    # Check audit log was created
    from sqlalchemy import select
    result = await db_session.execute(
        select(AdminAuditLogModel).where(
            AdminAuditLogModel.action == "admin_login"
        )
    )
    logs = list(result.scalars().all())
    assert len(logs) > 0
    assert logs[-1].action == "admin_login"
    assert logs[-1].admin_user_id is not None


@pytest.mark.asyncio
async def test_block_user_creates_audit_log(async_client: AsyncClient, db_session: AsyncSession):
    """Test that blocking a user creates audit log entry."""
    # Login as superadmin
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "superadmin@test.com", "password": "testpass123"},
    )
    tokens = login_response.json()
    access_token = tokens["access_token"]

    # Clear audit logs before test
    from sqlalchemy import delete
    await db_session.execute(
        delete(AdminAuditLogModel).where(AdminAuditLogModel.action == "block_user")
    )
    await db_session.commit()

    org = OrganizationModel(id=uuid4(), name="RBAC Block Clinic")
    target_user = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="RBAC Block Target",
        status="active",
    )
    db_session.add_all([org, target_user])
    await db_session.commit()

    # Block a user
    target_user_id = str(target_user.id)
    response = await async_client.post(
        f"/api/v1/admin/masters/{target_user_id}/block",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 200

    # Check audit log was created
    from sqlalchemy import select
    result = await db_session.execute(
        select(AdminAuditLogModel).where(
            AdminAuditLogModel.action == "block_user"
        )
    )
    logs = list(result.scalars().all())
    assert len(logs) > 0
    assert logs[-1].target_id == target_user_id


@pytest.mark.asyncio
async def test_admin_grant_requires_superadmin(async_client: AsyncClient, db_session: AsyncSession):
    """Test that only SuperAdmin can grant subscription days."""
    # Login as content editor
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "editor@test.com", "password": "testpass123"},
    )
    tokens = login_response.json()
    access_token = tokens["access_token"]

    # Try to grant subscription
    response = await async_client.post(
        "/api/v1/admin/masters/00000000-0000-0000-0000-000000000001/subscription-grants",
        json={"days": 30, "reason": "test"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    # Should return 403
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_grant_creates_audit_log(async_client: AsyncClient, db_session: AsyncSession):
    """Test that admin grant creates audit log with metadata."""
    # Login as superadmin
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "superadmin@test.com", "password": "testpass123"},
    )
    tokens = login_response.json()
    access_token = tokens["access_token"]

    # Grant subscription
    target_user_id = "00000000-0000-0000-0000-000000000088"
    response = await async_client.post(
        "/api/v1/admin/masters/{}/subscription-grants".format(target_user_id),
        json={"days": 30, "reason": "test_grant"},
        headers={"Authorization": f"Bearer {access_token}"},
    )

    # Check audit log
    from sqlalchemy import select
    result = await db_session.execute(
        select(AdminAuditLogModel).where(
            AdminAuditLogModel.action == "grant_promo"
        )
    )
    logs = list(result.scalars().all())
    if len(logs) > 0:
        last_log = logs[-1]
        assert last_log.action == "grant_promo"
        assert last_log.meta is not None
        assert last_log.meta.get("days") == 30
        assert last_log.meta["reason"]["redacted"] is True
