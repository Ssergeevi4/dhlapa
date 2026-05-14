from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.organization import OrganizationModel
from db.models.user import UserModel
from db.models.user_device import UserDeviceModel
from services.token import create_access_token


async def _master_auth_headers(
    db_session: AsyncSession,
    *,
    email: str = "device-master@example.com",
) -> tuple[UserModel, dict[str, str]]:
    org = OrganizationModel(id=uuid4(), name=f"Device Org {uuid4()}")
    user = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="Device Master",
        email=email,
        phone=f"79{uuid4().int % 10**9:09d}",
        status="active",
    )
    db_session.add_all([org, user])
    await db_session.commit()

    token = create_access_token(user.id, user.org_id)
    return user, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_register_device_does_not_create_duplicate_token(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    user, headers = await _master_auth_headers(db_session)
    device_token = f"push-token-{uuid4()}"

    first_response = await async_client.post(
        "/api/v1/devices/register",
        json={"device_token": device_token, "platform": "ios"},
        headers=headers,
    )
    second_response = await async_client.post(
        "/api/v1/devices/register",
        json={"device_token": device_token, "platform": "android"},
        headers=headers,
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["id"] == second_response.json()["id"]
    assert second_response.json()["platform"] == "android"

    result = await db_session.execute(
        select(UserDeviceModel).where(UserDeviceModel.device_token == device_token)
    )
    devices = list(result.scalars().all())
    assert len(devices) == 1
    assert devices[0].user_id == user.id
    assert devices[0].org_id == user.org_id
    assert devices[0].platform == "android"
    assert devices[0].is_active is True


@pytest.mark.asyncio
async def test_register_device_rebinds_duplicate_token_to_authorized_user(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    first_user, first_headers = await _master_auth_headers(
        db_session,
        email="first-device-master@example.com",
    )
    second_user, second_headers = await _master_auth_headers(
        db_session,
        email="second-device-master@example.com",
    )
    device_token = f"shared-push-token-{uuid4()}"

    first_response = await async_client.post(
        "/api/v1/devices/register",
        json={"device_token": device_token, "platform": "ios"},
        headers=first_headers,
    )
    second_response = await async_client.post(
        "/api/v1/devices/register",
        json={"device_token": device_token, "platform": "android"},
        headers=second_headers,
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["id"] == second_response.json()["id"]
    assert second_response.json()["user_id"] == str(second_user.id)

    result = await db_session.execute(
        select(UserDeviceModel).where(UserDeviceModel.device_token == device_token)
    )
    devices = list(result.scalars().all())
    assert len(devices) == 1
    assert devices[0].user_id == second_user.id
    assert devices[0].user_id != first_user.id
    assert devices[0].org_id == second_user.org_id
    assert devices[0].is_active is True


@pytest.mark.asyncio
async def test_deactivate_device_marks_current_user_token_inactive(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    user, headers = await _master_auth_headers(
        db_session,
        email="deactivate-device-master@example.com",
    )
    device_token = f"push-token-{uuid4()}"

    register_response = await async_client.post(
        "/api/v1/devices/register",
        json={"device_token": device_token, "platform": "ios"},
        headers=headers,
    )
    deactivate_response = await async_client.post(
        "/api/v1/devices/deactivate",
        json={"device_token": device_token},
        headers=headers,
    )
    repeat_response = await async_client.post(
        "/api/v1/devices/deactivate",
        json={"device_token": device_token},
        headers=headers,
    )

    assert register_response.status_code == 200
    assert deactivate_response.status_code == 204
    assert repeat_response.status_code == 204

    device = await db_session.scalar(
        select(UserDeviceModel).where(UserDeviceModel.device_token == device_token)
    )
    assert device is not None
    assert device.user_id == user.id
    assert device.is_active is False
    assert device.deactivated_at is not None


@pytest.mark.asyncio
async def test_register_device_rejects_invalid_platform(
    async_client: AsyncClient,
    db_session: AsyncSession,
):
    _, headers = await _master_auth_headers(
        db_session,
        email="invalid-platform-master@example.com",
    )
    device_token = f"push-token-{uuid4()}"

    response = await async_client.post(
        "/api/v1/devices/register",
        json={"device_token": device_token, "platform": "web"},
        headers=headers,
    )

    assert response.status_code == 422
    existing = await db_session.scalar(
        select(UserDeviceModel).where(UserDeviceModel.device_token == device_token)
    )
    assert existing is None


@pytest.mark.asyncio
async def test_register_device_requires_authorized_master(
    async_client: AsyncClient,
):
    response = await async_client.post(
        "/api/v1/devices/register",
        json={"device_token": f"push-token-{uuid4()}", "platform": "ios"},
    )

    assert response.status_code == 401
