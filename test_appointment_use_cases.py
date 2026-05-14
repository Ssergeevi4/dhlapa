"""Unit-tests for appointments use-cases."""
from pathlib import Path
import sys
import uuid
import datetime
import decimal

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.append(str(SRC))

from api.v1.schemas.appointment import (
    AppointmentCreateSchema,
    AppointmentPatchSchema,
    AppointmentStatus,
)
from dto.appointment import AppointmentDTO
from exceptions.appointment import AppointmentAlreadyExistsError, AppointmentNotFoundError
from exceptions.client import ClientNotFoundError
from use_cases.appointment import (
    CreateAppointmentUseCase,
    GetAllAppointmentsUseCase,
    GetAppointmentByIdUseCase,
    PatchAppointmentUseCase,
    DeleteAppointmentUseCase,
)


class FakeClientDAO:
    def __init__(self):
        self.clients = {}  # key: (client_id, org_id)

    async def get_by_id(self, client_id: uuid.UUID, org_id: uuid.UUID):
        return self.clients.get((client_id, org_id))


class FakeAppointmentDAO:
    def __init__(self):
        self.appointments = {}  # key: appointment_id
        self.last_get_all_kwargs = None
        self.last_patch_data = None

    async def get_existing_active_appointment(
        self,
        *,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        client_id: uuid.UUID,
        visit_at: datetime.datetime,
    ) -> AppointmentDTO | None:
        for appt in self.appointments.values():
            if (
                appt.org_id == org_id
                and appt.user_id == user_id
                and appt.client_id == client_id
                and appt.visit_at == visit_at
                and appt.deleted_at is None
            ):
                return appt
        return None

    async def create(
        self,
        *,
        org_id: uuid.UUID,
        user_id: uuid.UUID,
        client_id: uuid.UUID,
        visit_at: datetime.datetime,
        procedure_service_id: uuid.UUID | None = None,
        status: str = "scheduled",
        product_purchased: bool = False,
        procedures_desc: str | None = None,
        price: decimal.Decimal | None = None,
        recommendations_common: str | None = None,
        recommendations_product: str | None = None,
        next_visit_at: datetime.datetime | None = None,
        next_visit_plan: str | None = None,
        completion_warned_at: datetime.datetime | None = None,
    ) -> AppointmentDTO:
        now = datetime.datetime.now(datetime.timezone.utc)
        appt = AppointmentDTO(
            id=uuid.uuid4(),
            org_id=org_id,
            client_id=client_id,
            user_id=user_id,
            visit_at=visit_at,
            product_purchased=product_purchased,
            status=status,
            created_at=now,
            updated_at=now,
            procedure_service_id=procedure_service_id,
            procedures_desc=procedures_desc,
            price=price,
            recommendations_common=recommendations_common,
            recommendations_product=recommendations_product,
            next_visit_at=next_visit_at,
            next_visit_plan=next_visit_plan,
            completion_warned_at=completion_warned_at,
            deleted_at=None,
        )
        self.appointments[appt.id] = appt
        return appt

    async def get_by_id(self, appointment_id: uuid.UUID, org_id: uuid.UUID) -> AppointmentDTO | None:
        appt = self.appointments.get(appointment_id)
        if appt and appt.org_id == org_id and appt.deleted_at is None:
            return appt
        return None

    async def get_all(
        self,
        org_id: uuid.UUID,
        date_from: datetime.datetime,
        date_to: datetime.datetime,
        user_id: uuid.UUID | None = None,
    ) -> list[AppointmentDTO]:
        self.last_get_all_kwargs = {
            "org_id": org_id,
            "date_from": date_from,
            "date_to": date_to,
            "user_id": user_id,
        }

        result = []
        for appt in self.appointments.values():
            if appt.org_id != org_id or appt.deleted_at is not None:
                continue
            if user_id and appt.user_id != user_id:
                continue
            if not (date_from <= appt.visit_at <= date_to):
                continue
            result.append(appt)

        return sorted(result, key=lambda a: a.visit_at, reverse=True)

    async def patch(self, appointment_id: uuid.UUID, org_id: uuid.UUID, data: dict) -> AppointmentDTO | None:
        self.last_patch_data = data

        appt = self.appointments.get(appointment_id)
        if not appt or appt.org_id != org_id or appt.deleted_at is not None:
            return None

        updated = appt.model_copy(
            update={
                **data,
                "updated_at": datetime.datetime.now(datetime.timezone.utc),
            }
        )
        self.appointments[appointment_id] = updated
        return updated

    async def delete(self, appointment_id: uuid.UUID, org_id: uuid.UUID) -> bool:
        appt = self.appointments.get(appointment_id)
        if not appt or appt.org_id != org_id or appt.deleted_at is not None:
            return False

        self.appointments[appointment_id] = appt.model_copy(
            update={"deleted_at": datetime.datetime.now(datetime.timezone.utc)}
        )
        return True


@pytest.mark.asyncio
async def test_create_appointment_success():
    appt_dao = FakeAppointmentDAO()
    client_dao = FakeClientDAO()
    use_case = CreateAppointmentUseCase(appt_dao, client_dao)

    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    client_id = uuid.uuid4()
    client_dao.clients[(client_id, org_id)] = object()

    payload = AppointmentCreateSchema(
        visit_at=datetime.datetime(2026, 5, 10, 16, 0, tzinfo=datetime.timezone.utc),
        status=AppointmentStatus.SCHEDULED,
        product_purchased=False,
        procedures_desc="Маникюр",
        price=decimal.Decimal("2500.00"),
    )

    result = await use_case.execute(payload, org_id=org_id, user_id=user_id, client_id=client_id)

    assert result.org_id == org_id
    assert result.user_id == user_id
    assert result.client_id == client_id
    assert result.visit_at == payload.visit_at
    assert result.id in appt_dao.appointments


@pytest.mark.asyncio
async def test_create_appointment_raises_client_not_found():
    appt_dao = FakeAppointmentDAO()
    client_dao = FakeClientDAO()
    use_case = CreateAppointmentUseCase(appt_dao, client_dao)

    payload = AppointmentCreateSchema(
        visit_at=datetime.datetime(2026, 5, 10, 16, 0, tzinfo=datetime.timezone.utc),
        status=AppointmentStatus.SCHEDULED,
    )

    with pytest.raises(ClientNotFoundError):
        await use_case.execute(
            payload=payload,
            org_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            client_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_create_appointment_raises_when_duplicate_exists():
    appt_dao = FakeAppointmentDAO()
    client_dao = FakeClientDAO()
    use_case = CreateAppointmentUseCase(appt_dao, client_dao)

    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    client_id = uuid.uuid4()
    client_dao.clients[(client_id, org_id)] = object()

    visit_at = datetime.datetime(2026, 5, 10, 16, 0, tzinfo=datetime.timezone.utc)

    await appt_dao.create(
        org_id=org_id,
        user_id=user_id,
        client_id=client_id,
        visit_at=visit_at,
        status="scheduled",
    )

    payload = AppointmentCreateSchema(
        visit_at=visit_at,
        status=AppointmentStatus.SCHEDULED,
    )

    with pytest.raises(AppointmentAlreadyExistsError):
        await use_case.execute(payload, org_id=org_id, user_id=user_id, client_id=client_id)


@pytest.mark.asyncio
async def test_get_all_appointments_converts_date_to_datetime_range():
    appt_dao = FakeAppointmentDAO()
    use_case = GetAllAppointmentsUseCase(appt_dao)

    org_id = uuid.uuid4()
    user_id = uuid.uuid4()

    date_from = datetime.date(2026, 5, 10)
    date_to = datetime.date(2026, 5, 12)

    await use_case.execute(org_id=org_id, date_from=date_from, date_to=date_to, user_id=user_id)

    assert appt_dao.last_get_all_kwargs is not None
    assert appt_dao.last_get_all_kwargs["org_id"] == org_id
    assert appt_dao.last_get_all_kwargs["user_id"] == user_id
    assert appt_dao.last_get_all_kwargs["date_from"] == datetime.datetime.combine(date_from, datetime.time.min)
    assert appt_dao.last_get_all_kwargs["date_to"] == datetime.datetime.combine(date_to, datetime.time.max)


@pytest.mark.asyncio
async def test_get_appointment_by_id_success():
    appt_dao = FakeAppointmentDAO()
    use_case = GetAppointmentByIdUseCase(appt_dao)

    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    client_id = uuid.uuid4()

    created = await appt_dao.create(
        org_id=org_id,
        user_id=user_id,
        client_id=client_id,
        visit_at=datetime.datetime(2026, 5, 10, 16, 0, tzinfo=datetime.timezone.utc),
        status="scheduled",
    )

    result = await use_case.execute(appointment_id=created.id, org_id=org_id)

    assert result == created


@pytest.mark.asyncio
async def test_get_appointment_by_id_raises_when_missing():
    appt_dao = FakeAppointmentDAO()
    use_case = GetAppointmentByIdUseCase(appt_dao)

    with pytest.raises(AppointmentNotFoundError):
        await use_case.execute(appointment_id=uuid.uuid4(), org_id=uuid.uuid4())


@pytest.mark.asyncio
async def test_patch_appointment_success():
    appt_dao = FakeAppointmentDAO()
    use_case = PatchAppointmentUseCase(appt_dao)

    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    client_id = uuid.uuid4()

    created = await appt_dao.create(
        org_id=org_id,
        user_id=user_id,
        client_id=client_id,
        visit_at=datetime.datetime(2026, 5, 10, 16, 0, tzinfo=datetime.timezone.utc),
        status="scheduled",
        product_purchased=False,
    )

    payload = AppointmentPatchSchema(
        status=AppointmentStatus.COMPLETED,
        product_purchased=True,
    )

    result = await use_case.execute(appointment_id=created.id, org_id=org_id, update_data=payload)

    assert result.status == "completed"
    assert result.product_purchased is True
    assert appt_dao.last_patch_data == {
        "status": AppointmentStatus.COMPLETED,
        "product_purchased": True,
    }


@pytest.mark.asyncio
async def test_patch_appointment_raises_when_missing():
    appt_dao = FakeAppointmentDAO()
    use_case = PatchAppointmentUseCase(appt_dao)

    payload = AppointmentPatchSchema(status=AppointmentStatus.CANCELED)

    with pytest.raises(AppointmentNotFoundError):
        await use_case.execute(appointment_id=uuid.uuid4(), org_id=uuid.uuid4(), update_data=payload)


@pytest.mark.asyncio
async def test_delete_appointment_success():
    appt_dao = FakeAppointmentDAO()
    use_case = DeleteAppointmentUseCase(appt_dao)

    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    client_id = uuid.uuid4()

    created = await appt_dao.create(
        org_id=org_id,
        user_id=user_id,
        client_id=client_id,
        visit_at=datetime.datetime(2026, 5, 10, 16, 0, tzinfo=datetime.timezone.utc),
        status="scheduled",
    )

    result = await use_case.execute(appointment_id=created.id, org_id=org_id)

    assert result is True
    assert appt_dao.appointments[created.id].deleted_at is not None


@pytest.mark.asyncio
async def test_delete_appointment_raises_when_missing():
    appt_dao = FakeAppointmentDAO()
    use_case = DeleteAppointmentUseCase(appt_dao)

    with pytest.raises(AppointmentNotFoundError):
        await use_case.execute(appointment_id=uuid.uuid4(), org_id=uuid.uuid4())