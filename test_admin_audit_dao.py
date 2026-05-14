from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from db.daos.admin_audit_log import AdminAuditLogDAO


pytestmark = pytest.mark.no_db


class _EmptyScalarResult:
    def all(self):
        return []


class _EmptyExecuteResult:
    def scalars(self):
        return _EmptyScalarResult()


class _RecordingSession:
    def __init__(self) -> None:
        self.executed = []
        self.scalars = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _EmptyExecuteResult()

    async def scalar(self, stmt):
        self.scalars.append(stmt)
        return 0


@pytest.mark.asyncio
async def test_admin_audit_log_dao_builds_filtered_stable_page_query():
    session = _RecordingSession()
    admin_id = uuid4()

    await AdminAuditLogDAO(session).list_logs(
        created_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        created_to=datetime(2026, 5, 4, tzinfo=timezone.utc),
        action=" admin_client_card_viewed ",
        admin_user_id=admin_id,
        target_id=" client-1 ",
        page=3,
        size=50,
        sort_by="action",
        sort_order="asc",
    )

    stmt = session.executed[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))

    assert "admin_audit_logs.created_at >=" in sql
    assert "admin_audit_logs.created_at <=" in sql
    assert "admin_audit_logs.action =" in sql
    assert "admin_audit_logs.admin_user_id =" in sql
    assert "admin_audit_logs.target_id =" in sql
    assert (
        "ORDER BY admin_audit_logs.action ASC, "
        "admin_audit_logs.created_at DESC, admin_audit_logs.id DESC"
    ) in sql
    assert stmt._limit_clause.value == 50
    assert stmt._offset_clause.value == 100

    count_sql = str(session.scalars[0].compile(dialect=postgresql.dialect()))
    assert "count(admin_audit_logs.id)" in count_sql
    assert "admin_audit_logs.target_id =" in count_sql
