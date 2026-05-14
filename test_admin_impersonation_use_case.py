from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.token import decode_token
from use_cases.admin_impersonation import IssueMasterImpersonationTokenUseCase


pytestmark = pytest.mark.no_db


class FakeUserDAO:
    def __init__(self, user):
        self.user = user

    async def get_by_id(self, user_id):
        if self.user and self.user.id == user_id:
            return self.user
        return None


class FakeAuditDAO:
    def __init__(self):
        self.audit_id = uuid4()
        self.calls = []

    async def write(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=self.audit_id, created_at=datetime.now(timezone.utc))


class FakeNotificationScheduler:
    def __init__(self):
        self.calls = []

    async def enqueue_impersonation_notice(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(devices_seen=1, created=1)


@pytest.mark.asyncio
async def test_issue_impersonation_token_audits_and_notifies():
    org_id = uuid4()
    target_user = SimpleNamespace(id=uuid4(), org_id=org_id, status="active")
    actor_admin_id = uuid4()
    audit_dao = FakeAuditDAO()
    scheduler = FakeNotificationScheduler()
    use_case = IssueMasterImpersonationTokenUseCase(
        user_dao=FakeUserDAO(target_user),
        audit_dao=audit_dao,
        notification_scheduler=scheduler,
        token_expires_minutes=5,
    )

    result = await use_case.execute(
        target_user_id=target_user.id,
        actor_admin_id=actor_admin_id,
        actor_role="TechSupport",
        ip_address="127.0.0.1",
    )

    claims = decode_token(result.access_token, required_aud="access")
    assert result.original_user_id == target_user.id
    assert result.actor_admin_id == actor_admin_id
    assert result.audit_id == audit_dao.audit_id
    assert claims["sub"] == str(target_user.id)
    assert claims["actor_admin_id"] == str(actor_admin_id)
    assert claims["original_user_id"] == str(target_user.id)
    assert claims["impersonation_audit_id"] == str(audit_dao.audit_id)

    assert audit_dao.calls[0]["action"] == "admin_impersonation_started"
    assert audit_dao.calls[0]["admin_user_id"] == actor_admin_id
    assert audit_dao.calls[0]["target_id"] == str(target_user.id)
    assert scheduler.calls[0]["user_id"] == target_user.id
    assert scheduler.calls[0]["audit_id"] == audit_dao.audit_id
