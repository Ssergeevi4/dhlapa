from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.daos import AdminUserDAO
from db.models.admin_audit_log import AdminAuditLogModel
from db.models.admin_session import AdminSessionModel
from db.models.notification_outbox import NotificationOutboxModel
from db.models.organization import OrganizationModel
from db.models.subscription import SubscriptionModel
from db.models.user import UserModel
from db.models.user_device import UserDeviceModel
from services.token import create_admin_access_token, decode_token
from config.settings import settings


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
async def test_support_can_filter_masters_without_extra_fields(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    now = datetime.utcnow()
    org = OrganizationModel(id=uuid4(), name="Alpha Support Clinic")
    other_org = OrganizationModel(id=uuid4(), name="Beta Support Clinic")
    db_session.add_all([org, other_org])
    await db_session.flush()

    target = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="Alice Support",
        email="alice.support@example.com",
        phone="79000000001",
        status="active",
        trial_ends_at=now + timedelta(days=7),
        last_seen_at=now - timedelta(hours=2),
    )
    db_session.add_all(
        [
            target,
            UserModel(
                id=uuid4(),
                org_id=org.id,
                full_name="Blocked Master",
                email="blocked.support@example.com",
                phone="79000000002",
                status="blocked",
                last_seen_at=None,
            ),
            UserModel(
                id=uuid4(),
                org_id=other_org.id,
                full_name="Other Master",
                email="other.support@example.com",
                phone="79000000003",
                status="active",
                last_seen_at=now,
            ),
        ]
    )
    await db_session.commit()

    headers = await _admin_auth_headers(db_session, "support@test.com")
    response = await async_client.get(
        "/api/v1/admin/masters",
        params={
            "email": "alice.support",
            "phone": "0001",
            "org": "Alpha",
            "status": "active",
            "has_activity": "true",
            "sort_by": "email",
            "sort_order": "asc",
            "page": 1,
            "size": 10,
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["page"] == 1
    assert payload["size"] == 10

    item = payload["items"][0]
    assert item["id"] == str(target.id)
    assert item["email"] == "alice.support@example.com"
    assert item["phone"] == "79000000001"
    assert item["org_name"] == "Alpha Support Clinic"
    assert item["subscription_plan"] == "trial"

    leaked_keys = {
        "password_hash",
        "sessions_revoked_at",
        "session_version",
        "last_receipt_encrypted",
        "last_receipt",
    }
    assert leaked_keys.isdisjoint(item.keys())


@pytest.mark.asyncio
async def test_master_list_rejects_content_editor(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _admin_auth_headers(db_session, "editor@test.com")
    response = await async_client.get("/api/v1/admin/masters", headers=headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_superadmin_can_issue_impersonation_token_audits_and_notifies(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    org = OrganizationModel(id=uuid4(), name="Impersonation Clinic")
    master = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="Impersonated Master",
        email="impersonated-master@example.com",
        phone="79990000001",
        status="active",
    )
    device = UserDeviceModel(
        id=uuid4(),
        user_id=master.id,
        org_id=org.id,
        platform="ios",
        device_token="impersonation-device-token",
        is_active=True,
    )
    db_session.add_all([org, master])
    await db_session.flush()
    db_session.add(device)
    await db_session.commit()

    superadmin = await AdminUserDAO(db_session).get_by_email("superadmin@test.com")
    assert superadmin is not None
    headers = await _admin_auth_headers(db_session, "superadmin@test.com")
    response = await async_client.post(
        f"/api/v1/admin/masters/{master.id}/impersonation-token",
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "bearer"
    assert payload["actor_admin_id"] == str(superadmin.id)
    assert payload["original_user_id"] == str(master.id)
    assert payload["notification_devices_seen"] == 1
    assert payload["notification_created"] == 1

    claims = decode_token(payload["access_token"], required_aud="access")
    assert claims["sub"] == str(master.id)
    assert claims["org_id"] == str(org.id)
    assert claims["impersonation"] is True
    assert claims["actor_admin_id"] == str(superadmin.id)
    assert claims["original_user_id"] == str(master.id)
    assert claims["impersonation_audit_id"] == payload["audit_id"]
    assert claims["exp"] - claims["iat"] <= settings.IMPERSONATION_TOKEN_EXPIRE_MINUTES * 60

    audit_result = await db_session.execute(
        select(AdminAuditLogModel).where(
            AdminAuditLogModel.id == UUID(payload["audit_id"])
        )
    )
    audit_log = audit_result.scalar_one()
    assert audit_log.action == "admin_impersonation_started"
    assert audit_log.admin_user_id == superadmin.id
    assert audit_log.target_id == str(master.id)
    assert audit_log.meta["actor_role"] == "SuperAdmin"
    assert audit_log.meta["actor"]["role"] == "SuperAdmin"
    assert audit_log.meta["original_user_id"] == str(master.id)
    assert audit_log.meta["org_id"] == str(org.id)

    notification_result = await db_session.execute(
        select(NotificationOutboxModel).where(
            NotificationOutboxModel.event_type == "impersonation",
            NotificationOutboxModel.user_id == master.id,
        )
    )
    notification = notification_result.scalar_one()
    assert notification.org_id == org.id
    assert notification.payload["audit_id"] == payload["audit_id"]
    assert notification.payload["admin_user_id"] == str(superadmin.id)


@pytest.mark.asyncio
async def test_content_editor_cannot_issue_impersonation_token(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    headers = await _admin_auth_headers(db_session, "editor@test.com")
    response = await async_client.post(
        f"/api/v1/admin/masters/{uuid4()}/impersonation-token",
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_master_list_pagination_sorting_is_stable(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    now = datetime.utcnow()
    org = OrganizationModel(id=uuid4(), name="Stable Sort Clinic")
    db_session.add(org)
    await db_session.flush()

    user_ids = [
        UUID("00000000-0000-0000-0000-000000000101"),
        UUID("00000000-0000-0000-0000-000000000102"),
        UUID("00000000-0000-0000-0000-000000000103"),
    ]
    for index, user_id in enumerate(user_ids):
        db_session.add(
            UserModel(
                id=user_id,
                org_id=org.id,
                full_name=f"Stable Master {index}",
                email=f"stable-{index}@example.com",
                phone=f"7911000000{index}",
                status="active",
                last_seen_at=now,
            )
        )

    db_session.add(
        SubscriptionModel(
            user_id=user_ids[0],
            status="active",
            current_period_start=now - timedelta(days=1),
            current_period_end=now + timedelta(days=30),
            auto_renew=True,
        )
    )
    await db_session.commit()

    headers = await _admin_auth_headers(db_session, "support@test.com")
    first_page = await async_client.get(
        "/api/v1/admin/masters",
        params={
            "org_id": str(org.id),
            "sort_by": "status",
            "sort_order": "asc",
            "page": 1,
            "size": 2,
        },
        headers=headers,
    )
    second_page = await async_client.get(
        "/api/v1/admin/masters",
        params={
            "org_id": str(org.id),
            "sort_by": "status",
            "sort_order": "asc",
            "page": 2,
            "size": 2,
        },
        headers=headers,
    )

    assert first_page.status_code == 200
    assert second_page.status_code == 200
    assert [item["id"] for item in first_page.json()["items"]] == [
        str(user_ids[0]),
        str(user_ids[1]),
    ]
    assert [item["id"] for item in second_page.json()["items"]] == [str(user_ids[2])]
