import asyncio

import pytest
from datetime import datetime, timedelta
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import uuid4

from db.daos.subscription_dao import SubscriptionDAO
from db.models.organization import OrganizationModel
from db.models.user import UserModel
from domain.entities.subscription import Subscription, Plan
from services.token import create_access_token
from use_cases.subscription import GetSubscriptionUseCase


async def _master_auth_headers(db_session: AsyncSession) -> tuple[UserModel, dict[str, str]]:
    org = OrganizationModel(id=uuid4(), name=f"Subscription Test Org {uuid4()}")
    user = UserModel(
        id=uuid4(),
        org_id=org.id,
        full_name="Subscription Test Master",
        email=f"subscription-{uuid4()}@example.com",
        phone=f"79{str(uuid4().int)[:9]}",
        status="active",
    )
    db_session.add_all([org, user])
    await db_session.commit()
    token = create_access_token(user.id, org.id)
    return user, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_1_trial_to_limited(async_client: AsyncClient, db_session: AsyncSession):
    """1. Trial → Limited по времени"""
    user, _ = await _master_auth_headers(db_session)
    dao = SubscriptionDAO(db_session)
    sub = Subscription(
        user_id=user.id,
        current_period_start=datetime.utcnow(),
        current_period_end=datetime.utcnow() - timedelta(days=1),
    )
    await dao.save(sub)
    uc = GetSubscriptionUseCase(dao)
    entitlements = await uc.execute(user.id)
    assert entitlements.plan == Plan.LIMITED


@pytest.mark.asyncio
async def test_2_validate_receipt_to_active(async_client: AsyncClient, db_session: AsyncSession):
    """2. Успешная валидация receipt переводит в Active"""
    _, headers = await _master_auth_headers(db_session)
    response = await async_client.post(
        "/api/v1/me/subscription-receipt-validations",
        json={"platform": "Acquiring", "receipt": "valid_receipt_123"},
        headers=headers,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["plan"] == "active"


@pytest.mark.asyncio
async def test_3_idempotency(async_client: AsyncClient, db_session: AsyncSession):
    """3. Повторный receipt не удваивает период (идемпотентность)"""
    _, headers = await _master_auth_headers(db_session)
    # первый запрос
    r1 = await async_client.post(
        "/api/v1/me/subscription-receipt-validations",
        json={"platform": "Acquiring", "receipt": "same_receipt"},
        headers=headers,
    )
    assert r1.status_code == 200, r1.json()
    # второй запрос
    response = await async_client.post(
        "/api/v1/me/subscription-receipt-validations",
        json={"platform": "Acquiring", "receipt": "same_receipt"},
        headers=headers,
    )
    assert response.status_code == 409, response.json()  # ReceiptAlreadyProcessed


@pytest.mark.asyncio
async def test_4_limited_blocks_write(async_client: AsyncClient, db_session: AsyncSession):
    """4. Limited: оплата разрешена даже в limited-режиме (ТЗ п. 3.2)"""
    user, headers = await _master_auth_headers(db_session)
    dao = SubscriptionDAO(db_session)
    sub = Subscription(
        user_id=user.id,
        current_period_start=datetime.utcnow(),
        current_period_end=datetime.utcnow() - timedelta(days=1),
    )
    await dao.save(sub)
    await db_session.commit()  # commit so the router's session can see it

    # Limited пользователь МОЖЕТ оформить оплату (ТЗ п. 3.2)
    response = await async_client.post(
        "/api/v1/me/subscription-receipt-validations",
        json={"platform": "Acquiring", "receipt": "test_4_limited_receipt"},
        headers=headers,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["plan"] == "active"


@pytest.mark.asyncio
async def test_5_promo_concurrency(async_client: AsyncClient, db_session: AsyncSession):
    """5. Промокоды: конкурентные запросы не обходят лимиты"""
    _, headers = await _master_auth_headers(db_session)
    # тест имитирует два одновременных запроса
    responses = await asyncio.gather(
        async_client.post(
            "/api/v1/me/promo-code-redemptions",
            json={"code": "TESTPROMO"},
            headers=headers,
        ),
        async_client.post(
            "/api/v1/me/promo-code-redemptions",
            json={"code": "TESTPROMO"},
            headers=headers,
        ),
        return_exceptions=True,
    )
    # один из запросов должен пройти, второй — упасть на лимит (по ТЗ)
    success = sum(
        1 for r in responses
        if not isinstance(r, Exception) and r.status_code == 200
    )
    assert success == 1, [(r.status_code, r.json()) if not isinstance(r, Exception) else r for r in responses]


@pytest.mark.asyncio
async def test_6_admin_grant_limit(async_client: AsyncClient):
    """6. Admin grants: ограничение days <= 365 + audit"""
    login_response = await async_client.post(
        "/api/v1/auth/admin/login",
        json={"email": "superadmin@test.com", "password": "testpass123"},
    )
    assert login_response.status_code == 200, login_response.json()
    access_token = login_response.json()["access_token"]
    response = await async_client.post(
        f"/api/v1/admin/masters/{uuid4()}/subscription-grants",
        json={"days": 400, "reason": "test"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "INVALID_GRANT_DAYS"
