from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.daos import AdminUserDAO
from db.models.admin_audit_log import AdminAuditLogModel
from db.models.admin_session import AdminSessionModel
from db.models.client import ClientModel
from db.models.client_owner_history import ClientOwnerHistoryModel
from db.models.organization import OrganizationModel
from db.models.user import UserModel
from services.token import create_access_token, create_admin_access_token


def _master_headers(user: UserModel) -> dict[str, str]:
    token = create_access_token(user.id, user.org_id)
    return {"Authorization": f"Bearer {token}"}


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
async def test_transfer_owner_updates_client_and_logs_consistent_history(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    org = OrganizationModel(id=uuid4(), name="Transfer Clinic")
    owner = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="Current Owner",
        email="current-owner-history@example.com",
        phone="79003000001",
        status="active",
    )
    new_owner = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="New Owner",
        email="new-owner-history@example.com",
        phone="79003000002",
        status="active",
    )
    client = ClientModel(
        id=uuid4(),
        org_id=org.id,
        owner_user_id=owner.id,
        full_name="Transferred Client",
        phone="79003000003",
    )
    db_session.add_all([org, owner, new_owner])
    await db_session.flush()
    db_session.add(client)
    await db_session.commit()

    response = await async_client.post(
        f"/api/v1/clients/{client.id}/transfer-owner",
        json={"new_owner_user_id": str(new_owner.id)},
        headers=_master_headers(owner),
    )

    assert response.status_code == 200
    assert response.json()["owner_user_id"] == str(new_owner.id)

    await db_session.refresh(client)
    assert client.owner_user_id == new_owner.id

    history_result = await db_session.execute(
        select(ClientOwnerHistoryModel).where(
            ClientOwnerHistoryModel.client_id == client.id
        )
    )
    history = history_result.scalar_one()
    assert history.org_id == org.id
    assert history.from_owner_user_id == owner.id
    assert history.to_owner_user_id == new_owner.id
    assert history.changed_by_user_id == owner.id
    assert history.changed_at is not None

    read_response = await async_client.get(
        f"/api/v1/clients/{client.id}/owner-history",
        headers=_master_headers(owner),
    )

    assert read_response.status_code == 200
    payload = read_response.json()
    assert len(payload) == 1
    assert payload[0]["from_owner_user_id"] == str(owner.id)
    assert payload[0]["to_owner_user_id"] == str(new_owner.id)
    assert payload[0]["changed_by_user_id"] == str(owner.id)
    assert payload[0]["changed_at"] is not None


@pytest.mark.asyncio
async def test_owner_history_is_filtered_by_tenant_for_master_and_support(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    org = OrganizationModel(id=uuid4(), name="History Tenant Clinic")
    other_org = OrganizationModel(id=uuid4(), name="Other Tenant Clinic")
    owner = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="Tenant Owner",
        email="tenant-owner-history@example.com",
        phone="79004000001",
        status="active",
    )
    new_owner = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="Tenant New Owner",
        email="tenant-new-owner-history@example.com",
        phone="79004000002",
        status="active",
    )
    other_user = UserModel(
        id=uuid4(),
        org_id=other_org.id,
        full_name="Other Tenant User",
        email="other-tenant-history@example.com",
        phone="79004000003",
        status="active",
    )
    client = ClientModel(
        id=uuid4(),
        org_id=org.id,
        owner_user_id=new_owner.id,
        full_name="Tenant Filter Client",
    )
    changed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.add_all([org, other_org, owner, new_owner, other_user])
    await db_session.flush()
    db_session.add(client)
    await db_session.flush()
    db_session.add_all(
        [
            ClientOwnerHistoryModel(
                client_id=client.id,
                org_id=org.id,
                from_owner_user_id=owner.id,
                to_owner_user_id=new_owner.id,
                changed_by_user_id=owner.id,
                changed_at=changed_at,
            ),
            ClientOwnerHistoryModel(
                client_id=client.id,
                org_id=other_org.id,
                from_owner_user_id=owner.id,
                to_owner_user_id=new_owner.id,
                changed_by_user_id=owner.id,
                changed_at=changed_at + timedelta(minutes=1),
            ),
        ]
    )
    await db_session.commit()

    owner_response = await async_client.get(
        f"/api/v1/clients/{client.id}/owner-history",
        headers=_master_headers(owner),
    )

    assert owner_response.status_code == 200
    owner_payload = owner_response.json()
    assert len(owner_payload) == 1
    assert owner_payload[0]["org_id"] == str(org.id)

    other_response = await async_client.get(
        f"/api/v1/clients/{client.id}/owner-history",
        headers=_master_headers(other_user),
    )

    assert other_response.status_code == 404

    support_headers = await _admin_auth_headers(db_session, "support@test.com")
    support_response = await async_client.get(
        f"/api/v1/admin/clients/{client.id}/owner-history",
        headers=support_headers,
    )

    assert support_response.status_code == 200
    support_payload = support_response.json()
    assert len(support_payload) == 1
    assert support_payload[0]["org_id"] == str(org.id)

    audit_result = await db_session.execute(
        select(AdminAuditLogModel).where(
            AdminAuditLogModel.action == "admin_client_owner_history_viewed",
            AdminAuditLogModel.target_id == str(client.id),
        )
    )
    audit_log = audit_result.scalar_one()
    assert audit_log.meta["items"] == 1
    assert audit_log.meta["role"] == "TechSupport"
