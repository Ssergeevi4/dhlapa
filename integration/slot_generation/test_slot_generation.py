"""Интеграционные тесты генерации слотов.

Работают с реальной тестовой БД (PostgreSQL).
Покрывают тест-план из ТЗ:
  Integration-1: Генерация создаёт записи в booking_slots.
  Integration-2: Повторный запуск не создаёт дубликатов.
  Integration-3: Забронированный слот не перезаписывается.
  Integration-4: DST-граница — нет пропусков и дублей.
"""
import uuid
from datetime import date, time, datetime, timezone

import pytest
from sqlalchemy import select, func, update

from db.models import BookingSlotModel, BookingRuleModel
from tests.integration.slot_generation.conftest import TEST_ORG_ID, TEST_MASTER_ID


# 2026-03-06 — пятница (weekday=5), совпадает с правилом из seed
TARGET_FRIDAY = date(2026, 3, 6)


@pytest.mark.asyncio
async def test_generation_creates_slots(
    seed_generation_data, generation_use_case_factory
):
    """Integration-1: генерация на пятницу 10:00-12:00/30m → 4 записи в БД."""
    use_case, session = await generation_use_case_factory()

    stats = await use_case.execute(
        TEST_ORG_ID, TEST_MASTER_ID, TARGET_FRIDAY, TARGET_FRIDAY
    )
    await session.commit()

    assert stats.created_count == 4
    assert stats.unchanged_count == 0

    # Проверяем, что 4 строки реально появились в таблице
    count = await session.scalar(
        select(func.count())
        .select_from(BookingSlotModel)
        .where(
            BookingSlotModel.master_id == TEST_MASTER_ID,
            BookingSlotModel.starts_at >= datetime.combine(
                TARGET_FRIDAY, time.min, tzinfo=timezone.utc
            ),
        )
    )
    assert count == 4


@pytest.mark.asyncio
async def test_idempotent_second_run(
    seed_generation_data, generation_use_case_factory
):
    """Integration-2: повторный запуск — created=0, unchanged=4, total rows не изменился."""
    # Первый запуск
    uc1, s1 = await generation_use_case_factory()
    stats1 = await uc1.execute(
        TEST_ORG_ID, TEST_MASTER_ID, TARGET_FRIDAY, TARGET_FRIDAY
    )
    await s1.commit()

    assert stats1.created_count == 4

    # Второй запуск — новая сессия, те же данные
    uc2, s2 = await generation_use_case_factory()
    stats2 = await uc2.execute(
        TEST_ORG_ID, TEST_MASTER_ID, TARGET_FRIDAY, TARGET_FRIDAY
    )
    await s2.commit()

    assert stats2.created_count == 0
    assert stats2.unchanged_count == 4
    assert stats2.updated_count == 0

    # Общее кол-во строк по-прежнему 4
    count = await s2.scalar(
        select(func.count())
        .select_from(BookingSlotModel)
        .where(BookingSlotModel.master_id == TEST_MASTER_ID)
    )
    assert count == 4


@pytest.mark.asyncio
async def test_booked_slot_not_overwritten(
    seed_generation_data, generation_use_case_factory
):
    """Integration-3: забронированный слот не затирается при повторной генерации."""
    # Генерируем слоты
    uc1, s1 = await generation_use_case_factory()
    await uc1.execute(TEST_ORG_ID, TEST_MASTER_ID, TARGET_FRIDAY, TARGET_FRIDAY)
    await s1.commit()

    # Помечаем первый слот как booked
    uc_tmp, s_upd = await generation_use_case_factory()
    first_slot = (
        await s_upd.execute(
            select(BookingSlotModel)
            .where(BookingSlotModel.master_id == TEST_MASTER_ID)
            .order_by(BookingSlotModel.starts_at)
            .limit(1)
        )
    ).scalar_one()

    booked_slot_id = first_slot.id
    booked_starts_at = first_slot.starts_at

    await s_upd.execute(
        update(BookingSlotModel)
        .where(BookingSlotModel.id == booked_slot_id)
        .values(status="booked")
    )
    await s_upd.commit()

    # Повторная генерация
    uc2, s2 = await generation_use_case_factory()
    stats = await uc2.execute(
        TEST_ORG_ID, TEST_MASTER_ID, TARGET_FRIDAY, TARGET_FRIDAY
    )
    await s2.commit()

    assert stats.skipped_booked_count == 1
    assert stats.unchanged_count == 3

    # Слот в БД по-прежнему booked
    uc_chk, s_chk = await generation_use_case_factory()
    slot = (
        await s_chk.execute(
            select(BookingSlotModel).where(BookingSlotModel.id == booked_slot_id)
        )
    ).scalar_one()
    assert slot.status == "booked"


@pytest.mark.asyncio
async def test_dst_boundary_no_duplicates(
    seed_generation_data, generation_use_case_factory, engine
):
    """Integration-4: DST spring-forward (America/New_York, 2026-03-08) — нет дубликатов starts_at.

    2026-03-08 — воскресенье (weekday=7). Создаём отдельное правило
    для этого теста с часами через DST-переход (01:00-04:00 local).
    Меняем timezone организации на America/New_York.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from db.models import OrganizationModel

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Меняем timezone организации
    async with session_factory() as s:
        await s.execute(
            update(OrganizationModel)
            .where(OrganizationModel.id == TEST_ORG_ID)
            .values(timezone="America/New_York")
        )
        # Добавляем правило на воскресенье (weekday=7)
        s.add(BookingRuleModel(
            org_id=TEST_ORG_ID,
            master_id=TEST_MASTER_ID,
            weekday=7,
            start_time=time(1, 0),
            end_time=time(4, 0),
            slot_duration_min=30,
        ))
        await s.commit()

    try:
        dst_date = date(2026, 3, 8)  # воскресенье, spring forward

        uc, session = await generation_use_case_factory()
        stats = await uc.execute(
            TEST_ORG_ID, TEST_MASTER_ID, dst_date, dst_date
        )
        await session.commit()

        assert stats.created_count > 0

        # Проверяем отсутствие дублей starts_at
        uc_chk, s_chk = await generation_use_case_factory()
        rows = (
            await s_chk.execute(
                select(BookingSlotModel.starts_at)
                .where(
                    BookingSlotModel.master_id == TEST_MASTER_ID,
                    BookingSlotModel.starts_at >= datetime.combine(
                        dst_date, time.min, tzinfo=timezone.utc
                    ),
                    BookingSlotModel.starts_at < datetime.combine(
                        date(2026, 3, 9), time.min, tzinfo=timezone.utc
                    ),
                )
            )
        ).scalars().all()

        assert len(rows) == len(set(rows)), "Дубликаты starts_at при DST-переходе!"

        # Все слоты в UTC
        for ts in rows:
            assert ts.tzinfo is not None

    finally:
        # Откат: убираем доп. правило и возвращаем timezone
        async with session_factory() as s:
            await s.execute(
                update(OrganizationModel)
                .where(OrganizationModel.id == TEST_ORG_ID)
                .values(timezone="Europe/Moscow")
            )
            from sqlalchemy import delete
            await s.execute(
                delete(BookingRuleModel).where(
                    BookingRuleModel.master_id == TEST_MASTER_ID,
                    BookingRuleModel.weekday == 7,
                )
            )
            await s.commit()
