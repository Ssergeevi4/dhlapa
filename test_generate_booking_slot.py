"""Unit-тесты для GenerateBookingSlotsUseCase.

Паттерн: Fake DAO (in-memory), без обращения к БД.
Покрывает тест-план из ТЗ:
  Unit-1: Генерация на день с одним правилом и без исключений.
  Unit-2: Конвертация timezone → UTC корректна.
"""
import uuid
from datetime import date, time, datetime, timezone

import pytest

from dto.booking_rule import BookingRuleDTO
from dto.booking_exception import BookingExceptionDTO
from dto.generation_stats import GenerationStatsDTO
from use_cases.generate_booking_slot import GenerateBookingSlotsUseCase

pytestmark = pytest.mark.no_db


# ─── константы ───────────────────────────────────────────────
ORG_ID = uuid.uuid4()
MASTER_ID = uuid.uuid4()
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ─── Fake DAO-и ──────────────────────────────────────────────
class FakeOrganizationDAO:
    def __init__(self, tz: str = "Europe/Moscow"):
        self._tz = tz

    async def get_by_id(self, org_id):
        class Org:
            id = org_id
            timezone = self._tz
        return Org()


class FakeBookingRuleDAO:
    def __init__(self, rules: list[BookingRuleDTO] | None = None):
        self._rules = rules or []

    async def get_all(self, org_id, master_id):
        return self._rules


class FakeBookingExceptionDAO:
    def __init__(self, exceptions: list[BookingExceptionDTO] | None = None):
        self._exceptions = exceptions or []

    async def get_by_date_range(self, org_id, master_id, start_date, end_date):
        return [
            e for e in self._exceptions
            if start_date <= e.date <= end_date
        ]


class FakeBookingSlotDAO:
    """Хранит аргументы, переданные в upsert, для проверки в тестах."""

    def __init__(self, existing: list | None = None):
        self._existing = existing or []
        self.upserted: list[dict] = []

    async def get_existing_slots_in_range(self, master_id, starts_after, ends_before):
        return self._existing

    async def bulk_upsert_free_slots(self, slots_data, org_id, master_id):
        self.upserted.extend(slots_data)


# ─── хелпер для создания правила ─────────────────────────────
def make_rule(
    weekday: int,
    start: time,
    end: time,
    duration: int = 30,
) -> BookingRuleDTO:
    return BookingRuleDTO(
        id=uuid.uuid4(),
        org_id=ORG_ID,
        master_id=MASTER_ID,
        weekday=weekday,
        start_time=start,
        end_time=end,
        slot_duration_min=duration,
        created_at=NOW,
        updated_at=NOW,
    )


def make_exception(
    exc_date: date,
    start: time,
    end: time,
    kind: str = "unavailable",
) -> BookingExceptionDTO:
    return BookingExceptionDTO(
        id=uuid.uuid4(),
        org_id=ORG_ID,
        master_id=MASTER_ID,
        date=exc_date,
        start_time=start,
        end_time=end,
        kind=kind,
        created_at=NOW,
        updated_at=NOW,
    )


# ─── Тесты ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_generate_one_rule_no_exceptions():
    """Unit-1: Генерация на один день, одно правило с 10:00-12:00/30m, без исключений.
    
    Ожидаем 4 слота: 10:00, 10:30, 11:00, 11:30
    """
    # 2026-03-06 — пятница (weekday() == 4, weekday_db == 5)
    target_date = date(2026, 3, 6)

    rule = make_rule(weekday=5, start=time(10, 0), end=time(12, 0), duration=30)

    slot_dao = FakeBookingSlotDAO()
    use_case = GenerateBookingSlotsUseCase(
        _booking_rule_dao=FakeBookingRuleDAO([rule]),
        _booking_exception_dao=FakeBookingExceptionDAO(),
        _organization_dao=FakeOrganizationDAO("Europe/Moscow"),
        _booking_slot_dao=slot_dao,
    )

    stats = await use_case.execute(ORG_ID, MASTER_ID, target_date, target_date)

    # Ровно 4 слота создано
    assert stats.created_count == 4
    assert stats.unchanged_count == 0
    assert stats.skipped_booked_count == 0
    assert stats.skipped_reserved_count == 0
    assert stats.updated_count == 0

    # В DAO передано 4 словаря
    assert len(slot_dao.upserted) == 4

    # Все слоты — free, с правильными org_id / master_id
    for s in slot_dao.upserted:
        assert s["status"] == "free"
        assert s["org_id"] == ORG_ID
        assert s["master_id"] == MASTER_ID


@pytest.mark.asyncio
async def test_timezone_conversion_moscow():
    """Unit-2: Europe/Moscow (UTC+3). Локальное 10:00 → UTC 07:00.

    Правило 10:00-12:00 MSK должно дать слоты 07:00-09:00 UTC.
    """
    target_date = date(2026, 3, 6)
    rule = make_rule(weekday=5, start=time(10, 0), end=time(12, 0), duration=30)

    slot_dao = FakeBookingSlotDAO()
    use_case = GenerateBookingSlotsUseCase(
        _booking_rule_dao=FakeBookingRuleDAO([rule]),
        _booking_exception_dao=FakeBookingExceptionDAO(),
        _organization_dao=FakeOrganizationDAO("Europe/Moscow"),
        _booking_slot_dao=slot_dao,
    )

    await use_case.execute(ORG_ID, MASTER_ID, target_date, target_date)

    starts = sorted([s["starts_at"] for s in slot_dao.upserted])
    ends = sorted([s["ends_at"] for s in slot_dao.upserted])

    # Первый слот starts_at = 07:00 UTC
    assert starts[0].hour == 7
    assert starts[0].minute == 0
    assert starts[0].tzinfo == timezone.utc

    # Последний слот ends_at = 09:00 UTC (11:30 + 30min = 12:00 MSK = 09:00 UTC)
    assert ends[-1].hour == 9
    assert ends[-1].minute == 0


@pytest.mark.asyncio
async def test_idempotent_second_run():
    """Unit: повторная генерация на тот же диапазон — created=0, unchanged=4."""
    target_date = date(2026, 3, 6)
    rule = make_rule(weekday=5, start=time(10, 0), end=time(12, 0), duration=30)

    # Имитируем что в БД уже есть 4 слота от прошлого запуска
    class ExistingSlot:
        def __init__(self, starts_at, ends_at, status="free"):
            self.starts_at = starts_at
            self.ends_at = ends_at
            self.status = status

    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Moscow")
    existing = []
    from datetime import timedelta
    current = datetime.combine(target_date, time(10, 0), tzinfo=tz).astimezone(timezone.utc)
    for _ in range(4):
        existing.append(ExistingSlot(
            starts_at=current,
            ends_at=current + timedelta(minutes=30),
        ))
        current += timedelta(minutes=30)

    slot_dao = FakeBookingSlotDAO(existing=existing)
    use_case = GenerateBookingSlotsUseCase(
        _booking_rule_dao=FakeBookingRuleDAO([rule]),
        _booking_exception_dao=FakeBookingExceptionDAO(),
        _organization_dao=FakeOrganizationDAO("Europe/Moscow"),
        _booking_slot_dao=slot_dao,
    )

    stats = await use_case.execute(ORG_ID, MASTER_ID, target_date, target_date)

    assert stats.created_count == 0
    assert stats.unchanged_count == 4
    assert len(slot_dao.upserted) == 0


@pytest.mark.asyncio
async def test_booked_slots_not_overwritten():
    """Unit: слоты со статусом booked не перезаписываются."""
    target_date = date(2026, 3, 6)
    rule = make_rule(weekday=5, start=time(10, 0), end=time(12, 0), duration=30)

    from zoneinfo import ZoneInfo
    from datetime import timedelta

    tz = ZoneInfo("Europe/Moscow")

    class ExistingSlot:
        def __init__(self, starts_at, ends_at, status):
            self.starts_at = starts_at
            self.ends_at = ends_at
            self.status = status

    base = datetime.combine(target_date, time(10, 0), tzinfo=tz).astimezone(timezone.utc)
    existing = [
        # Первый слот — booked
        ExistingSlot(base, base + timedelta(minutes=30), "booked"),
        # Второй — reserved
        ExistingSlot(base + timedelta(minutes=30), base + timedelta(minutes=60), "reserved"),
        # Третий — free
        ExistingSlot(base + timedelta(minutes=60), base + timedelta(minutes=90), "free"),
        # Четвёртый — free
        ExistingSlot(base + timedelta(minutes=90), base + timedelta(minutes=120), "free"),
    ]

    slot_dao = FakeBookingSlotDAO(existing=existing)
    use_case = GenerateBookingSlotsUseCase(
        _booking_rule_dao=FakeBookingRuleDAO([rule]),
        _booking_exception_dao=FakeBookingExceptionDAO(),
        _organization_dao=FakeOrganizationDAO("Europe/Moscow"),
        _booking_slot_dao=slot_dao,
    )

    stats = await use_case.execute(ORG_ID, MASTER_ID, target_date, target_date)

    assert stats.skipped_booked_count == 1
    assert stats.skipped_reserved_count == 1
    assert stats.unchanged_count == 2
    assert stats.created_count == 0
    # booked и reserved НЕ попадают в upsert
    assert len(slot_dao.upserted) == 0


@pytest.mark.asyncio
async def test_unavailable_exception_filters_slots():
    """Unit: unavailable-исключение убирает слоты в указанном диапазоне."""
    target_date = date(2026, 3, 6)
    rule = make_rule(weekday=5, start=time(10, 0), end=time(12, 0), duration=30)
    # Мастер недоступен 10:00-11:00 — должен остаться только 11:00 и 11:30
    exc = make_exception(target_date, time(10, 0), time(11, 0), kind="unavailable")

    slot_dao = FakeBookingSlotDAO()
    use_case = GenerateBookingSlotsUseCase(
        _booking_rule_dao=FakeBookingRuleDAO([rule]),
        _booking_exception_dao=FakeBookingExceptionDAO([exc]),
        _organization_dao=FakeOrganizationDAO("Europe/Moscow"),
        _booking_slot_dao=slot_dao,
    )

    stats = await use_case.execute(ORG_ID, MASTER_ID, target_date, target_date)

    assert stats.created_count == 2
    assert len(slot_dao.upserted) == 2


@pytest.mark.asyncio
async def test_additional_exception_adds_slots():
    """Unit: additional-исключение добавляет дополнительные слоты."""
    target_date = date(2026, 3, 6)
    rule = make_rule(weekday=5, start=time(10, 0), end=time(11, 0), duration=30)
    # Дополнительный выход 14:00-15:00 — ещё 2 слота
    exc = make_exception(target_date, time(14, 0), time(15, 0), kind="additional")

    slot_dao = FakeBookingSlotDAO()
    use_case = GenerateBookingSlotsUseCase(
        _booking_rule_dao=FakeBookingRuleDAO([rule]),
        _booking_exception_dao=FakeBookingExceptionDAO([exc]),
        _organization_dao=FakeOrganizationDAO("Europe/Moscow"),
        _booking_slot_dao=slot_dao,
    )

    stats = await use_case.execute(ORG_ID, MASTER_ID, target_date, target_date)

    # 2 из правила (10:00, 10:30) + 2 из additional (14:00, 14:30)
    assert stats.created_count == 4
    assert len(slot_dao.upserted) == 4


@pytest.mark.asyncio
async def test_dst_boundary_no_gaps():
    """Unit: на границе DST (America/New_York, 2026-03-08) нет дублей UTC.

    Spring forward: 2:00 AM → 3:00 AM.
    Правило 01:00-04:00 local → 6 wall-clock кандидатов, но 2 пары
    коллапсируют в одинаковый UTC. Итого 4 уникальных слота.
    """
    target_date = date(2026, 3, 8)  # DST spring forward для America/New_York
    # weekday: воскресенье → weekday() == 6, weekday_db == 7
    rule = make_rule(weekday=7, start=time(1, 0), end=time(4, 0), duration=30)

    slot_dao = FakeBookingSlotDAO()
    use_case = GenerateBookingSlotsUseCase(
        _booking_rule_dao=FakeBookingRuleDAO([rule]),
        _booking_exception_dao=FakeBookingExceptionDAO(),
        _organization_dao=FakeOrganizationDAO("America/New_York"),
        _booking_slot_dao=slot_dao,
    )

    stats = await use_case.execute(ORG_ID, MASTER_ID, target_date, target_date)

    # 4 уникальных UTC-слота (2 дубля отброшены)
    starts = [s["starts_at"] for s in slot_dao.upserted]
    assert len(starts) == len(set(starts)), "Дубли starts_at при DST-переходе"
    assert stats.created_count == 4

    # Все слоты в UTC
    for s in slot_dao.upserted:
        assert s["starts_at"].tzinfo == timezone.utc
