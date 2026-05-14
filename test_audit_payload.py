from datetime import datetime, timezone
from uuid import uuid4

import pytest

from services.audit import (
    AuditAction,
    BUSINESS_OPERATION_AUDIT_ACTION_MAP,
    build_audit_payload,
)


pytestmark = pytest.mark.no_db


def test_audit_payload_has_required_shape_and_redacts_sensitive_meta():
    actor_id = uuid4()
    target_id = uuid4()
    payload = build_audit_payload(
        action=AuditAction.ADMIN_CLIENT_CARD_VIEWED.value,
        actor_id=actor_id,
        actor_type="admin",
        actor_role="SuperAdmin",
        target_id=str(target_id),
        target_type="client",
        timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
        meta={
            "phone": "+79001234567",
            "diagnoses": "raw medical text",
            "org_id": str(uuid4()),
            "masked": False,
        },
    )

    assert set(payload) >= {"actor", "action", "target", "timestamp", "meta"}
    assert payload["actor"] == {
        "type": "admin",
        "id": str(actor_id),
        "role": "SuperAdmin",
    }
    assert payload["target"] == {"type": "client", "id": str(target_id)}
    assert payload["meta"]["masked"] is False
    assert payload["meta"]["phone"]["redacted"] is True
    assert "+79001234567" not in str(payload)
    assert "raw medical text" not in str(payload)


def test_business_operation_audit_action_map_covers_acceptance_operations():
    assert BUSINESS_OPERATION_AUDIT_ACTION_MAP["admin.client.view"] == "admin_client_card_viewed"
    assert BUSINESS_OPERATION_AUDIT_ACTION_MAP["admin.master.impersonation.start"] == "admin_impersonation_started"
    assert BUSINESS_OPERATION_AUDIT_ACTION_MAP["admin.medical_text.export.request"] == "admin_medical_text_export_requested"
    assert BUSINESS_OPERATION_AUDIT_ACTION_MAP["media.archive.export.request"] == "media_archive_export_requested"
    assert BUSINESS_OPERATION_AUDIT_ACTION_MAP["admin.client.restore"] == "admin_client_restored"
    assert BUSINESS_OPERATION_AUDIT_ACTION_MAP["admin.promo.update"] == "admin_promo_code_updated"
    assert BUSINESS_OPERATION_AUDIT_ACTION_MAP["admin.subscription.grant"] == "grant_promo"
