import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.append(str(SRC))

from exceptions.auth import AuthTempLockedError, InvalidCredentialsError
from services.auth_protection import AuthProtectionService
from use_cases.auth import LoginEmailUseCase

pytestmark = pytest.mark.no_db


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.expirations = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = str(value)
        self.expirations[key] = ttl

    async def incr(self, key):
        current = int(self.store.get(key, "0")) + 1
        self.store[key] = str(current)
        return current

    async def expire(self, key, ttl):
        self.expirations[key] = ttl

    async def ttl(self, key):
        return self.expirations.get(key, -1)

    async def delete(self, key):
        self.store.pop(key, None)
        self.expirations.pop(key, None)

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def setex(self, key, ttl, value):
        self.ops.append(("setex", key, ttl, value))
        return self

    def delete(self, key):
        self.ops.append(("delete", key))
        return self

    async def execute(self):
        for op in self.ops:
            if op[0] == "setex":
                _, key, ttl, value = op
                await self.redis.setex(key, ttl, value)
            elif op[0] == "delete":
                _, key = op
                await self.redis.delete(key)
        self.ops.clear()


class FakeAttemptDAO:
    def __init__(self):
        self.attempts = []
        self.cleanup_calls = []

    async def create_attempt(self, *, ip, identifier_hash, success):
        self.attempts.append(
            {"ip": ip, "identifier_hash": identifier_hash, "success": success}
        )

    async def cleanup_old_attempts(self, retention_days):
        self.cleanup_calls.append(retention_days)


class FakeUserDAO:
    def __init__(self, user=None):
        self.user = user
        self.emails = []

    async def get_by_email(self, email):
        self.emails.append(email)
        return self.user


@pytest.fixture
def protection_settings(monkeypatch):
    monkeypatch.setattr("services.auth_protection.settings.AUTH_LOGIN_MAX_FAILURES", 3)
    monkeypatch.setattr("services.auth_protection.settings.AUTH_LOGIN_FAILURE_WINDOW_SECONDS", 60)
    monkeypatch.setattr("services.auth_protection.settings.AUTH_LOGIN_LOCK_BASE_SECONDS", 2)
    monkeypatch.setattr("services.auth_protection.settings.AUTH_LOGIN_LOCK_MAX_SECONDS", 60)
    monkeypatch.setattr("services.auth_protection.settings.AUTH_LOGIN_RATE_LIMIT_PER_IP", 10)
    monkeypatch.setattr("services.auth_protection.settings.AUTH_LOGIN_RATE_LIMIT_WINDOW_SECONDS", 60)
    monkeypatch.setattr("services.auth_protection.settings.AUTH_LOGIN_LOG_RETENTION_DAYS", 30)
    monkeypatch.setattr("services.auth_protection.settings.AUTH_IDENTIFIER_HASH_PEPPER", "pepper")
    monkeypatch.setattr("services.auth_protection.settings.AUTH_FAILURE_DELAY_BASE_MS", 0)
    monkeypatch.setattr("services.auth_protection.settings.AUTH_FAILURE_DELAY_MAX_MS", 0)


@pytest.mark.asyncio
async def test_login_locks_after_n_failures(protection_settings):
    service = AuthProtectionService(FakeRedis(), FakeAttemptDAO())
    use_case = LoginEmailUseCase(FakeUserDAO(user=None), service)

    with pytest.raises(InvalidCredentialsError):
        await use_case.execute("user@example.com", "wrong", ip_address="127.0.0.1")
    with pytest.raises(InvalidCredentialsError):
        await use_case.execute("user@example.com", "wrong", ip_address="127.0.0.1")
    with pytest.raises(AuthTempLockedError) as exc:
        await use_case.execute("user@example.com", "wrong", ip_address="127.0.0.1")

    assert exc.value.retry_after_seconds == 2


@pytest.mark.asyncio
async def test_successful_login_clears_block(protection_settings, monkeypatch):
    fake_redis = FakeRedis()
    attempt_dao = FakeAttemptDAO()
    service = AuthProtectionService(fake_redis, attempt_dao)

    user = SimpleNamespace(
        id="user-id",
        org_id=uuid4(),
        password_hash="stored-hash",
        status="active",
    )
    use_case = LoginEmailUseCase(FakeUserDAO(user=user), service)

    monkeypatch.setattr("use_cases.auth._verify_password", lambda password, hashed: password == "secret")
    monkeypatch.setattr("use_cases.auth.create_access_token", lambda user_id, org_id: "access")
    monkeypatch.setattr("use_cases.auth.create_refresh_token", lambda user_id, org_id: "refresh")

    await service.record_failure(ip="127.0.0.1", identifier="user@example.com")
    await service.record_failure(ip="127.0.0.1", identifier="user@example.com")

    result = await use_case.execute("user@example.com", "secret", ip_address="127.0.0.1")
    status = await service.check_login_allowed(ip="127.0.0.1", identifier="user@example.com")

    assert result.access_token == "access"
    assert result.refresh_token == "refresh"
    assert status.is_locked is False
    assert status.failure_count == 0


@pytest.mark.asyncio
async def test_identifier_hash_is_not_plain_identifier(protection_settings):
    attempt_dao = FakeAttemptDAO()
    service = AuthProtectionService(FakeRedis(), attempt_dao)

    await service.record_failure(ip="127.0.0.1", identifier="User@Example.com")

    stored_hash = attempt_dao.attempts[0]["identifier_hash"]
    assert stored_hash != "User@Example.com"
    assert stored_hash != "user@example.com"
    assert len(stored_hash) == 64


@pytest.mark.asyncio
async def test_ip_rate_limit_blocks_even_without_identifier_lock(protection_settings):
    fake_redis = FakeRedis()
    service = AuthProtectionService(fake_redis, FakeAttemptDAO())

    rate_key = service._rate_limit_key("127.0.0.1")
    await fake_redis.setex(rate_key, 60, 10)

    status = await service.check_login_allowed(
        ip="127.0.0.1",
        identifier="user@example.com",
    )

    assert status.is_locked is True
    assert status.rate_limited is True
    assert status.retry_after_seconds == 0


@pytest.mark.asyncio
async def test_repeated_lock_cycles_escalate_retry_after(protection_settings):
    service = AuthProtectionService(FakeRedis(), FakeAttemptDAO())

    status = None
    for _ in range(3):
        status = await service.record_failure(ip="127.0.0.1", identifier="user@example.com")

    assert status is not None
    assert status.is_locked is True
    assert status.retry_after_seconds == 2
    assert status.lock_level == 1

    fourth_failure = await service.record_failure(ip="127.0.0.1", identifier="user@example.com")
    second_cycle = await service.record_failure(ip="127.0.0.1", identifier="user@example.com")

    assert fourth_failure.is_locked is True
    assert fourth_failure.retry_after_seconds == 4
    assert fourth_failure.lock_level == 2
    assert second_cycle.is_locked is True
    assert second_cycle.retry_after_seconds == 8
    assert second_cycle.lock_level == 3


@pytest.mark.asyncio
async def test_login_email_normalizes_identifier_before_lookup_and_tracking(protection_settings, monkeypatch):
    fake_redis = FakeRedis()
    attempt_dao = FakeAttemptDAO()
    user = SimpleNamespace(
        id="user-id",
        org_id=uuid4(),
        password_hash="stored-hash",
        status="active",
    )
    user_dao = FakeUserDAO(user=user)
    service = AuthProtectionService(fake_redis, attempt_dao)
    use_case = LoginEmailUseCase(user_dao, service)

    monkeypatch.setattr("use_cases.auth._verify_password", lambda password, hashed: password == "secret")
    monkeypatch.setattr("use_cases.auth.create_access_token", lambda user_id, org_id: "access")
    monkeypatch.setattr("use_cases.auth.create_refresh_token", lambda user_id, org_id: "refresh")

    await use_case.execute("  USER@Example.com  ", "secret", ip_address="127.0.0.1")

    assert user_dao.emails == ["user@example.com"]
    assert len(attempt_dao.attempts) == 1
    assert attempt_dao.attempts[0]["success"] is True
    assert attempt_dao.cleanup_calls == [30]


@pytest.mark.asyncio
async def test_record_failure_triggers_cleanup_with_retention_window(protection_settings):
    attempt_dao = FakeAttemptDAO()
    service = AuthProtectionService(FakeRedis(), attempt_dao)

    await service.record_failure(ip="127.0.0.1", identifier="user@example.com")

    assert attempt_dao.cleanup_calls == [30]
