import csv
from io import StringIO
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

import use_cases.admin_medical_texts as medical_texts_use_cases
from db.daos.admin_audit_log import AdminAuditLogDAO
from db.daos.medical_text_export import MedicalTextExportDAO
from use_cases.admin_medical_texts import CreateMedicalTextExportUseCase, CSV_FIELDNAMES

pytestmark = pytest.mark.no_db


@dataclass
class FakeAuditDAO:
    calls: list[dict] = field(default_factory=list)

    async def write(self, **kwargs):
        self.calls.append(kwargs)


class FakeMedicalTextDAO:
    def __init__(self, clients=None, appointments=None):
        self.clients = clients or []
        self.appointments = appointments or []

    async def get_all_clients(self, org_id=None):
        return self.clients

    async def get_all_appointments(self, org_id=None):
        return self.appointments


@pytest.mark.asyncio
async def test_export_keeps_all_columns_for_mixed_rows(monkeypatch):
    org_id = uuid4()
    admin_id = uuid4()
    client_id = uuid4()
    appointment_id = uuid4()

    fake_audit = FakeAuditDAO()
    fake_dao = FakeMedicalTextDAO(
        clients=[
            SimpleNamespace(
                org_id=org_id,
                id=client_id,
                full_name="Client A",
                diagnoses="dx",
                allergies="alg",
                contraindications="contra",
                notes="note",
            )
        ],
        appointments=[
            SimpleNamespace(
                org_id=org_id,
                id=appointment_id,
                client_id=client_id,
                procedures_desc="proc",
                recommendations_common="rec_common",
                recommendations_product="rec_product",
                next_visit_plan="next",
            )
        ],
    )

    captured = {}

    def _fake_put_object_bytes(key, payload, content_type):
        captured["key"] = key
        captured["payload"] = payload
        captured["content_type"] = content_type

    monkeypatch.setattr(medical_texts_use_cases, "put_object_bytes", _fake_put_object_bytes)
    monkeypatch.setattr(
        medical_texts_use_cases,
        "generate_presigned_get_url",
        lambda *, key, expires_in: {"url": f"https://seaweed-s3:8333/master-podolog/{key}?sig=short", "expires_in": expires_in},
    )

    use_case = CreateMedicalTextExportUseCase(
        cast(AdminAuditLogDAO, fake_audit),
        cast(MedicalTextExportDAO, fake_dao),
    )
    result = await use_case.execute(reason="audit", admin_user_id=admin_id, org_id=org_id, ip_address="127.0.0.1")

    assert result["download_url"].startswith("/master-podolog/admin/medical_texts_exports/")
    assert result["download_url"].endswith("?sig=short")
    assert captured["content_type"] == "text/csv"

    csv_text = captured["payload"].decode("utf-8")
    # strip BOM if present and parse with semicolon delimiter
    csv_text = csv_text.lstrip('\ufeff')
    rows = list(csv.DictReader(StringIO(csv_text), delimiter=';'))

    assert rows
    assert set(rows[0].keys()) == set(CSV_FIELDNAMES)

    client_row = next(r for r in rows if r["full_name"] == "Client A")
    assert client_row["diagnoses"] == "dx"
    assert client_row["procedures_desc"] == ""

    appointment_row = next(r for r in rows if r["appointment_id"] == str(appointment_id))
    assert appointment_row["procedures_desc"] == "proc"
    assert appointment_row["full_name"] == ""

    assert [c["action"] for c in fake_audit.calls] == [
        "medical_texts_export_requested",
        "medical_texts_export_completed",
    ]


@pytest.mark.asyncio
async def test_export_skips_fully_empty_rows(monkeypatch):
    fake_audit = FakeAuditDAO()
    fake_dao = FakeMedicalTextDAO(
        clients=[
            SimpleNamespace(
                org_id=uuid4(),
                id=uuid4(),
                full_name="Empty Client",
                diagnoses=None,
                allergies="",
                contraindications=None,
                notes="",
            )
        ],
        appointments=[
            SimpleNamespace(
                org_id=uuid4(),
                id=uuid4(),
                client_id=uuid4(),
                procedures_desc=None,
                recommendations_common="",
                recommendations_product=None,
                next_visit_plan="",
            )
        ],
    )

    captured = {}

    def _fake_put_object_bytes(key, payload, content_type):
        captured["payload"] = payload

    monkeypatch.setattr(medical_texts_use_cases, "put_object_bytes", _fake_put_object_bytes)
    monkeypatch.setattr(
        medical_texts_use_cases,
        "generate_presigned_get_url",
        lambda *, key, expires_in: {"url": f"https://seaweed-s3:8333/master-podolog/{key}?sig=short", "expires_in": expires_in},
    )

    use_case = CreateMedicalTextExportUseCase(
        cast(AdminAuditLogDAO, fake_audit),
        cast(MedicalTextExportDAO, fake_dao),
    )
    await use_case.execute(reason="skip-empty", admin_user_id=uuid4())

    csv_text = captured["payload"].decode("utf-8").lstrip('\ufeff').strip().splitlines()
    assert csv_text == [';'.join(CSV_FIELDNAMES)]


@pytest.mark.asyncio
async def test_export_writes_full_header_when_no_rows(monkeypatch):
    fake_audit = FakeAuditDAO()
    fake_dao = FakeMedicalTextDAO(clients=[], appointments=[])

    captured = {}

    def _fake_put_object_bytes(key, payload, content_type):
        captured["payload"] = payload

    monkeypatch.setattr(medical_texts_use_cases, "put_object_bytes", _fake_put_object_bytes)
    monkeypatch.setattr(
        medical_texts_use_cases,
        "generate_presigned_get_url",
        lambda *, key, expires_in: {"url": f"https://seaweed-s3:8333/master-podolog/{key}?sig=short", "expires_in": expires_in},
    )

    use_case = CreateMedicalTextExportUseCase(
        cast(AdminAuditLogDAO, fake_audit),
        cast(MedicalTextExportDAO, fake_dao),
    )
    await use_case.execute(reason="empty", admin_user_id=uuid4())

    csv_text = captured["payload"].decode("utf-8").lstrip('\ufeff').strip()
    assert csv_text == ";".join(CSV_FIELDNAMES)


