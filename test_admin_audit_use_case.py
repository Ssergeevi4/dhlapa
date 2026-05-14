from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dto.admin_audit import ListAdminAuditLogsQueryDTO
from services.audit import AuditAction, build_audit_payload
from use_cases.admin_audit import ListAdminAuditLogsUseCase


pytestmark = pytest.mark.no_db


class FakeAuditDAO:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def list_logs(self, **kwargs):
        self.calls.append(kwargs)
        return self.rows, len(self.rows)


@pytest.mark.asyncio
async def test_list_admin_audit_logs_maps_payload_and_forwards_filters():
    admin_id = uuid4()
    target_id = str(uuid4())
    created_at = datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(
        id=uuid4(),
        admin_user_id=admin_id,
        action=AuditAction.ADMIN_CLIENT_CARD_VIEWED.value,
        target_id=target_id,
        created_at=created_at,
        ip_address="127.0.0.1",
        event_payload=build_audit_payload(
            action=AuditAction.ADMIN_CLIENT_CARD_VIEWED.value,
            actor_id=admin_id,
            actor_type="admin",
            actor_role="SuperAdmin",
            target_id=target_id,
            target_type="client",
            timestamp=created_at,
            meta={"masked": False},
        ),
    )
    dao = FakeAuditDAO([row])
    query = ListAdminAuditLogsQueryDTO(
        action=AuditAction.ADMIN_CLIENT_CARD_VIEWED.value,
        admin_user_id=admin_id,
        target_id=target_id,
        page=2,
        size=10,
        sort_by="action",
        sort_order="asc",
    )

    result = await ListAdminAuditLogsUseCase(dao).execute(query)

    assert result.total == 1
    assert result.page == 2
    assert result.size == 10
    assert result.items[0].actor["role"] == "SuperAdmin"
    assert result.items[0].target == {"type": "client", "id": target_id}
    assert result.items[0].meta == {"masked": False}
    assert dao.calls[0]["sort_by"] == "action"
    assert dao.calls[0]["sort_order"] == "asc"
