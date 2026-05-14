"""Фикстуры для интеграционных тестов генерации слотов.

Создаёт в реальной тестовой БД:
  - организацию (с timezone)
  - мастера (пользователя)
  - booking_rule для мастера
и предоставляет factory для создания use-case с чистой сессией.
"""
import uuid
from datetime import time

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from db.models import (
    OrganizationModel,
    UserModel,
    BookingRuleModel,
    BookingSlotModel,
    BookingExceptionModel,
)
from db.daos import (
    BookingRuleDAO,
    BookingExceptionDAO,
    BookingSlotDAO,
    OrganizationDAO,
)
from use_cases.generate_booking_slot import GenerateBookingSlotsUseCase


# Фиксированные ID для тестов
TEST_ORG_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TEST_MASTER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest_asyncio.fixture
async def seed_generation_data(engine):
    """Seed: организация + мастер + одно правило (пт 10:00-12:00, 30m)."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        # Организация
        org = OrganizationModel(
            id=TEST_ORG_ID,
            name="Test Org",
            timezone="Europe/Moscow",
        )
        session.add(org)
        await session.flush()

        # Мастер
        master = UserModel(
            id=TEST_MASTER_ID,
            org_id=TEST_ORG_ID,
            full_name="Test Master",
        )
        session.add(master)
        await session.flush()

        # Правило: пятница 10:00-12:00, слот 30 мин
        rule = BookingRuleModel(
            org_id=TEST_ORG_ID,
            master_id=TEST_MASTER_ID,
            weekday=5,
            start_time=time(10, 0),
            end_time=time(12, 0),
            slot_duration_min=30,
        )
        session.add(rule)
        await session.commit()

    yield

    # Cleanup
    async with session_factory() as session:
        await session.execute(delete(BookingSlotModel).where(
            BookingSlotModel.master_id == TEST_MASTER_ID
        ))
        await session.execute(delete(BookingExceptionModel).where(
            BookingExceptionModel.master_id == TEST_MASTER_ID
        ))
        await session.execute(delete(BookingRuleModel).where(
            BookingRuleModel.master_id == TEST_MASTER_ID
        ))
        await session.execute(delete(UserModel).where(
            UserModel.id == TEST_MASTER_ID
        ))
        await session.execute(delete(OrganizationModel).where(
            OrganizationModel.id == TEST_ORG_ID
        ))
        await session.commit()


@pytest_asyncio.fixture
async def generation_use_case_factory(engine):
    """Factory: создаёт GenerateBookingSlotsUseCase с новой сессией."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    created_sessions: list[AsyncSession] = []

    async def _create():
        session = session_factory()
        created_sessions.append(session)
        return GenerateBookingSlotsUseCase(
            _booking_rule_dao=BookingRuleDAO(session),
            _booking_exception_dao=BookingExceptionDAO(session),
            _organization_dao=OrganizationDAO(session),
            _booking_slot_dao=BookingSlotDAO(session),
        ), session

    yield _create

    for session in created_sessions:
        await session.close()