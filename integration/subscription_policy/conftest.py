"""Фикстуры для интеграционных тестов subscription policy.

Создаёт в реальной тестовой БД:
  - организацию
  - мастера (пользователя)
и предоставляет factory-фикстуры для DAO и use-case с изолированными сессиями.
"""
import uuid
from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from db.daos.client import ClientDAO
from db.daos.subscription_dao import SubscriptionDAO
from db.models.client import ClientModel
from db.models.organization import OrganizationModel
from db.models.subscription import SubscriptionModel
from db.models.user import UserModel
from use_cases.subscription import GetSubscriptionUseCase


# Фиксированные ID для тестов
TEST_ORG_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
TEST_USER_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


@pytest_asyncio.fixture
async def seed_subscription_data(engine):
    """Seed: организация + мастер без подписки."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        org = OrganizationModel(
            id=TEST_ORG_ID,
            name="Policy Test Org",
            timezone="Europe/Moscow",
        )
        session.add(org)
        await session.flush()

        master = UserModel(
            id=TEST_USER_ID,
            org_id=TEST_ORG_ID,
            full_name="Policy Test Master",
        )
        session.add(master)
        await session.commit()

    yield

    # Cleanup (порядок важен из-за FK)
    async with session_factory() as session:
        await session.execute(
            delete(SubscriptionModel).where(SubscriptionModel.user_id == TEST_USER_ID)
        )
        await session.execute(
            delete(ClientModel).where(ClientModel.owner_user_id == TEST_USER_ID)
        )
        await session.execute(
            delete(UserModel).where(UserModel.id == TEST_USER_ID)
        )
        await session.execute(
            delete(OrganizationModel).where(OrganizationModel.id == TEST_ORG_ID)
        )
        await session.commit()


@pytest_asyncio.fixture
async def subscription_dao_factory(engine):
    """Factory: создаёт SubscriptionDAO с новой сессией."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    created_sessions: list[AsyncSession] = []

    async def _create():
        session = session_factory()
        created_sessions.append(session)
        return SubscriptionDAO(session), session

    yield _create

    for session in created_sessions:
        await session.close()


@pytest_asyncio.fixture
async def subscription_use_case_factory(engine):
    """Factory: создаёт GetSubscriptionUseCase с новой сессией."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    created_sessions: list[AsyncSession] = []

    async def _create():
        session = session_factory()
        created_sessions.append(session)
        return GetSubscriptionUseCase(SubscriptionDAO(session)), session

    yield _create

    for session in created_sessions:
        await session.close()


@pytest_asyncio.fixture
async def client_dao_factory(engine):
    """Factory: создаёт ClientDAO с новой сессией."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    created_sessions: list[AsyncSession] = []

    async def _create():
        session = session_factory()
        created_sessions.append(session)
        return ClientDAO(session), session

    yield _create

    for session in created_sessions:
        await session.close()
