"""Интеграционные тесты subscription policy.

Работают с реальной тестовой БД (PostgreSQL).
Покрывают:
  Integration-1: Новый пользователь → GetSubscriptionUseCase автоматически создаёт TRIAL.
  Integration-2: Активная подписка в БД → ACTIVE план.
  Integration-3: Просроченная подписка → LIMITED план, can_write=False.
  Integration-4: ClientDAO.count_active считает только не удалённых клиентов.
  Integration-5: Гард require_active_subscription с реальным LIMITED → 403.
  Integration-6: Гард require_client_write_allowed — 5 клиентов в БД → 403.
  Integration-7: Гард require_client_write_allowed — 3 клиента в БД → проходит.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from db.models.client import ClientModel
from domain.entities.subscription import Plan, Subscription
from dto.auth import CurrentUserDTO
from tests.integration.subscription_policy.conftest import TEST_ORG_ID, TEST_USER_ID


# ─── хелперы ─────────────────────────────────────────────────
class FakeRequest:
    class _URL:
        def __init__(self, path):
            self.path = path

    def __init__(self, path: str = "/api/v1/clients/"):
        self.url = self._URL(path)


def make_current_user() -> CurrentUserDTO:
    return CurrentUserDTO(
        id=TEST_USER_ID,
        session_id=uuid.uuid4(),
        email="policy@test.com",
        phone=None,
        full_name="Policy Test Master",
        org_id=TEST_ORG_ID,
        status="active",
    )


def make_limited_subscription() -> Subscription:
    now = datetime.utcnow()
    return Subscription(
        user_id=TEST_USER_ID,
        current_period_start=now - timedelta(days=60),
        current_period_end=now - timedelta(days=1),
        trial_ends_at=now - timedelta(days=30),
    )


# ─── Integration-1: автосоздание trial ───────────────────────

@pytest.mark.asyncio
async def test_new_user_gets_trial_entitlements(
    seed_subscription_data, subscription_use_case_factory
):
    """Нет подписки в БД → use_case создаёт TRIAL автоматически."""
    use_case, session = await subscription_use_case_factory()
    entitlements = await use_case.execute(TEST_USER_ID)
    await session.commit()

    assert entitlements.plan == Plan.TRIAL
    assert entitlements.can_write is True
    assert entitlements.limits is None


# ─── Integration-2: активная подписка → ACTIVE ───────────────

@pytest.mark.asyncio
async def test_active_subscription_returns_active_plan(
    seed_subscription_data, subscription_dao_factory, subscription_use_case_factory
):
    """Подписка с period_end в будущем → ACTIVE план."""
    dao, s_save = await subscription_dao_factory()
    now = datetime.utcnow()
    await dao.save(Subscription(
        user_id=TEST_USER_ID,
        current_period_start=now - timedelta(days=1),
        current_period_end=now + timedelta(days=30),
        trial_ends_at=now - timedelta(days=1),
    ))
    await s_save.commit()

    use_case, _ = await subscription_use_case_factory()
    entitlements = await use_case.execute(TEST_USER_ID)

    assert entitlements.plan == Plan.ACTIVE
    assert entitlements.can_write is True
    assert entitlements.limits is None


# ─── Integration-3: просроченная подписка → LIMITED ──────────

@pytest.mark.asyncio
async def test_expired_subscription_returns_limited_plan(
    seed_subscription_data, subscription_dao_factory, subscription_use_case_factory
):
    """Обе даты в прошлом → LIMITED план, can_write=False."""
    dao, s_save = await subscription_dao_factory()
    await dao.save(make_limited_subscription())
    await s_save.commit()

    use_case, _ = await subscription_use_case_factory()
    entitlements = await use_case.execute(TEST_USER_ID)

    assert entitlements.plan == Plan.LIMITED
    assert entitlements.can_write is False
    assert entitlements.limits == {"max_clients": 5, "max_media_per_client": 2}


# ─── Integration-4: ClientDAO.count_active ───────────────────

@pytest.mark.asyncio
async def test_count_active_counts_only_non_deleted_clients(
    seed_subscription_data, client_dao_factory
):
    """count_active возвращает правильное число; мягко удалённые не считаются."""
    _, s_insert = await client_dao_factory()
    for i in range(3):
        s_insert.add(ClientModel(
            id=uuid.uuid4(),
            org_id=TEST_ORG_ID,
            owner_user_id=TEST_USER_ID,
            full_name=f"Active Client {i}",
        ))
    # Удалённый клиент — не должен попасть в счётчик
    s_insert.add(ClientModel(
        id=uuid.uuid4(),
        org_id=TEST_ORG_ID,
        owner_user_id=TEST_USER_ID,
        full_name="Deleted Client",
        deleted_at=datetime.utcnow() - timedelta(days=1),
    ))
    await s_insert.commit()

    dao2, _ = await client_dao_factory()
    count = await dao2.count_active(TEST_ORG_ID)

    assert count == 3


# ─── Integration-5: гард с реальным LIMITED → 403 ────────────

@pytest.mark.asyncio
async def test_guard_limited_subscription_raises_403(
    seed_subscription_data, subscription_dao_factory, subscription_use_case_factory
):
    """require_active_subscription с реальными LIMITED-entitlements из БД → 403."""
    from api.v1.dependencies.subscription.guards import require_active_subscription

    dao, s_save = await subscription_dao_factory()
    await dao.save(make_limited_subscription())
    await s_save.commit()

    use_case, _ = await subscription_use_case_factory()
    entitlements = await use_case.execute(TEST_USER_ID)

    with pytest.raises(HTTPException) as exc_info:
        await require_active_subscription(
            request=FakeRequest(),
            current_user=make_current_user(),
            entitlements=entitlements,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error_code"] == "SUBSCRIPTION_REQUIRED"


# ─── Integration-6: 5 клиентов → гард блокирует ─────────────

@pytest.mark.asyncio
async def test_guard_client_cap_reached_with_real_count(
    seed_subscription_data, subscription_dao_factory, subscription_use_case_factory, client_dao_factory
):
    """5 активных клиентов в БД + LIMITED план → CLIENT_LIMIT_REACHED."""
    from api.v1.dependencies.subscription.guards import require_client_write_allowed

    dao_sub, s_sub = await subscription_dao_factory()
    await dao_sub.save(make_limited_subscription())
    await s_sub.commit()

    _, s_insert = await client_dao_factory()
    for i in range(5):
        s_insert.add(ClientModel(
            id=uuid.uuid4(),
            org_id=TEST_ORG_ID,
            owner_user_id=TEST_USER_ID,
            full_name=f"Cap Client {i}",
        ))
    await s_insert.commit()

    use_case, _ = await subscription_use_case_factory()
    entitlements = await use_case.execute(TEST_USER_ID)

    client_dao, _ = await client_dao_factory()

    with pytest.raises(HTTPException) as exc_info:
        await require_client_write_allowed(
            request=FakeRequest(),
            entitlements=entitlements,
            current_user=make_current_user(),
            client_dao=client_dao,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error_code"] == "CLIENT_LIMIT_REACHED"


# ─── Integration-7: 3 клиента → гард пропускает ──────────────

@pytest.mark.asyncio
async def test_guard_client_below_cap_passes_with_real_count(
    seed_subscription_data, subscription_dao_factory, subscription_use_case_factory, client_dao_factory
):
    """3 активных клиента в БД + LIMITED план → гард пропускает."""
    from api.v1.dependencies.subscription.guards import require_client_write_allowed

    dao_sub, s_sub = await subscription_dao_factory()
    await dao_sub.save(make_limited_subscription())
    await s_sub.commit()

    _, s_insert = await client_dao_factory()
    for i in range(3):
        s_insert.add(ClientModel(
            id=uuid.uuid4(),
            org_id=TEST_ORG_ID,
            owner_user_id=TEST_USER_ID,
            full_name=f"Below Cap Client {i}",
        ))
    await s_insert.commit()

    use_case, _ = await subscription_use_case_factory()
    entitlements = await use_case.execute(TEST_USER_ID)

    client_dao, _ = await client_dao_factory()

    # Не должно бросать исключение
    await require_client_write_allowed(
        request=FakeRequest(),
        entitlements=entitlements,
        current_user=make_current_user(),
        client_dao=client_dao,
    )
