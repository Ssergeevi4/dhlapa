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
)
from exceptions.user import UserAlreadyExistsError
from use_cases.master_auth import (
    MasterLoginUseCase,
    MasterLogoutAllUseCase,
    MasterRefreshUseCase,
    MasterRegisterUseCase,
)


class FakeUserDAO:
    def __init__(self):
        self.email_users = {}
        self.phone_users = {}
        self.by_id = {}
        self.lookup_email_calls = []
        self.lookup_phone_calls = []
        self.lookup_identifier_calls = []
        self.created_users = []
        self.revoked_user_ids = []

    async def get_by_email(self, email):
        self.lookup_email_calls.append(email)
        return self.email_users.get(email)

    async def get_by_phone(self, phone):
        self.lookup_phone_calls.append(phone)
        return self.phone_users.get(phone)

    async def get_by_email_or_phone(self, email, phone):
        self.lookup_identifier_calls.append({"email": email, "phone": phone})
        if email is not None:
            return self.email_users.get(email)
        if phone is not None:
            return self.phone_users.get(phone)
        return None

    async def create(self, user):
        if getattr(user, "id", None) is None:
            user.id = uuid4()
        self.created_users.append(user)
        self.by_id[user.id] = user
        if getattr(user, "email", None):
            self.email_users[user.email] = user
        if getattr(user, "phone", None):
            self.phone_users[user.phone] = user
        return user

    async def get_by_id(self, user_id):
        return self.by_id.get(user_id)

    async def revoke_all_sessions(self, user_id):
        self.revoked_user_ids.append(user_id)


class FakeOrganizationDAO:
    def __init__(self):
        self.created_orgs = []

    async def create(self, org):
        if getattr(org, "id", None) is None:
            org.id = uuid4()
        self.created_orgs.append(org)
        return org


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
    monkeypatch.setattr("use_cases.master_auth.create_access_token", lambda user_id, org_id: f"access-{user_id}-{org_id}")
    monkeypatch.setattr("use_cases.master_auth.create_refresh_token", lambda user_id, org_id: f"refresh-{user_id}-{org_id}")


@pytest.mark.asyncio
async def test_master_register_success_creates_org_user_and_tokens(monkeypatch, token_stubs):
    user_dao = FakeUserDAO()
    org_dao = FakeOrganizationDAO()
    use_case = MasterRegisterUseCase(user_dao, org_dao)

    monkeypatch.setattr("use_cases.master_auth._hash_password", lambda password: f"hashed::{password}")
    monkeypatch.setattr("use_cases.master_auth.settings.TRIAL_DAYS", 14)

    before = datetime.now(timezone.utc)
    result = await use_case.execute(
        full_name="Doctor Master",
        email="  MASTER@example.com ",
        password="secret",
        phone="+7 (999) 123-45-67",
    )
    after = datetime.now(timezone.utc)

    assert result.token_type == "bearer"
    assert len(org_dao.created_orgs) == 1
    assert org_dao.created_orgs[0].name == "Doctor Master"
    assert len(user_dao.created_users) == 1

    created_user = user_dao.created_users[0]
    assert created_user.full_name == "Doctor Master"
    assert created_user.email == "master@example.com"
    assert created_user.phone == "+79991234567"
    assert created_user.password_hash == "hashed::secret"
    assert created_user.status == "active"
    assert created_user.org_id == org_dao.created_orgs[0].id
    assert created_user.trial_ends_at.tzinfo is not None
    assert before + timedelta(days=14) <= created_user.trial_ends_at <= after + timedelta(days=14)
    assert result.access_token == f"access-{created_user.id}-{created_user.org_id}"
    assert result.refresh_token == f"refresh-{created_user.id}-{created_user.org_id}"


@pytest.mark.asyncio
async def test_master_register_raises_when_email_already_exists():
    existing_user = SimpleNamespace(id=uuid4(), email="master@example.com")
    user_dao = FakeUserDAO()
    user_dao.email_users["master@example.com"] = existing_user
    use_case = MasterRegisterUseCase(user_dao, FakeOrganizationDAO())

    with pytest.raises(UserAlreadyExistsError):
        await use_case.execute(
            full_name="Doctor Master",
            email="MASTER@example.com",
            password="secret",
            phone=None,
        )

    assert user_dao.lookup_email_calls == ["master@example.com"]
    assert user_dao.created_users == []


@pytest.mark.asyncio
async def test_master_register_raises_when_phone_already_exists():
    existing_user = SimpleNamespace(id=uuid4(), phone="+79991234567")
    user_dao = FakeUserDAO()
    user_dao.phone_users["+79991234567"] = existing_user
    use_case = MasterRegisterUseCase(user_dao, FakeOrganizationDAO())

    with pytest.raises(UserAlreadyExistsError):
        await use_case.execute(
            full_name="Doctor Master",
            email="master@example.com",
            password="secret",
            phone="8 (999) 123-45-67",
        )

    assert user_dao.lookup_phone_calls == ["+79991234567"]
    assert user_dao.created_users == []


@pytest.mark.asyncio
async def test_master_register_normalizes_phone_by_stripping_non_digits(monkeypatch, token_stubs):
    user_dao = FakeUserDAO()
    org_dao = FakeOrganizationDAO()
    use_case = MasterRegisterUseCase(user_dao, org_dao)

    monkeypatch.setattr("use_cases.master_auth._hash_password", lambda password: f"hashed::{password}")

    await use_case.execute(
        full_name="Doctor Master",
        email="master@example.com",
        password="secret",
        phone="8 (999) 123-45-67",
    )

    assert user_dao.created_users[0].phone == "+79991234567"


@pytest.mark.asyncio
async def test_master_login_success_with_email_records_success(monkeypatch, token_stubs):
    org_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(),
        org_id=org_id,
        email="master@example.com",
        phone="+79991234567",
        password_hash="stored-hash",
        status="active",
    )
    user_dao = FakeUserDAO()
    user_dao.email_users["master@example.com"] = user
    protection = FakeProtectionService()
    use_case = MasterLoginUseCase(user_dao, protection)

    monkeypatch.setattr("use_cases.master_auth._verify_password", lambda password, hashed: password == "secret")

    result = await use_case.execute(
        identifier="  MASTER@example.com  ",
        password="secret",
        ip_address="127.0.0.1",
    )

    assert result.access_token == f"access-{user.id}-{org_id}"
    assert result.refresh_token == f"refresh-{user.id}-{org_id}"
    assert protection.checked == [{"ip": "127.0.0.1", "identifier": "master@example.com"}]
    assert protection.successes == [{"ip": "127.0.0.1", "identifier": "master@example.com"}]
    assert user_dao.lookup_identifier_calls == [{"email": "master@example.com", "phone": None}]


@pytest.mark.asyncio
async def test_master_login_success_with_phone_normalizes_lookup(monkeypatch, token_stubs):
    org_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(),
        org_id=org_id,
        email="master@example.com",
        phone="+79991234567",
        password_hash="stored-hash",
        status="active",
    )
    user_dao = FakeUserDAO()
    user_dao.phone_users["+79991234567"] = user
    protection = FakeProtectionService()
    use_case = MasterLoginUseCase(user_dao, protection)

    monkeypatch.setattr("use_cases.master_auth._verify_password", lambda password, hashed: password == "secret")

    result = await use_case.execute(
        identifier=" +7 (999) 123-45-67 ",
        password="secret",
        ip_address="127.0.0.1",
    )

    assert result.access_token == f"access-{user.id}-{org_id}"
    assert result.refresh_token == f"refresh-{user.id}-{org_id}"
    assert protection.checked == [{"ip": "127.0.0.1", "identifier": "+7 (999) 123-45-67".strip().lower()}]
    assert user_dao.lookup_identifier_calls == [{"email": None, "phone": "+79991234567"}]


@pytest.mark.asyncio
async def test_master_login_raises_invalid_credentials_and_records_failure(monkeypatch):
    user = SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        password_hash="stored-hash",
        status="active",
    )
    user_dao = FakeUserDAO()
    user_dao.email_users["master@example.com"] = user
    protection = FakeProtectionService(
        failure_status=SimpleNamespace(is_locked=False, retry_after_seconds=0, failure_count=2)
    )
    use_case = MasterLoginUseCase(user_dao, protection)

    monkeypatch.setattr("use_cases.master_auth._verify_password", lambda password, hashed: False)

    with pytest.raises(InvalidCredentialsError):
        await use_case.execute("master@example.com", "wrong", ip_address="127.0.0.1")

    assert protection.failures == [{"ip": "127.0.0.1", "identifier": "master@example.com"}]
    assert protection.delays == [2]
    assert protection.successes == []


@pytest.mark.asyncio
async def test_master_login_raises_temp_lock_on_precheck():
    protection = FakeProtectionService(status=SimpleNamespace(is_locked=True, retry_after_seconds=20))
    use_case = MasterLoginUseCase(FakeUserDAO(), protection)

    with pytest.raises(AuthTempLockedError) as exc:
        await use_case.execute("master@example.com", "wrong", ip_address="127.0.0.1")

    assert exc.value.retry_after_seconds == 20
    assert protection.failures == []


@pytest.mark.asyncio
async def test_master_login_raises_temp_lock_after_failed_attempt(monkeypatch):
    protection = FakeProtectionService(
        failure_status=SimpleNamespace(is_locked=True, retry_after_seconds=45, failure_count=3)
    )
    use_case = MasterLoginUseCase(FakeUserDAO(), protection)

    monkeypatch.setattr("use_cases.master_auth._verify_password", lambda password, hashed: False)

    with pytest.raises(AuthTempLockedError) as exc:
        await use_case.execute("master@example.com", "wrong", ip_address="127.0.0.1")

    assert exc.value.retry_after_seconds == 45
    assert protection.failures == [{"ip": "127.0.0.1", "identifier": "master@example.com"}]
    assert protection.delays == [3]


@pytest.mark.asyncio
async def test_master_login_raises_blocked_for_blocked_user(monkeypatch):
    user = SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        password_hash="stored-hash",
        status="blocked",
    )
    user_dao = FakeUserDAO()
    user_dao.email_users["master@example.com"] = user
    use_case = MasterLoginUseCase(user_dao, FakeProtectionService())

    monkeypatch.setattr("use_cases.master_auth._verify_password", lambda password, hashed: True)

    with pytest.raises(UserBlockedError):
        await use_case.execute("master@example.com", "secret")


@pytest.mark.asyncio
async def test_master_login_raises_invalid_credentials_for_non_active_user(monkeypatch):
    user = SimpleNamespace(
        id=uuid4(),
        org_id=uuid4(),
        password_hash="stored-hash",
        status="pending",
    )
    user_dao = FakeUserDAO()
    user_dao.email_users["master@example.com"] = user
    use_case = MasterLoginUseCase(user_dao, FakeProtectionService())

    monkeypatch.setattr("use_cases.master_auth._verify_password", lambda password, hashed: True)

    with pytest.raises(InvalidCredentialsError):
        await use_case.execute("master@example.com", "secret")


@pytest.mark.asyncio
async def test_master_refresh_success_rotates_token_pair(monkeypatch, token_stubs):
    user_id = uuid4()
    org_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        org_id=org_id,
        status="active",
        sessions_revoked_at=None,
    )
    user_dao = FakeUserDAO()
    user_dao.by_id[user_id] = user
    use_case = MasterRefreshUseCase(user_dao)
    issued_at = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "use_cases.master_auth.decode_token",
        lambda token, required_aud: {"sub": str(user_id), "iat": int(issued_at.timestamp())},
    )

    result = await use_case.execute("refresh-token")

    assert result.access_token == f"access-{user_id}-{org_id}"
    assert result.refresh_token == f"refresh-{user_id}-{org_id}"


@pytest.mark.asyncio
async def test_master_refresh_raises_invalid_token_when_user_missing(monkeypatch):
    user_id = uuid4()
    use_case = MasterRefreshUseCase(FakeUserDAO())

    monkeypatch.setattr(
        "use_cases.master_auth.decode_token",
        lambda token, required_aud: {"sub": str(user_id), "iat": int(datetime.now(timezone.utc).timestamp())},
    )

    with pytest.raises(InvalidTokenError):
        await use_case.execute("refresh-token")


@pytest.mark.asyncio
async def test_master_refresh_raises_blocked_when_user_blocked(monkeypatch):
    user_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        org_id=uuid4(),
        status="blocked",
        sessions_revoked_at=None,
    )
    user_dao = FakeUserDAO()
    user_dao.by_id[user_id] = user
    use_case = MasterRefreshUseCase(user_dao)

    monkeypatch.setattr(
        "use_cases.master_auth.decode_token",
        lambda token, required_aud: {"sub": str(user_id), "iat": int(datetime.now(timezone.utc).timestamp())},
    )

    with pytest.raises(UserBlockedError):
        await use_case.execute("refresh-token")


@pytest.mark.asyncio
async def test_master_refresh_raises_invalid_token_for_non_active_user(monkeypatch):
    user_id = uuid4()
    user = SimpleNamespace(
        id=user_id,
        org_id=uuid4(),
        status="pending",
        sessions_revoked_at=None,
    )
    user_dao = FakeUserDAO()
    user_dao.by_id[user_id] = user
    use_case = MasterRefreshUseCase(user_dao)

    monkeypatch.setattr(
        "use_cases.master_auth.decode_token",
        lambda token, required_aud: {"sub": str(user_id), "iat": int(datetime.now(timezone.utc).timestamp())},
    )

    with pytest.raises(InvalidTokenError):
        await use_case.execute("refresh-token")


@pytest.mark.asyncio
async def test_master_refresh_raises_revoked_when_token_issued_before_revoke(monkeypatch):
    user_id = uuid4()
    issued_at = datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)
    user = SimpleNamespace(
        id=user_id,
        org_id=uuid4(),
        status="active",
        sessions_revoked_at=(issued_at + timedelta(minutes=1)).replace(tzinfo=None),
    )
    user_dao = FakeUserDAO()
    user_dao.by_id[user_id] = user
    use_case = MasterRefreshUseCase(user_dao)

    monkeypatch.setattr(
        "use_cases.master_auth.decode_token",
        lambda token, required_aud: {"sub": str(user_id), "iat": int(issued_at.timestamp())},
    )

    with pytest.raises(SessionRevokedError):
        await use_case.execute("refresh-token")


@pytest.mark.asyncio
async def test_master_logout_all_delegates_to_user_dao():
    user_id = uuid4()
    user_dao = FakeUserDAO()
    use_case = MasterLogoutAllUseCase(user_dao)

    await use_case.execute(user_id)

    assert user_dao.revoked_user_ids == [user_id]
