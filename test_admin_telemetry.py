from datetime import date, timedelta
from uuid import uuid4

import pytest

from dto.admin_telemetry import DauEntryDTO, MauEntryDTO, TelemetryMetricsDTO, TopFeatureDTO
from exceptions.admin_telemetry import AdminTelemetryDateRangeError, AdminTelemetryOverRangeError
from use_cases.admin_telemetry import GetTelemetryMetricsUseCase


class FakeMetricsDAO:
    def __init__(
        self,
        *,
        dau: list[tuple] | None = None,
        mau: list[tuple] | None = None,
        top_features: list[tuple] | None = None,
    ):
        self._dau = dau or []
        self._mau = mau or []
        self._top_features = top_features or []

    async def get_dau(self, date_from, date_to):
        return self._dau

    async def get_mau(self, date_from, date_to):
        return self._mau

    async def get_top_features(self, date_from, date_to):
        return self._top_features


def _make_use_case(dao=None) -> GetTelemetryMetricsUseCase:
    return GetTelemetryMetricsUseCase(dao or FakeMetricsDAO())


APR_1 = date(2026, 4, 1)
APR_29 = date(2026, 4, 29)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_raises_when_date_from_after_date_to():
    with pytest.raises(AdminTelemetryDateRangeError):
        await _make_use_case().execute(date_from=APR_29, date_to=APR_1)


@pytest.mark.asyncio
async def test_raises_when_range_exceeds_180_days():
    date_from = date(2026, 1, 1)
    date_to = date_from + timedelta(days=181)

    with pytest.raises(AdminTelemetryOverRangeError):
        await _make_use_case().execute(date_from=date_from, date_to=date_to)


@pytest.mark.asyncio
async def test_same_date_is_valid():
    result = await _make_use_case().execute(date_from=APR_1, date_to=APR_1)

    assert isinstance(result, TelemetryMetricsDTO)


@pytest.mark.asyncio
async def test_exactly_180_days_is_valid():
    date_from = date(2026, 1, 1)
    date_to = date_from + timedelta(days=180)

    result = await _make_use_case().execute(date_from=date_from, date_to=date_to)

    assert isinstance(result, TelemetryMetricsDTO)


# ---------------------------------------------------------------------------
# DTO mapping — dau
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dau_tuples_mapped_to_dto():
    dao = FakeMetricsDAO(dau=[
        (date(2026, 4, 1), 5),
        (date(2026, 4, 2), 3),
    ])

    result = await _make_use_case(dao).execute(date_from=APR_1, date_to=APR_29)

    assert len(result.dau) == 2
    assert result.dau[0] == DauEntryDTO(day=date(2026, 4, 1), users=5)
    assert result.dau[1] == DauEntryDTO(day=date(2026, 4, 2), users=3)


@pytest.mark.asyncio
async def test_dau_empty_when_dao_returns_empty():
    result = await _make_use_case(FakeMetricsDAO(dau=[])).execute(date_from=APR_1, date_to=APR_29)

    assert result.dau == []


# ---------------------------------------------------------------------------
# DTO mapping — mau
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mau_tuples_mapped_to_dto_with_year_month_string():
    # DAO возвращает date из date_trunc('month'), use case берёт str[:7]
    dao = FakeMetricsDAO(mau=[
        (date(2026, 4, 1), 12),
        (date(2026, 5, 1), 7),
    ])

    result = await _make_use_case(dao).execute(date_from=APR_1, date_to=APR_29)

    assert result.mau[0] == MauEntryDTO(month="2026-04", users=12)
    assert result.mau[1] == MauEntryDTO(month="2026-05", users=7)


# ---------------------------------------------------------------------------
# DTO mapping — top_features
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_top_features_tuples_mapped_to_dto():
    dao = FakeMetricsDAO(top_features=[
        ("pdf_export", 100, 40),
        ("sms_remind", 50, 20),
    ])

    result = await _make_use_case(dao).execute(date_from=APR_1, date_to=APR_29)

    assert len(result.top_features) == 2
    assert result.top_features[0] == TopFeatureDTO(feature_code="pdf_export", events_count=100, users_count=40)
    assert result.top_features[1] == TopFeatureDTO(feature_code="sms_remind", events_count=50, users_count=20)


@pytest.mark.asyncio
async def test_top_features_empty_when_dao_returns_empty():
    result = await _make_use_case().execute(date_from=APR_1, date_to=APR_29)

    assert result.top_features == []
