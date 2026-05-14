from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dto.device import UserDeviceDTO
from exceptions.device import InvalidDevicePlatformError
from use_cases.device import DeactivateDeviceUseCase, RegisterDeviceUseCase

pytestmark = pytest.mark.no_db


class FakeUserDeviceDAO:
    def __init__(self):
        self.devices_by_token = {}
        self.created_count = 0

    async def upsert(self, *, user_id, org_id, platform, device_token):
        now = datetime.now(timezone.utc)
        device = self.devices_by_token.get(device_token)
        if device is None:
            device = SimpleNamespace(
                id=uuid4(),
                user_id=user_id,
                org_id=org_id,
                platform=platform,
                device_token=device_token,
                is_active=True,
                deactivated_at=None,
                created_at=now,
                updated_at=now,
            )
            self.devices_by_token[device_token] = device
            self.created_count += 1
        else:
            device.user_id = user_id
            device.org_id = org_id
            device.platform = platform
            device.is_active = True
            device.deactivated_at = None
            device.updated_at = now

        return UserDeviceDTO.model_validate(device)

    async def deactivate(self, *, user_id, device_token):
        device = self.devices_by_token.get(device_token)
        if device is None or device.user_id != user_id or not device.is_active:
            return False

        device.is_active = False
        device.deactivated_at = datetime.now(timezone.utc)
        return True


@pytest.mark.asyncio
async def test_register_device_does_not_create_duplicate_tokens():
    dao = FakeUserDeviceDAO()
    use_case = RegisterDeviceUseCase(dao)
    user_id = uuid4()
    org_id = uuid4()

    first = await use_case.execute(
        user_id=user_id,
        org_id=org_id,
        platform="ios",
        device_token=" token-1 ",
    )
    second = await use_case.execute(
        user_id=user_id,
        org_id=org_id,
        platform="android",
        device_token="token-1",
    )

    assert first.id == second.id
    assert second.platform == "android"
    assert second.device_token == "token-1"
    assert dao.created_count == 1


@pytest.mark.asyncio
async def test_register_device_rebinds_existing_token_to_current_user():
    dao = FakeUserDeviceDAO()
    use_case = RegisterDeviceUseCase(dao)
    first_user_id = uuid4()
    second_user_id = uuid4()
    second_org_id = uuid4()

    await use_case.execute(
        user_id=first_user_id,
        org_id=uuid4(),
        platform="ios",
        device_token="token-1",
    )
    result = await use_case.execute(
        user_id=second_user_id,
        org_id=second_org_id,
        platform="android",
        device_token="token-1",
    )

    assert result.user_id == second_user_id
    assert result.org_id == second_org_id
    assert result.is_active is True
    assert dao.created_count == 1


@pytest.mark.asyncio
async def test_deactivate_device_deactivates_only_current_user_token():
    dao = FakeUserDeviceDAO()
    register_use_case = RegisterDeviceUseCase(dao)
    deactivate_use_case = DeactivateDeviceUseCase(dao)
    owner_id = uuid4()
    other_user_id = uuid4()

    await register_use_case.execute(
        user_id=owner_id,
        org_id=uuid4(),
        platform="ios",
        device_token="token-1",
    )

    other_result = await deactivate_use_case.execute(
        user_id=other_user_id,
        device_token="token-1",
    )
    owner_result = await deactivate_use_case.execute(
        user_id=owner_id,
        device_token="token-1",
    )
    repeat_result = await deactivate_use_case.execute(
        user_id=owner_id,
        device_token="token-1",
    )

    assert other_result.deactivated is False
    assert owner_result.deactivated is True
    assert repeat_result.deactivated is False
    assert dao.devices_by_token["token-1"].is_active is False


@pytest.mark.asyncio
async def test_register_device_rejects_invalid_platform():
    use_case = RegisterDeviceUseCase(FakeUserDeviceDAO())

    with pytest.raises(InvalidDevicePlatformError):
        await use_case.execute(
            user_id=uuid4(),
            org_id=uuid4(),
            platform="web",
            device_token="token-1",
        )
