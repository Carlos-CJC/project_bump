"""Model/ACK regression tests. Real TLS MQTT is covered by integration tests."""

import copy
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bump import device as device_module
from bump.device import DeviceSimulator


class FakeClient:
    def __init__(self):
        self.published = []
        self.subscribed = []
        self.on_publish_test = None
        self.started = False

    def subscribe(self, topic, qos):
        self.subscribed.append((topic, qos))
        return 0, 7

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, json.loads(payload), qos, retain))
        if self.on_publish_test:
            self.on_publish_test()
        return SimpleNamespace(rc=0)

    def connect_async(self, *args):
        pass

    def loop_start(self):
        self.started = True

    def disconnect(self):
        pass

    def loop_stop(self):
        self.started = False


@pytest.fixture
def settings():
    return {
        "mqtt": {"host": "localhost", "port": 18883},
        "devices": {
            "badge-a": {"owner": "alice", "alias": "A00001"},
            "badge-b": {"owner": "bob", "alias": "B00002"},
        },
        "sessions": {"demo-001": {"A00001": "badge-a", "B00002": "badge-b"}},
    }


@pytest.fixture
def make_device(monkeypatch, settings):
    created = []
    monkeypatch.setattr(device_module, "make_mqtt_client", lambda *_: FakeClient())

    def factory(**kwargs):
        device = DeviceSimulator(settings, "badge-a", **kwargs)
        created.append(device)
        return device

    yield factory
    for device in created:
        device.close()


def observation(device):
    assert device.shake(now=0)
    return device.observe(
        "B00002", start_utc=datetime(2026, 10, 23, 8, tzinfo=timezone.utc)
    )


def ack_for(event, status="stored"):
    return {
        "schema_version": 1,
        "device_id": event["device_id"],
        "boot_id": event["boot_id"],
        "seq": event["seq"],
        "status": status,
    }


def test_window_expiry_and_manual_pause_require_a_new_gesture(make_device):
    device = make_device()
    first = observation(device)
    assert first["source"] == "simulated"
    assert not device.shake(now=30)  # A second gesture must not extend this window.
    assert device.tick(now=59.99) == "DISCOVERY"
    assert device.tick(now=60) == "SYNC"
    device.pause()
    assert device.queue_size == 1
    assert not device.shake(now=70)
    assert device.observe("B00002") is None
    device.resume()
    assert device.state == "IDLE"
    assert device.shake(now=80)
    second = device.observe("B00002")
    assert second["seq"] == 2
    assert second["boot_id"] == first["boot_id"]


def test_ram_queue_is_bounded_and_events_are_frozen(make_device):
    device = make_device(capacity=1)
    original = observation(device)
    same = copy.deepcopy(original)
    assert device.enqueue(same)
    original["rssi_median"] = -90
    assert device.queue[0]["rssi_median"] == -55
    with pytest.raises(ValueError, match="cannot change"):
        device.enqueue(original)
    extra = copy.deepcopy(same)
    extra["seq"] = 2
    assert not device.enqueue(extra)
    assert device.overflow_count == 1
    assert device.queue_size == 1


@pytest.mark.parametrize(
    "patch",
    [
        {"device_id": "badge-b"}, {"boot_id": "other-boot"}, {"seq": 9},
        {"status": "rejected"}, {"schema_version": True}, {"seq": True},
        {"status": "PUBACK"}, {"schema_version": 2}, {"status": []},
    ],
)
def test_wrong_or_non_success_ack_never_clears_queue(make_device, patch):
    device = make_device()
    event = observation(device)
    ack = ack_for(event)
    ack.update(patch)
    assert not device.handle_ack(ack)
    assert device.queue_size == 1
    assert device.handle_ack(ack_for(event, "duplicate"))
    assert device.queue_size == 0


def test_retained_and_malformed_ack_are_ignored(make_device):
    device = make_device()
    event = observation(device)
    retained = SimpleNamespace(
        topic=device.ack_topic, retain=True, payload=json.dumps(ack_for(event)).encode()
    )
    device._on_message(device._client, None, retained)
    malformed = SimpleNamespace(topic=device.ack_topic, retain=False, payload=b"{")
    device._on_message(device._client, None, malformed)
    assert device.queue_size == 1


def test_only_simulated_events_and_new_boot_on_new_instance(make_device):
    device = make_device()
    event = observation(device)
    event["source"] = "hardware"
    with pytest.raises(ValueError, match="simulated"):
        device.enqueue(event)
    replacement = make_device()
    assert replacement.boot_id != device.boot_id
    assert replacement.queue_size == 0


def test_unknown_time_is_explicitly_null(make_device):
    device = make_device()
    assert device.shake(now=0)
    event = device.observe("B00002", time_quality="unknown")
    assert event["start_utc"] is None and event["end_utc"] is None


def test_connect_is_not_ready_until_matching_suback(make_device):
    device = make_device()
    assert not device.connect(timeout=0)
    device._on_connect(device._client, None, None, 0, None)
    assert device._client.subscribed == [(device.ack_topic, 1)]
    assert not device.health
    device._on_subscribe(device._client, None, 999, [1], None)
    assert not device.health
    device._on_subscribe(device._client, None, 7, [128], None)
    assert not device.health
    device._on_subscribe(device._client, None, 7, [1], None)
    assert device.health
    device._on_disconnect(device._client, None, None, 1, None)
    assert not device.health


def test_sync_waits_for_subscription_and_keeps_queue_on_timeout(make_device, monkeypatch):
    device = make_device()
    observation(device)
    device.tick(now=60)

    def not_subscribed(timeout):
        time.sleep(min(0.01, max(0, timeout)))
        return False

    monkeypatch.setattr(device, "connect", not_subscribed)
    assert not device.sync(timeout=0.03)
    assert device.queue_size == 1
    assert device._client.published == []
    assert device.state == "IDLE"


def test_discovery_does_not_upload_but_paused_backlog_retries(make_device, monkeypatch):
    device = make_device(drop_ack_count=1)
    event = observation(device)
    monkeypatch.setattr(device, "connect", lambda timeout: True)
    device._client.on_publish_test = lambda: device.handle_ack(ack_for(event, "duplicate"))
    assert not device.sync(timeout=0.5)
    assert device._client.published == []
    device.pause()
    assert device.sync(timeout=1)
    assert device.state == "PAUSED"
    assert device.queue_size == 0
    assert device.dropped_ack_count == 1
    assert device.publish_count == 2
    assert all(qos == 1 and not retain for _, _, qos, retain in device._client.published)


def test_worker_acks_only_after_store_returns_and_reacks_duplicates(
    monkeypatch, settings, make_device
):
    from bump import worker as worker_module

    client = FakeClient()
    monkeypatch.setattr(worker_module, "make_mqtt_client", lambda *_: client)
    device = make_device()
    event = observation(device)
    calls = []

    class FakeStore:
        def ingest_event(self, incoming, topic_device_id):
            calls.append((incoming, topic_device_id))
            if len(calls) == 1:
                raise RuntimeError("database unavailable")
            return ack_for(incoming, "stored" if len(calls) == 2 else "duplicate")

    worker = worker_module.Worker(settings, store=FakeStore(), drop_ack_count=1)
    message = SimpleNamespace(
        topic=device.events_topic, retain=False, payload=json.dumps(event).encode()
    )
    worker._on_message(client, None, message)
    assert client.published == []  # Storage failed.
    worker._on_message(client, None, message)
    assert client.published == []  # Commit succeeded; test hook loses its ACK.
    worker._on_message(client, None, message)
    assert client.published[0][1] == ack_for(event, "duplicate")
    assert worker.stats["store_failures"] == 1
    assert worker.stats["acks_dropped"] == 1


def test_worker_rejection_never_becomes_success(monkeypatch, settings, make_device):
    from bump import worker as worker_module

    client = FakeClient()
    monkeypatch.setattr(worker_module, "make_mqtt_client", lambda *_: client)
    device = make_device()
    event = observation(device)

    class RejectStore:
        def ingest_event(self, incoming, topic_device_id):
            raise ValueError("conflict")

    worker = worker_module.Worker(settings, store=RejectStore(), fail_store_count=1)
    message = SimpleNamespace(
        topic=device.events_topic, retain=False, payload=json.dumps(event).encode()
    )
    worker._on_message(client, None, message)
    assert client.published == []
    worker._on_message(client, None, message)
    rejection = client.published[0][1]
    assert rejection["status"] == "rejected"
    assert not device.handle_ack(rejection)
    assert device.queue_size == 1
