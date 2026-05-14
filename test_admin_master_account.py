from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.daos import AdminUserDAO
from db.models.admin_audit_log import AdminAuditLogModel
from db.models.admin_session import AdminSessionModel
from db.models.organization import OrganizationModel
from db.models.user import UserModel
from services.token import create_admin_access_token


async def _admin_auth_headers(db_session: AsyncSession, email: str) -> dict[str, str]:
    admin = await AdminUserDAO(db_session).get_by_email(email)
    assert admin is not None
    session_id = uuid4()
    db_session.add(
        AdminSessionModel(
            id=session_id,
            admin_user_id=admin.id,
            refresh_token_hash=f"test-refresh:{session_id}",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            status="active",
        )
    )
    await db_session.commit()
    token = create_admin_access_token(admin.id, session_id, admin.role)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_superadmin_unblocks_master_idempotently_and_audits(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    org = OrganizationModel(id=uuid4(), name="Unlock Clinic")
    master = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="Blocked Master",
        email="blocked.unlock@example.com",
        phone="79000000111",
        status="blocked",
    )
    db_session.add_all([org, master])
    await db_session.commit()

    headers = await _admin_auth_headers(db_session, "superadmin@test.com")
    response = await async_client.post(
        f"/api/v1/admin/masters/{master.id}/unblock",
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == str(master.id)
    assert payload["previous_status"] == "blocked"
    assert payload["status"] == "active"
    assert payload["changed"] is True
    assert payload["sessions_revoked"] is True

    await db_session.refresh(master)
    first_revoke_marker = master.sessions_revoked_at
    assert master.status == "active"
    assert first_revoke_marker is not None

    second_response = await async_client.post(
        f"/api/v1/admin/masters/{master.id}/unblock",
        headers=headers,
    )

    assert second_response.status_code == 200
    second_payload = second_response.json()
    assert second_payload["previous_status"] == "active"
    assert second_payload["status"] == "active"
    assert second_payload["changed"] is False
    assert second_payload["sessions_revoked"] is False

    await db_session.refresh(master)
    assert master.sessions_revoked_at == first_revoke_marker

    result = await db_session.execute(
        select(AdminAuditLogModel)
        .where(AdminAuditLogModel.action == "unblock_master_account")
        .where(AdminAuditLogModel.target_id == str(master.id))
        .order_by(AdminAuditLogModel.created_at.asc())
    )
    logs = list(result.scalars().all())
    assert len(logs) == 2
    assert logs[0].meta["previous_status"] == "blocked"
    assert logs[0].meta["sessions_revoked"] is True
    assert logs[1].meta["previous_status"] == "active"
    assert logs[1].meta["sessions_revoked"] is False
    assert "email" not in logs[0].meta
    assert "phone" not in logs[0].meta


@pytest.mark.asyncio
async def test_tech_support_cannot_unblock_master(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _admin_auth_headers(db_session, "support@test.com")
    response = await async_client.post(
        f"/api/v1/admin/masters/{uuid4()}/unblock",
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_unblock_uses_database_role_not_token_claim(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    editor = await AdminUserDAO(db_session).get_by_email("editor@test.com")
    assert editor is not None
    session_id = uuid4()
    db_session.add(
        AdminSessionModel(
            id=session_id,
            admin_user_id=editor.id,
            refresh_token_hash=f"test-refresh:{session_id}",
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            status="active",
        )
    )
    await db_session.commit()
    forged_token = create_admin_access_token(editor.id, session_id, "SuperAdmin")

    response = await async_client.post(
        f"/api/v1/admin/masters/{uuid4()}/unblock",
        headers={"Authorization": f"Bearer {forged_token}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"
