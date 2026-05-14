import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.append(str(SRC))

from exceptions.auth import (
    AuthTempLockedError,
    InvalidCredentialsError,
    InvalidTokenError,
    SessionRevokedError,
    UserBlockedError,
    UserInactiveError,
)
from use_cases.auth import (
    GetMeUseCase,
    LoginEmailUseCase,
    LogoutAllUseCase,
    RefreshUseCase,
)


class FakeUserDAO:
    def __init__(self, *, by_email=None, by_id=None):
        self.by_email = by_email
        self.by_id = by_id
        self.requested_emails = []
        self.requested_ids = []
        self.revoked_user_ids = []

    async def get_by_email(self, email):
        self.requested_emails.append(email)
        return self.by_email

    async def get_by_id(self, user_id):
        self.requested_ids.append(user_id)
        return self.by_id

    async def revoke_all_sessions(self, user_id):
        self.revoked_user_ids.append(user_id)


class FakeProtectionService:
    def __init__(self, *, status=None, failure_status=None):
        self.status = status or SimpleNamespace(is_locked=False, retry_after_seconds=0)
        self.failure_status = failure_status or SimpleNamespace(
            is_locked=False,
            retry_after_seconds=0,
            failure_count=1,
        )
        self.checked = []
        self.failures = []
        self.successes = []
        self.delays = []

    async def check_login_allowed(self, *, ip, identifier):
        self.checked.append({"ip": ip, "identifier": identifier})
        return self.status

    async def record_failure(self, *, ip, identifier):
        self.failures.append({"ip": ip, "identifier": identifier})
        return self.failure_status

    async def apply_failure_delay(self, failure_count):
        self.delays.append(failure_count)

    async def record_success(self, *, ip, identifier):
        self.successes.append({"ip": ip, "identifier": identifier})


@pytest.fixture
def token_stubs(monkeypatch):
    monkeypatch.setattr("use_cases.auth.create_access_token", lambda user_id, org_id: f"access-{user_id}-{org_id}")
    monkeypatch.setattr("use_cases.auth.create_refresh_token", lambda user_id, org_id: f"refresh-{user_id}-{org_id}")


@pytest.mark.asyncio
async def test_login_email_success_returns_token_pair_and_records_success(monkeypatch, token_stubs):
    org_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(),
        org_id=org_id,
        email="user@example.com",
        password_hash="stored-hash",
        status="active",
    )
    user_dao = FakeUserDAO(by_email=user)
    protection = FakeProtectionService()
    use_case = LoginEmailUseCase(user_dao, protection)

    monkeypatch.setattr("use_cases.auth._verify_password", lambda password, hashed: password == "secret")

    result = await use_case.execute(
        email="  USER@example.com ",
        password="secret",
        ip_address="127.0.0.1",
    )

    assert result.access_token == f"access-{user.id}-{org_id}"
    assert result.refresh_token == f"refresh-{user.id}-{org_id}"
    assert result.token_type == "bearer"
    assert user_dao.requested_emails == ["user@example.com"]
    assert protection.checked == [{"ip": "127.0.0.1", "identifier": "user@example.com"}]
    assert protection.successes == [{"ip": "127.0.0.1", "identifier": "user@example.com"}]
    assert protection.failures == []
    assert protection.delays == []


@pytest.mark.asyncio
async def test_login_email_raises_invalid_credentials_and_records_failure(monkeypatch):
    user = SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        email="user@example.com",
        password_hash="stored-hash",
        status="active",
    )
    user_dao = FakeUserDAO(by_email=user)
    protection = FakeProtectionService(
        failure_status=SimpleNamespace(is_locked=False, retry_after_seconds=0, failure_count=2)
    )
    use_case = LoginEmailUseCase(user_dao, protection)

    monkeypatch.setattr("use_cases.auth._verify_password", lambda password, hashed: False)

    with pytest.raises(InvalidCredentialsError):
        await use_case.execute(
            email="user@example.com",
            password="wrong",
            ip_address="127.0.0.1",
        )

    assert protection.failures == [{"ip": "127.0.0.1", "identifier": "user@example.com"}]
    assert protection.delays == [2]
    assert protection.successes == []


@pytest.mark.asyncio
async def test_login_email_raises_temp_lock_when_precheck_is_locked():
    protection = FakeProtectionService(
        status=SimpleNamespace(is_locked=True, retry_after_seconds=15)
    )
    use_case = LoginEmailUseCase(FakeUserDAO(by_email=None), protection)

    with pytest.raises(AuthTempLockedError) as exc:
        await use_case.execute(
            email="user@example.com",
            password="wrong",
            ip_address="127.0.0.1",
        )

    assert exc.value.retry_after_seconds == 15
    assert protection.failures == []
    assert protection.successes == []


@pytest.mark.asyncio
async def test_login_email_raises_temp_lock_when_failure_reaches_threshold(monkeypatch):
    user_dao = FakeUserDAO(by_email=None)
    protection = FakeProtectionService(
        failure_status=SimpleNamespace(is_locked=True, retry_after_seconds=30, failure_count=3)
    )
    use_case = LoginEmailUseCase(user_dao, protection)

    monkeypatch.setattr("use_cases.auth._verify_password", lambda password, hashed: False)

    with pytest.raises(AuthTempLockedError) as exc:
        await use_case.execute(
            email="user@example.com",
            password="wrong",
            ip_address="127.0.0.1",
        )

    assert exc.value.retry_after_seconds == 30
    assert protection.failures == [{"ip": "127.0.0.1", "identifier": "user@example.com"}]
    assert protection.delays == [3]


@pytest.mark.asyncio
async def test_login_email_raises_blocked_for_blocked_user(monkeypatch):
    user = SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        email="user@example.com",
        password_hash="stored-hash",
        status="blocked",
    )
    use_case = LoginEmailUseCase(FakeUserDAO(by_email=user), FakeProtectionService())

    monkeypatch.setattr("use_cases.auth._verify_password", lambda password, hashed: True)

    with pytest.raises(UserBlockedError):
        await use_case.execute("user@example.com", "secret")


@pytest.mark.asyncio
async def test_login_email_raises_inactive_for_non_active_user(monkeypatch):
    user = SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        email="user@example.com",
        password_hash="stored-hash",
        status="pending",
    )
    use_case = LoginEmailUseCase(FakeUserDAO(by_email=user), FakeProtectionService())

    monkeypatch.setattr("use_cases.auth._verify_password", lambda password, hashed: True)

    with pytest.raises(UserInactiveError):
        await use_case.execute("user@example.com", "secret")


@pytest.mark.asyncio
async def test_refresh_success_rotates_token_pair(monkeypatch, token_stubs):
    user_id = uuid4()
    org_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        org_id=org_id,
        status="active",
        sessions_revoked_at=None,
    )
    user_dao = FakeUserDAO(by_id=user)
    use_case = RefreshUseCase(user_dao)
    issued_at = datetime(2026, 3, 11, 10, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "use_cases.auth.decode_token",
        lambda token, required_aud: {
            "sub": str(user_id),
            "iat": int(issued_at.timestamp()),
        },
    )

    result = await use_case.execute("refresh-token")

    assert result.access_token == f"access-{user_id}-{org_id}"
    assert result.refresh_token == f"refresh-{user_id}-{org_id}"
    assert user_dao.requested_ids == [user_id]


@pytest.mark.asyncio
async def test_refresh_raises_invalid_token_when_user_missing(monkeypatch):
    user_id = uuid4()
    use_case = RefreshUseCase(FakeUserDAO(by_id=None))

    monkeypatch.setattr(
        "use_cases.auth.decode_token",
        lambda token, required_aud: {"sub": str(user_id), "iat": int(datetime.now(timezone.utc).timestamp())},
    )

    with pytest.raises(InvalidTokenError):
        await use_case.execute("refresh-token")


@pytest.mark.asyncio
async def test_refresh_raises_blocked_when_user_not_active(monkeypatch):
    user_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        org_id=uuid4(),
        status="blocked",
        sessions_revoked_at=None,
    )
    use_case = RefreshUseCase(FakeUserDAO(by_id=user))

    monkeypatch.setattr(
        "use_cases.auth.decode_token",
        lambda token, required_aud: {"sub": str(user_id), "iat": int(datetime.now(timezone.utc).timestamp())},
    )

    with pytest.raises(UserBlockedError):
        await use_case.execute("refresh-token")


@pytest.mark.asyncio
async def test_refresh_raises_revoked_when_token_issued_before_revoke(monkeypatch):
    user_id = uuid4()
    issued_at = datetime(2026, 3, 11, 10, 0, 0, tzinfo=timezone.utc)
    user = SimpleNamespace(
        id=user_id,
        org_id=uuid4(),
        status="active",
        sessions_revoked_at=(issued_at + timedelta(minutes=5)).replace(tzinfo=None),
    )
    use_case = RefreshUseCase(FakeUserDAO(by_id=user))

    monkeypatch.setattr(
        "use_cases.auth.decode_token",
        lambda token, required_aud: {"sub": str(user_id), "iat": int(issued_at.timestamp())},
    )

    with pytest.raises(SessionRevokedError):
        await use_case.execute("refresh-token")


@pytest.mark.asyncio
async def test_logout_all_delegates_to_user_dao():
    user_id = uuid4()
    user_dao = FakeUserDAO()
    use_case = LogoutAllUseCase(user_dao)

    await use_case.execute(user_id)

    assert user_dao.revoked_user_ids == [user_id]


@pytest.mark.asyncio
async def test_get_me_returns_current_user_dto():
    user_id = uuid4()
    session_id = uuid4()
    org_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        email="user@example.com",
        phone="+79990000000",
        full_name="Test User",
        org_id=org_id,
        status="active",
    )
    use_case = GetMeUseCase(FakeUserDAO(by_id=user))

    result = await use_case.execute(user_id=user_id, session_id=session_id)

    assert result.id == user_id
    assert result.session_id == session_id
    assert result.email == "user@example.com"
    assert result.phone == "+79990000000"
    assert result.full_name == "Test User"
    assert result.org_id == org_id
    assert result.status == "active"


@pytest.mark.asyncio
async def test_get_me_raises_invalid_token_when_user_missing():
    user_id = uuid4()
    session_id = uuid4()
    use_case = GetMeUseCase(FakeUserDAO(by_id=None))

    with pytest.raises(InvalidTokenError):
        await use_case.execute(user_id=user_id, session_id=session_id)

