from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.daos import AdminUserDAO
from db.models.admin_audit_log import AdminAuditLogModel
from db.models.admin_session import AdminSessionModel
from db.models.client import ClientModel
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
async def test_admin_clients_list_searches_across_orgs_and_masks_for_support(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    first_org = OrganizationModel(id=uuid4(), name="Global Alpha Clinic")
    second_org = OrganizationModel(id=uuid4(), name="Global Beta Clinic")
    first_owner = UserModel(
        id=uuid4(),
        org_id=first_org.id,
        full_name="Alpha Owner",
        email="alpha-owner-admin-clients@example.com",
        phone="79002000001",
        status="active",
    )
    second_owner = UserModel(
        id=uuid4(),
        org_id=second_org.id,
        full_name="Beta Owner",
        email="beta-owner-admin-clients@example.com",
        phone="79002000002",
        status="active",
    )
    first_client = ClientModel(
        id=uuid4(),
        org_id=first_org.id,
        owner_user_id=first_owner.id,
        full_name="Shared Needle Client",
        phone="79009991122",
        birth_date=date(1985, 5, 1),
        diagnoses="Sensitive diagnosis",
        is_flagged_bad=True,
        flag_comment="Sensitive flag",
    )
    second_client = ClientModel(
        id=uuid4(),
        org_id=second_org.id,
        owner_user_id=second_owner.id,
        full_name="Shared Nail Client",
        phone="79008881122",
        diagnoses="Other diagnosis",
    )
    db_session.add_all([first_org, second_org, first_owner, second_owner])
    await db_session.flush()
    db_session.add_all([first_client, second_client])
    await db_session.commit()

    headers = await _admin_auth_headers(db_session, "support@test.com")
    response = await async_client.get(
        "/api/v1/admin/clients",
        params={"search": "Shared", "sort_by": "full_name", "sort_order": "asc"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    ids = {item["id"] for item in payload["items"]}
    assert str(first_client.id) in ids
    assert str(second_client.id) in ids

    item = next(item for item in payload["items"] if item["id"] == str(first_client.id))
    assert item["org_name"] == "Global Alpha Clinic"
    assert item["phone"].endswith("1122")
    assert item["phone"] != "79009991122"
    assert item["birth_date"] is None
    assert item["diagnoses"] is None
    assert item["flag_comment"] is None
    assert item["is_masked"] is True
    assert "diagnoses" in item["masked_fields"]


@pytest.mark.asyncio
async def test_admin_client_card_superadmin_gets_masked_data_and_audits(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    org = OrganizationModel(id=uuid4(), name="Audit Client Clinic")
    owner = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="Audit Owner",
        email="audit-owner-admin-clients@example.com",
        phone="79002000003",
        status="active",
    )
    client = ClientModel(
        id=uuid4(),
        org_id=org.id,
        owner_user_id=owner.id,
        full_name="Audit Client",
        phone="79007776655",
        birth_date=date(1991, 7, 9),
        diagnoses="Full diagnosis",
        allergies="Full allergy",
        contraindications="Full contra",
        notes="Full note",
        flag_comment="Full flag",
        is_flagged_bad=True,
    )
    db_session.add_all([org, owner])
    await db_session.flush()
    db_session.add(client)
    await db_session.commit()

    headers = await _admin_auth_headers(db_session, "superadmin@test.com")
    response = await async_client.get(
        f"/api/v1/admin/clients/{client.id}",
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["phone"].endswith("6655")
    assert payload["phone"] != "79007776655"
    assert payload["birth_date"] is None
    assert payload["diagnoses"] is None
    assert payload["is_masked"] is True

    result = await db_session.execute(
        select(AdminAuditLogModel)
        .where(AdminAuditLogModel.action == "admin_client_card_viewed")
        .where(AdminAuditLogModel.target_id == str(client.id))
    )
    logs = list(result.scalars().all())
    assert len(logs) == 1
    assert logs[0].meta["org_id"] == str(org.id)
    assert logs[0].meta["role"] == "SuperAdmin"
    assert logs[0].meta["masked"] is True


@pytest.mark.asyncio
async def test_content_editor_cannot_open_admin_clients(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _admin_auth_headers(db_session, "editor@test.com")
    response = await async_client.get("/api/v1/admin/clients", headers=headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_superadmin_restores_deleted_client_inside_retention_and_audits(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    org = OrganizationModel(id=uuid4(), name="Restore Client Clinic")
    owner = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="Restore Owner",
        email="restore-owner-admin-clients@example.com",
        phone="79002000004",
        status="active",
    )
    deleted_at = datetime.now(timezone.utc) - timedelta(days=10)
    client = ClientModel(
        id=uuid4(),
        org_id=org.id,
        owner_user_id=owner.id,
        full_name="Deleted Restore Client",
        phone="79005554433",
        deleted_at=deleted_at,
    )
    db_session.add_all([org, owner])
    await db_session.flush()
    db_session.add(client)
    await db_session.commit()

    headers = await _admin_auth_headers(db_session, "superadmin@test.com")
    response = await async_client.post(
        f"/api/v1/admin/clients/{client.id}/restore",
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(client.id)
    assert payload["deleted_at"] is None

    await db_session.refresh(client)
    assert client.deleted_at is None

    result = await db_session.execute(
        select(AdminAuditLogModel)
        .where(AdminAuditLogModel.action == "admin_client_restored")
        .where(AdminAuditLogModel.target_id == str(client.id))
    )
    logs = list(result.scalars().all())
    assert len(logs) == 1
    assert logs[0].admin_user_id is not None
    assert logs[0].meta["org_id"] == str(org.id)
    assert logs[0].meta["owner_user_id"] == str(owner.id)
    assert logs[0].meta["retention_days"] == 90
    assert "deleted_at" in logs[0].meta
    assert "phone" not in logs[0].meta


@pytest.mark.asyncio
async def test_superadmin_cannot_restore_client_after_retention_window(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    org = OrganizationModel(id=uuid4(), name="Expired Restore Clinic")
    owner = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="Expired Restore Owner",
        email="expired-restore-owner-admin-clients@example.com",
        phone="79002000005",
        status="active",
    )
    deleted_at = datetime.now(timezone.utc) - timedelta(days=91)
    client = ClientModel(
        id=uuid4(),
        org_id=org.id,
        owner_user_id=owner.id,
        full_name="Expired Restore Client",
        deleted_at=deleted_at,
    )
    db_session.add_all([org, owner])
    await db_session.flush()
    db_session.add(client)
    await db_session.commit()

    headers = await _admin_auth_headers(db_session, "superadmin@test.com")
    response = await async_client.post(
        f"/api/v1/admin/clients/{client.id}/restore",
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "CLIENT_RESTORE_WINDOW_EXPIRED_ERROR"

    await db_session.refresh(client)
    assert client.deleted_at == deleted_at

    result = await db_session.execute(
        select(AdminAuditLogModel)
        .where(AdminAuditLogModel.action == "admin_client_restored")
        .where(AdminAuditLogModel.target_id == str(client.id))
    )
    assert list(result.scalars().all()) == []


@pytest.mark.asyncio
async def test_tech_support_cannot_restore_deleted_client(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _admin_auth_headers(db_session, "support@test.com")
    response = await async_client.post(
        f"/api/v1/admin/clients/{uuid4()}/restore",
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"
