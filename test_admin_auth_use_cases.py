"""Unit-tests for admin auth use-cases (single active session)."""

import hashlib
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
    ActiveSessionExistsError,
    AuthTempLockedError,
    InvalidCredentialsError,
    InvalidTokenError,
    SessionRevokedError,
    UserBlockedError,
)
from use_cases.admin_auth import AdminLoginUseCase, AdminRefreshUseCase, AdminLogoutUseCase

pytestmark = pytest.mark.no_db


# ── Fakes ────────────────────────────────────────────────────


class FakeAdminUserDAO:
    def __init__(self):
        self.by_email = {}
        self.by_id = {}

    async def get_by_email(self, email):
        return self.by_email.get(email)

    async def get_by_id(self, admin_user_id):
        return self.by_id.get(admin_user_id)


class FakeAdminSessionDAO:
    def __init__(self):
        self.sessions = {}  # id -> session
        self.active_by_user = {}  # admin_user_id -> session (if active)

    async def create(self, admin_session):
        sid = getattr(admin_session, "id", None)
        if sid is None or not isinstance(sid, __import__("uuid").UUID):
            admin_session.id = uuid4()
        self.sessions[admin_session.id] = admin_session
        status = getattr(admin_session, "status", "active")
        if status == "active":
            self.active_by_user[admin_session.admin_user_id] = admin_session
        return admin_session

    async def get_active_by_admin_user_id(self, admin_user_id):
        s = self.active_by_user.get(admin_user_id)
        if s and s.status == "active":
            return s
        return None

    async def get_active_global(self):
        for session in self.sessions.values():
            if session.status == "active":
                return session
        return None

    async def deactivate_expired(self, now=None):
        count = 0
        for session in list(self.sessions.values()):
            if session.status == "active" and session.expires_at <= now:
                await self.deactivate(session.id)
                count += 1
        return count

    async def get_active_by_id(self, session_id):
        s = self.sessions.get(session_id)
        if s and s.status == "active":
            return s
        return None

    async def deactivate(self, session_id):
        s = self.sessions.get(session_id)
        if s:
            s.status = "revoked"
            if self.active_by_user.get(s.admin_user_id) is s:
                del self.active_by_user[s.admin_user_id]

    async def update_tokens(self, session_id, new_refresh_token_hash, new_expires_at):
        s = self.sessions.get(session_id)
        if s:
            s.refresh_token_hash = new_refresh_token_hash
            s.expires_at = new_expires_at


class FakeProtectionService:
    def __init__(self, *, status=None, failure_status=None):
        self.status = status or SimpleNamespace(is_locked=False, retry_after_seconds=0)
        self.failure_status = failure_status or SimpleNamespace(
            is_locked=False, retry_after_seconds=0, failure_count=1
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


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture
def admin_user():
    uid = uuid4()
    user = SimpleNamespace(
        id=uid,
        email="admin@example.com",
        password_hash="hashed",
        status="active",
        role="SuperAdmin",
    )
    return user


@pytest.fixture
def token_stubs(monkeypatch):
    """Stub token functions to return predictable values."""
    monkeypatch.setattr(
        "use_cases.admin_auth.create_admin_access_token",
        lambda admin_user_id, session_id, role: f"access-{admin_user_id}-{session_id}-{role}",
    )
    monkeypatch.setattr(
        "use_cases.admin_auth.create_admin_refresh_token",
        lambda admin_user_id, session_id: f"refresh-{admin_user_id}-{session_id}",
    )


# ── AdminLoginUseCase ────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_login_success(monkeypatch, admin_user, token_stubs):
    """First login with valid credentials returns token pair."""
    admin_user_dao = FakeAdminUserDAO()
    admin_user_dao.by_email["admin@example.com"] = admin_user
    admin_session_dao = FakeAdminSessionDAO()
    protection = FakeProtectionService()

    monkeypatch.setattr("use_cases.admin_auth._verify_password", lambda pw, h: True)

    uc = AdminLoginUseCase(admin_user_dao, admin_session_dao, protection)
    result = await uc.execute(email="admin@example.com", password="secret", ip_address="1.2.3.4")

    assert result.token_type == "bearer"
    assert result.access_token.startswith("access-")
    assert result.refresh_token.startswith("refresh-")
    assert protection.successes == [{"ip": "1.2.3.4", "identifier": "admin@example.com"}]
    assert len(admin_session_dao.sessions) == 1


@pytest.mark.asyncio
async def test_admin_login_returns_409_when_active_session_exists(monkeypatch, admin_user, token_stubs):
    """Second parallel login returns 409 (ActiveSessionExistsError)."""
    admin_user_dao = FakeAdminUserDAO()
    admin_user_dao.by_email["admin@example.com"] = admin_user
    admin_session_dao = FakeAdminSessionDAO()
    protection = FakeProtectionService()

    monkeypatch.setattr("use_cases.admin_auth._verify_password", lambda pw, h: True)

    # First login — creates session
    uc = AdminLoginUseCase(admin_user_dao, admin_session_dao, protection)
    await uc.execute(email="admin@example.com", password="secret")

    # Second login — must raise 409
    with pytest.raises(ActiveSessionExistsError):
        await uc.execute(email="admin@example.com", password="secret")


@pytest.mark.asyncio
async def test_admin_login_auto_revokes_expired_session(monkeypatch, admin_user, token_stubs):
    """If existing active session is expired, auto-revoke it and create new one."""
    admin_user_dao = FakeAdminUserDAO()
    admin_user_dao.by_email["admin@example.com"] = admin_user
    admin_session_dao = FakeAdminSessionDAO()
    protection = FakeProtectionService()

    monkeypatch.setattr("use_cases.admin_auth._verify_password", lambda pw, h: True)

    # Create an expired session manually
    expired_session = SimpleNamespace(
        id=uuid4(),
        admin_user_id=admin_user.id,
        status="active",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        refresh_token_hash="old",
    )
    admin_session_dao.sessions[expired_session.id] = expired_session
    admin_session_dao.active_by_user[admin_user.id] = expired_session

    uc = AdminLoginUseCase(admin_user_dao, admin_session_dao, protection)
    result = await uc.execute(email="admin@example.com", password="secret")

    # Old session should be revoked
    assert expired_session.status == "revoked"
    # New session created
    assert result.access_token.startswith("access-")


@pytest.mark.asyncio
async def test_admin_login_invalid_credentials(monkeypatch, admin_user):
    """Wrong password raises InvalidCredentialsError."""
    admin_user_dao = FakeAdminUserDAO()
    admin_user_dao.by_email["admin@example.com"] = admin_user
    admin_session_dao = FakeAdminSessionDAO()
    protection = FakeProtectionService()

    monkeypatch.setattr("use_cases.admin_auth._verify_password", lambda pw, h: False)

    uc = AdminLoginUseCase(admin_user_dao, admin_session_dao, protection)
    with pytest.raises(InvalidCredentialsError):
        await uc.execute(email="admin@example.com", password="wrong")

    assert len(protection.failures) == 1


@pytest.mark.asyncio
async def test_admin_login_user_not_found(monkeypatch):
    """Non-existent email raises InvalidCredentialsError."""
    admin_user_dao = FakeAdminUserDAO()
    admin_session_dao = FakeAdminSessionDAO()
    protection = FakeProtectionService()

    uc = AdminLoginUseCase(admin_user_dao, admin_session_dao, protection)
    with pytest.raises(InvalidCredentialsError):
        await uc.execute(email="nobody@example.com", password="secret")


@pytest.mark.asyncio
async def test_admin_login_blocked_user(monkeypatch, admin_user):
    """Blocked admin raises UserBlockedError."""
    admin_user.status = "blocked"
    admin_user_dao = FakeAdminUserDAO()
    admin_user_dao.by_email["admin@example.com"] = admin_user
    admin_session_dao = FakeAdminSessionDAO()
    protection = FakeProtectionService()

    monkeypatch.setattr("use_cases.admin_auth._verify_password", lambda pw, h: True)

    uc = AdminLoginUseCase(admin_user_dao, admin_session_dao, protection)
    with pytest.raises(UserBlockedError):
        await uc.execute(email="admin@example.com", password="secret")


@pytest.mark.asyncio
async def test_admin_login_temp_locked():
    """Locked protection raises AuthTempLockedError."""
    admin_user_dao = FakeAdminUserDAO()
    admin_session_dao = FakeAdminSessionDAO()
    protection = FakeProtectionService(
        status=SimpleNamespace(is_locked=True, retry_after_seconds=60)
    )

    uc = AdminLoginUseCase(admin_user_dao, admin_session_dao, protection)
    with pytest.raises(AuthTempLockedError):
        await uc.execute(email="admin@example.com", password="secret")


# ── AdminRefreshUseCase ──────────────────────────────────────


def _make_refresh_payload(admin_user_id, session_id):
    return {
        "sub": str(admin_user_id),
        "sid": str(session_id),
        "role": "admin",
        "token_type": "admin_refresh",
        "aud": "admin_refresh",
    }


@pytest.mark.asyncio
async def test_admin_refresh_success(monkeypatch, admin_user, token_stubs):
    """Valid refresh token returns new token pair."""
    session_id = uuid4()
    refresh_token = f"refresh-{admin_user.id}-{session_id}"
    token_hash = hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()

    admin_user_dao = FakeAdminUserDAO()
    admin_user_dao.by_id[admin_user.id] = admin_user

    admin_session_dao = FakeAdminSessionDAO()
    active_session = SimpleNamespace(
        id=session_id,
        admin_user_id=admin_user.id,
        status="active",
        refresh_token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    admin_session_dao.sessions[session_id] = active_session
    admin_session_dao.active_by_user[admin_user.id] = active_session

    monkeypatch.setattr(
        "use_cases.admin_auth.decode_token",
        lambda token, required_aud=None: _make_refresh_payload(admin_user.id, session_id),
    )
    monkeypatch.setattr(
        "use_cases.admin_auth._hash_token",
        lambda t: hashlib.sha256(t.encode("utf-8")).hexdigest(),
    )

    uc = AdminRefreshUseCase(admin_user_dao, admin_session_dao)
    result = await uc.execute(refresh_token=refresh_token)

    assert result.token_type == "bearer"
    assert result.access_token.startswith("access-")
    assert result.refresh_token.startswith("refresh-")


@pytest.mark.asyncio
async def test_admin_refresh_inactive_session_returns_401(monkeypatch, admin_user):
    """Refresh for inactive session raises SessionRevokedError (→ 401)."""
    session_id = uuid4()

    admin_user_dao = FakeAdminUserDAO()
    admin_user_dao.by_id[admin_user.id] = admin_user

    # No active session in DAO
    admin_session_dao = FakeAdminSessionDAO()

    monkeypatch.setattr(
        "use_cases.admin_auth.decode_token",
        lambda token, required_aud=None: _make_refresh_payload(admin_user.id, session_id),
    )

    uc = AdminRefreshUseCase(admin_user_dao, admin_session_dao)
    with pytest.raises(SessionRevokedError):
        await uc.execute(refresh_token="some-token")


@pytest.mark.asyncio
async def test_admin_refresh_token_mismatch_deactivates_session(monkeypatch, admin_user):
    """Mismatched refresh_token_hash deactivates session and raises InvalidTokenError."""
    session_id = uuid4()

    admin_user_dao = FakeAdminUserDAO()
    admin_user_dao.by_id[admin_user.id] = admin_user

    admin_session_dao = FakeAdminSessionDAO()
    active_session = SimpleNamespace(
        id=session_id,
        admin_user_id=admin_user.id,
        status="active",
        refresh_token_hash="old-hash-that-wont-match",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    admin_session_dao.sessions[session_id] = active_session
    admin_session_dao.active_by_user[admin_user.id] = active_session

    monkeypatch.setattr(
        "use_cases.admin_auth.decode_token",
        lambda token, required_aud=None: _make_refresh_payload(admin_user.id, session_id),
    )
    monkeypatch.setattr(
        "use_cases.admin_auth._hash_token",
        lambda t: "completely-different-hash",
    )

    uc = AdminRefreshUseCase(admin_user_dao, admin_session_dao)
    with pytest.raises(InvalidTokenError, match="replay"):
        await uc.execute(refresh_token="some-token")

    assert active_session.status == "revoked"


@pytest.mark.asyncio
async def test_admin_refresh_blocked_user(monkeypatch, admin_user):
    """Refresh for blocked admin raises UserBlockedError."""
    admin_user.status = "blocked"
    session_id = uuid4()

    admin_user_dao = FakeAdminUserDAO()
    admin_user_dao.by_id[admin_user.id] = admin_user

    admin_session_dao = FakeAdminSessionDAO()

    monkeypatch.setattr(
        "use_cases.admin_auth.decode_token",
        lambda token, required_aud=None: _make_refresh_payload(admin_user.id, session_id),
    )

    uc = AdminRefreshUseCase(admin_user_dao, admin_session_dao)
    with pytest.raises(UserBlockedError):
        await uc.execute(refresh_token="some-token")


# ── AdminLogoutUseCase ───────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_logout_deactivates_session():
    """Logout deactivates the session."""
    session_id = uuid4()
    admin_session_dao = FakeAdminSessionDAO()
    active_session = SimpleNamespace(
        id=session_id,
        admin_user_id=uuid4(),
        status="active",
    )
    admin_session_dao.sessions[session_id] = active_session
    admin_session_dao.active_by_user[active_session.admin_user_id] = active_session

    uc = AdminLogoutUseCase(admin_session_dao)
    await uc.execute(session_id=session_id)

    assert active_session.status == "revoked"


@pytest.mark.asyncio
async def test_admin_logout_idempotent():
    """Second logout does not raise any error (idempotent)."""
    session_id = uuid4()
    admin_session_dao = FakeAdminSessionDAO()
    active_session = SimpleNamespace(
        id=session_id,
        admin_user_id=uuid4(),
        status="active",
    )
    admin_session_dao.sessions[session_id] = active_session
    admin_session_dao.active_by_user[active_session.admin_user_id] = active_session

    uc = AdminLogoutUseCase(admin_session_dao)

    # First logout
    await uc.execute(session_id=session_id)
    assert active_session.status == "revoked"

    # Second logout — no error
    await uc.execute(session_id=session_id)
    assert active_session.status == "revoked"


@pytest.mark.asyncio
async def test_admin_logout_nonexistent_session_idempotent():
    """Logout for non-existent session does not raise any error."""
    admin_session_dao = FakeAdminSessionDAO()
    uc = AdminLogoutUseCase(admin_session_dao)

    # Should not raise
    await uc.execute(session_id=uuid4())

