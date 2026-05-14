from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from dto.notification import (
    ActiveDeviceDTO,
    BusinessNotificationCandidateDTO,
    EnqueueNotificationResultDTO,
    NotificationOutboxDTO,
    PushDeliveryResultDTO,
)
from services.push_providers import APNsPushProvider, FCMPushProvider, PushDispatcher, PushProviderError
from use_cases.notification_outbox import (
    BusinessNotificationSchedulerUseCase,
    ProcessPushOutboxUseCase,
)

pytestmark = pytest.mark.no_db

NOW = datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc)


class FakeOutboxDAO:
    def __init__(
        self,
        *,
        notifications: list[NotificationOutboxDTO] | None = None,
        devices: list[ActiveDeviceDTO] | None = None,
    ) -> None:
        self.notifications = {item.id: item for item in notifications or []}
        self.by_dedupe = {
            item.dedupe_key: item.id for item in self.notifications.values()
        }
        self.devices = devices or []

    async def enqueue(self, **kwargs) -> EnqueueNotificationResultDTO:
        existing_id = self.by_dedupe.get(kwargs["dedupe_key"])
        if existing_id is not None:
            return EnqueueNotificationResultDTO(
                notification=self.notifications[existing_id],
                created=False,
            )

        notification = make_notification(
            user_id=kwargs["user_id"],
            org_id=kwargs["org_id"],
            device_id=kwargs["device_id"],
            platform=kwargs["platform"],
            device_token=kwargs["device_token"],
            event_type=kwargs["event_type"],
            dedupe_key=kwargs["dedupe_key"],
            trace_id=kwargs["trace_id"],
            correlation_id=kwargs["correlation_id"],
            payload=kwargs["payload"],
            max_attempts=kwargs["max_attempts"],
            next_attempt_at=kwargs["next_attempt_at"],
        )
        self.notifications[notification.id] = notification
        self.by_dedupe[notification.dedupe_key] = notification.id
        return EnqueueNotificationResultDTO(notification=notification, created=True)

    async def claim_batch(self, *, now, limit, lease_seconds):
        claimed = []
        for notification in self.notifications.values():
            if notification.status == "queued" and notification.next_attempt_at <= now:
                notification.locked_at = now
                claimed.append(notification)
            if len(claimed) == limit:
                break
        return claimed

    async def mark_sent(self, *, notification_id, now, provider_message_id):
        notification = self.notifications[notification_id]
        if notification.status != "queued":
            return False
        notification.status = "sent"
        notification.sent_at = now
        notification.provider_message_id = provider_message_id
        notification.locked_at = None
        return True

    async def mark_retry(self, *, notification_id, now, retry_at, error):
        notification = self.notifications[notification_id]
        if notification.status != "queued":
            return False
        notification.attempts += 1
        notification.next_attempt_at = retry_at
        notification.last_error = error
        notification.locked_at = None
        return True

    async def mark_failed(self, *, notification_id, now, error):
        notification = self.notifications[notification_id]
        if notification.status != "queued":
            return False
        notification.status = "failed"
        notification.failed_at = now
        notification.attempts += 1
        notification.last_error = error
        notification.locked_at = None
        return True

    async def list_active_devices(self, user_ids):
        allowed = set(user_ids)
        return [device for device in self.devices if device.user_id in allowed]


class FakeNotificationSourceDAO:
    def __init__(self, candidates: list[BusinessNotificationCandidateDTO]) -> None:
        self._candidates = candidates

    async def list_next_visit_candidates(self, **kwargs):
        return self._by_scenario("next_visit")

    async def list_birthday_candidates(self, **kwargs):
        return self._by_scenario("birthday")

    async def list_long_break_candidates(self, **kwargs):
        return self._by_scenario("long_break")

    async def list_expiry_candidates(self, **kwargs):
        return self._by_scenario("expiry")

    async def list_booking_candidates(self, **kwargs):
        return self._by_scenario("booking")

    async def list_impersonation_candidates(self, **kwargs):
        return self._by_scenario("impersonation")

    def _by_scenario(self, scenario):
        return [item for item in self._candidates if item.scenario == scenario]


class RecordingProvider:
    provider_name = "recording"

    def __init__(self, *, provider_name="recording", error=None) -> None:
        self.provider_name = provider_name
        self.error = error
        self.calls = []

    async def send(self, notification):
        self.calls.append(notification)
        if self.error:
            raise self.error
        return PushDeliveryResultDTO(
            provider=self.provider_name,
            provider_message_id=f"{self.provider_name}:{notification.dedupe_key}",
        )


@pytest.mark.asyncio
async def test_push_worker_routes_by_platform_and_marks_sent():
    ios = make_notification(platform="ios", dedupe_key="ios-1")
    android = make_notification(platform="android", dedupe_key="android-1")
    outbox = FakeOutboxDAO(notifications=[ios, android])
    apns = RecordingProvider(provider_name="apns")
    fcm = RecordingProvider(provider_name="fcm")

    result = await make_worker(outbox, apns=apns, fcm=fcm).execute()

    assert result.claimed == 2
    assert result.sent == 2
    assert [item.id for item in apns.calls] == [ios.id]
    assert [item.id for item in fcm.calls] == [android.id]
    assert outbox.notifications[ios.id].status == "sent"
    assert outbox.notifications[android.id].status == "sent"


@pytest.mark.asyncio
async def test_push_worker_schedules_retry_with_exponential_backoff():
    notification = make_notification(attempts=0, max_attempts=3)
    outbox = FakeOutboxDAO(notifications=[notification])
    provider_error = PushProviderError("timeout", retryable=True)
    apns = RecordingProvider(provider_name="apns", error=provider_error)

    result = await make_worker(outbox, apns=apns).execute()

    updated = outbox.notifications[notification.id]
    assert result.retried == 1
    assert updated.status == "queued"
    assert updated.attempts == 1
    assert updated.next_attempt_at == NOW + timedelta(seconds=60)
    assert updated.last_error == "timeout"


@pytest.mark.asyncio
async def test_push_worker_marks_failed_after_max_attempts():
    notification = make_notification(attempts=2, max_attempts=3)
    outbox = FakeOutboxDAO(notifications=[notification])
    provider_error = PushProviderError("provider rejected", retryable=True, status_code=500)
    apns = RecordingProvider(provider_name="apns", error=provider_error)

    result = await make_worker(outbox, apns=apns).execute()

    updated = outbox.notifications[notification.id]
    assert result.failed == 1
    assert updated.status == "failed"
    assert updated.attempts == 3
    assert updated.failed_at == NOW


@pytest.mark.asyncio
async def test_scheduler_supports_all_scenarios_and_is_idempotent():
    user_id = uuid4()
    org_id = uuid4()
    device = make_device(user_id=user_id, org_id=org_id, platform="ios")
    candidates = [
        make_candidate(scenario=scenario, user_id=user_id, org_id=org_id)
        for scenario in (
            "next_visit",
            "birthday",
            "long_break",
            "expiry",
            "booking",
            "impersonation",
        )
    ]
    outbox = FakeOutboxDAO(devices=[device])
    scheduler = make_scheduler(FakeNotificationSourceDAO(candidates), outbox)

    first = await scheduler.execute(trace_id="trace-1")
    second = await scheduler.execute(trace_id="trace-2")

    assert first.created == 6
    assert first.duplicates == 0
    assert second.created == 0
    assert second.duplicates == 6
    assert set(first.by_scenario) == {
        "next_visit",
        "birthday",
        "long_break",
        "expiry",
        "booking",
        "impersonation",
    }
    assert all(item.trace_id == "trace-1" for item in outbox.notifications.values())
    assert all(item.correlation_id for item in outbox.notifications.values())


@pytest.mark.asyncio
async def test_e2e_smoke_scheduler_to_dry_run_providers_without_duplicates():
    user_id = uuid4()
    org_id = uuid4()
    outbox = FakeOutboxDAO(
        devices=[
            make_device(user_id=user_id, org_id=org_id, platform="ios"),
            make_device(user_id=user_id, org_id=org_id, platform="android"),
        ],
    )
    source = FakeNotificationSourceDAO(
        [make_candidate(scenario="booking", user_id=user_id, org_id=org_id)],
    )

    scheduled = await make_scheduler(source, outbox).execute(trace_id="smoke")
    sent = await make_worker(
        outbox,
        apns=APNsPushProvider(endpoint_url=None, auth_token=None, timeout_seconds=1.0),
        fcm=FCMPushProvider(endpoint_url=None, server_key=None, timeout_seconds=1.0),
    ).execute()
    duplicate_schedule = await make_scheduler(source, outbox).execute(trace_id="smoke-2")
    second_send = await make_worker(
        outbox,
        apns=APNsPushProvider(endpoint_url=None, auth_token=None, timeout_seconds=1.0),
        fcm=FCMPushProvider(endpoint_url=None, server_key=None, timeout_seconds=1.0),
    ).execute()

    assert scheduled.created == 2
    assert sent.sent == 2
    assert duplicate_schedule.created == 0
    assert duplicate_schedule.duplicates == 2
    assert second_send.claimed == 0
    assert {item.status for item in outbox.notifications.values()} == {"sent"}


@pytest.mark.asyncio
async def test_provider_network_errors_are_retryable(monkeypatch):
    provider = APNsPushProvider(
        endpoint_url="https://push.example.test",
        auth_token="token",
        timeout_seconds=0.1,
    )

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    def raise_timeout(body, headers):
        raise TimeoutError("provider timeout")

    monkeypatch.setattr("services.push_providers.asyncio.to_thread", run_inline)
    monkeypatch.setattr(provider._http, "_post_json", raise_timeout)

    with pytest.raises(PushProviderError) as exc_info:
        await provider.send(make_notification(platform="ios"))

    assert exc_info.value.retryable is True


def make_worker(outbox, *, apns=None, fcm=None):
    return ProcessPushOutboxUseCase(
        outbox_dao=outbox,
        dispatcher=PushDispatcher(
            apns=apns or RecordingProvider(provider_name="apns"),
            fcm=fcm or RecordingProvider(provider_name="fcm"),
        ),
        batch_size=100,
        lease_seconds=300,
        base_backoff_seconds=60,
        max_backoff_seconds=300,
        now_factory=lambda: NOW,
    )


def make_scheduler(source, outbox):
    return BusinessNotificationSchedulerUseCase(
        source_dao=source,
        outbox_dao=outbox,
        batch_size=100,
        max_attempts=3,
        next_visit_lookahead_hours=24,
        birthday_lookahead_days=0,
        long_break_days=60,
        expiry_lookahead_days=3,
        booking_lookback_hours=24,
        impersonation_lookback_hours=24,
        now_factory=lambda: NOW,
    )


def make_device(*, user_id, org_id, platform):
    return ActiveDeviceDTO(
        id=uuid4(),
        user_id=user_id,
        org_id=org_id,
        platform=platform,
        device_token=f"{platform}-token-{uuid4()}",
    )


def make_candidate(*, scenario, user_id, org_id):
    entity_id = uuid4()
    return BusinessNotificationCandidateDTO(
        scenario=scenario,
        user_id=user_id,
        org_id=org_id,
        entity_id=entity_id,
        dedupe_scope=f"{entity_id}:2026-04-28",
        payload={"title": scenario, "body": "body"},
    )


def make_notification(
    *,
    user_id=None,
    org_id=None,
    device_id=None,
    platform="ios",
    device_token=None,
    event_type="booking",
    dedupe_key=None,
    trace_id="trace",
    correlation_id="booking:1",
    payload=None,
    status="queued",
    attempts=0,
    max_attempts=3,
    next_attempt_at=NOW,
):
    return NotificationOutboxDTO(
        id=uuid4(),
        user_id=user_id or uuid4(),
        org_id=org_id or uuid4(),
        device_id=device_id or uuid4(),
        platform=platform,
        device_token=device_token or f"{platform}-token",
        event_type=event_type,
        dedupe_key=dedupe_key or str(uuid4()),
        trace_id=trace_id,
        correlation_id=correlation_id,
        payload=payload or {"title": "Title", "body": "Body"},
        status=status,
        attempts=attempts,
        max_attempts=max_attempts,
        next_attempt_at=next_attempt_at,
        locked_at=None,
        sent_at=None,
        failed_at=None,
        provider_message_id=None,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
    )
