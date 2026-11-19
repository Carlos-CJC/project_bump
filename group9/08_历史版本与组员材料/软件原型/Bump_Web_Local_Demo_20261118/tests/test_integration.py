"""Real TLS MQTT integration; synthetic observations and in-process HTTP API."""

import copy
import json
import socket
import ssl
import threading
import time
from contextlib import ExitStack

import paho.mqtt.client as mqtt
import pytest
from fastapi.testclient import TestClient

from bump.api import create_app
from bump.broker import BrokerThread
from bump.config import init_runtime
from bump.device import DeviceSimulator
from bump.simulator import run_demo
from bump.store import Store
from bump.worker import Worker
from test_store import event


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def eventually(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


@pytest.fixture
def settings(tmp_path):
    return init_runtime(tmp_path / "runtime", mqtt_port=free_port())


@pytest.fixture
def broker(settings):
    with BrokerThread(settings) as active:
        yield active


class Probe:
    """Small real MQTT client for authentication and delivery assertions."""

    def __init__(self, settings, account, *, password=None, client_id=None,
                 ca_file=None, will=False, clean_session=True):
        self.settings = settings
        credentials = settings["mqtt"]["accounts"][account]
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or credentials["client_id"],
            protocol=mqtt.MQTTv311, clean_session=clean_session,
            reconnect_on_failure=False,
        )
        self.client.username_pw_set(
            account, credentials["password"] if password is None else password
        )
        self.client.tls_set_context(ssl.create_default_context(
            cafile=ca_file or settings["mqtt"]["ca_file"]
        ))
        self.client.connect_timeout = 2
        if will:
            self.client.will_set(f"bump/v1/device/{account}/ack", "{}", qos=1)
        self.connected = False
        self.outcome = threading.Event()
        self.subacks = {}
        self.messages = []
        self._running = False
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.connected = reason_code == 0
        self.outcome.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        self.outcome.set()

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        self.subacks[mid] = [code.value for code in reason_codes]

    def _on_message(self, client, userdata, message):
        self.messages.append((message.topic, json.loads(message.payload)))

    def connect(self):
        connection = self.settings["mqtt"]
        self.client.connect(connection["host"], connection["port"], 10)
        self.client.loop_start()
        self._running = True
        assert self.outcome.wait(3), "MQTT connection produced no result."
        return self.connected

    def subscribe(self, topic):
        result, mid = self.client.subscribe(topic, qos=1)
        assert result == mqtt.MQTT_ERR_SUCCESS
        assert eventually(lambda: mid in self.subacks, timeout=3), "SUBACK timed out."
        return self.subacks[mid]

    def publish(self, topic, payload):
        result = self.client.publish(topic, json.dumps(payload), qos=1, retain=False)
        assert result.rc == mqtt.MQTT_ERR_SUCCESS
        # ACL assertions below use observed delivery, never PUBACK as authorization.
        return result

    def close(self):
        self.client.disconnect()
        if self._running:
            self.client.loop_stop()
            self._running = False


def login(stack, app, settings, username):
    client = stack.enter_context(TestClient(app))
    password = next(user["password"] for user in settings["users"] if user["id"] == username)
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


@pytest.mark.parametrize("scenario", ["normal", "offline", "retry"])
def test_real_mqtt_scenarios_reach_durable_store_and_authorized_api(settings, broker, scenario):
    worker = Worker(settings)
    try:
        assert worker.start()
        result = run_demo(settings, scenario)
        assert result["ok"]
        assert result["source"] == "simulated" and result["hardware_verified"] is False
        assert all(item["queue_size"] == 0 for item in result["devices"])
        if scenario == "offline":
            assert result["connected_before_outage"] == [True, True]
            assert result["offline_pending"] == [1, 1]
            assert all(item["state"] == "PAUSED" for item in result["devices"])
        if scenario == "retry":
            assert result["devices"][0]["dropped_ack_count"] == 1
            assert result["devices"][0]["acks"][-1]["status"] == "duplicate"
            assert worker.stats["duplicates"] >= 1
        assert worker.stats["stored"] == 2

        reopened = Store(settings)
        for username in ("alice", "bob"):
            items = reopened.list_encounters(username)
            assert len(items) == 1 and items[0]["mutual"]
            assert items[0]["source"] == "simulated" and items[0]["profile"] is None

        app = create_app(settings)
        with ExitStack() as stack:
            alice, bob, charlie = [
                login(stack, app, settings, user) for user in ("alice", "bob", "charlie")
            ]
            items = alice.get("/api/encounters").json()["items"]
            assert len(items) == 1 and items[0]["profile"] is None
            assert "bob@example.invalid" not in json.dumps(items)
            response = alice.post("/api/requests", json={"encounter_id": items[0]["id"]})
            assert response.status_code == 200
            request_id = response.json()["item"]["id"]
            assert charlie.post(f"/api/requests/{request_id}/accept").status_code == 404
            assert alice.post(f"/api/requests/{request_id}/accept").status_code == 403
            assert bob.post(f"/api/requests/{request_id}/accept").status_code == 200
            shared = alice.get("/api/encounters").json()["items"][0]["profile"]
            assert shared["contact"] == "bob@example.invalid"
    finally:
        worker.stop()


def test_broker_restart_store_failure_and_lost_ack_recover_while_paused(settings):
    broker = BrokerThread(settings).start()
    worker = Worker(settings, drop_ack_count=1, fail_store_count=1)
    badge = DeviceSimulator(settings, "badge-a")
    try:
        assert worker.start() and badge.connect()
        assert badge.shake(now=0)
        assert badge.observe("B00002") is not None
        badge.tick(now=60)
        badge.pause()
        broker.stop()
        assert eventually(lambda: not badge.health and not worker.health, timeout=3)
        assert not badge.sync(timeout=0.2)
        assert badge.queue_size == 1
        broker = BrokerThread(settings).start()
        assert badge.sync(timeout=10)
        assert badge.state == "PAUSED" and badge.queue_size == 0
        assert badge.ack_history[-1]["status"] == "duplicate"
        assert worker.stats["store_failures"] == 1
        assert worker.stats["stored"] == 1
        assert worker.stats["acks_dropped"] == 1
        assert worker.stats["duplicates"] >= 1
        assert len(Store(settings).list_encounters("alice")) == 1
    finally:
        badge.close()
        worker.stop()
        broker.stop()


@pytest.mark.parametrize("fault", [
    "wrong_password", "wrong_client_id", "will", "persistent_session",
])
def test_real_broker_rejects_invalid_authentication_or_will(settings, broker, fault):
    options = {
        "wrong_password": {"password": "intentionally-invalid-test-value"},
        "wrong_client_id": {"client_id": "not-the-account-client"},
        "will": {"will": True},
        "persistent_session": {"clean_session": False},
    }
    probe = Probe(settings, "badge-a", **options[fault])
    try:
        assert not probe.connect()
        assert not probe.connected
    finally:
        probe.close()


@pytest.mark.parametrize("attempt", ["wrong_password", "mismatched_account"])
def test_failed_duplicate_id_connection_preserves_the_live_session(settings, broker, attempt):
    worker = Worker(settings)
    original = Probe(settings, "badge-a")
    if attempt == "wrong_password":
        duplicate = Probe(settings, "badge-a", password="invalid-duplicate-test-value")
    else:
        duplicate = Probe(
            settings, "badge-b", client_id="badge-a", clean_session=False,
        )
    try:
        assert worker.start() and original.connect()
        assert original.subscribe("bump/v1/device/badge-a/ack") == [1]
        assert not duplicate.connect()
        assert original.connected
        original.publish(
            "bump/v1/device/badge-a/events", event(boot_id="session-preserved")
        )
        assert eventually(lambda: any(
            value.get("boot_id") == "session-preserved"
            and value.get("status") == "stored" for _, value in original.messages
        ))
        assert original.connected and worker.stats["stored"] == 1
        assert len(Store(settings).list_encounters("alice")) == 1
        assert Store(settings).list_encounters("bob") == []
    finally:
        duplicate.close()
        original.close()
        worker.stop()


def test_legitimate_clean_disconnect_and_new_client_reconnect(settings, broker):
    worker = Worker(settings)
    first = Probe(settings, "badge-a")
    second = Probe(settings, "badge-a")
    try:
        assert worker.start() and first.connect()
        assert first.subscribe("bump/v1/device/badge-a/ack") == [1]
        first.publish("bump/v1/device/badge-a/events", event(boot_id="before-reconnect"))
        assert eventually(lambda: len(first.messages) == 1)
        first.close()
        assert not first.connected

        assert second.connect()
        assert second.subscribe("bump/v1/device/badge-a/ack") == [1]
        second.publish("bump/v1/device/badge-a/events", event(boot_id="after-reconnect"))
        assert eventually(lambda: any(
            value.get("boot_id") == "after-reconnect"
            and value.get("status") == "stored" for _, value in second.messages
        ))
        assert worker.stats["stored"] == 2
        assert len(Store(settings).list_encounters("alice")) == 2
    finally:
        first.close()
        second.close()
        worker.stop()


def test_real_tls_rejects_a_different_runtime_ca(settings, broker, tmp_path):
    unrelated = init_runtime(tmp_path / "different-ca", mqtt_port=free_port())
    probe = Probe(settings, "badge-a", ca_file=unrelated["mqtt"]["ca_file"])
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            probe.connect()
    finally:
        probe.close()


@pytest.mark.parametrize("topic", [
    "bump/v1/device/badge-b/ack",
    "bump/v1/device/+/events",
    "bump/#",
])
def test_device_cannot_subscribe_to_other_accounts_or_event_wildcards(settings, broker, topic):
    probe = Probe(settings, "badge-a")
    try:
        assert probe.connect()
        assert probe.subscribe("bump/v1/device/badge-a/ack") == [1]
        assert probe.subscribe(topic) == [128]
    finally:
        probe.close()


@pytest.mark.parametrize("forbidden", ["other_events", "own_ack", "other_ack"])
def test_forbidden_publications_are_not_delivered_or_stored(settings, broker, forbidden):
    worker = Worker(settings)
    alice, bob = Probe(settings, "badge-a"), Probe(settings, "badge-b")
    try:
        assert worker.start() and alice.connect() and bob.connect()
        assert alice.subscribe("bump/v1/device/badge-a/ack") == [1]
        assert bob.subscribe("bump/v1/device/badge-b/ack") == [1]
        forged = {
            "schema_version": 1, "device_id": "badge-b", "boot_id": "forged-ack",
            "seq": 1, "status": "stored",
        }
        if forbidden == "other_events":
            topic = "bump/v1/device/badge-b/events"
            payload = event("badge-b", boot_id="forbidden-event")
        else:
            target = "badge-a" if forbidden == "own_ack" else "badge-b"
            topic = f"bump/v1/device/{target}/ack"
            payload = copy.deepcopy(forged)
            payload["device_id"] = target
        alice.publish(topic, payload)

        # An authorized event on the same connection acts as a processing barrier.
        alice.publish("bump/v1/device/badge-a/events", event(boot_id="acl-barrier"))
        assert eventually(lambda: any(
            value.get("boot_id") == "acl-barrier" for _, value in alice.messages
        ))
        # Wait for the second subscriber too, so its absence assertion cannot
        # pass merely because that client's network thread has not run yet.
        bob.publish(
            "bump/v1/device/badge-b/events",
            event("badge-b", boot_id="acl-b-barrier"),
        )
        assert eventually(lambda: any(
            value.get("boot_id") == "acl-b-barrier" for _, value in bob.messages
        ))
        assert worker.stats["received"] == 2
        assert worker.stats["stored"] == 2
        assert [value["boot_id"] for _, value in bob.messages] == ["acl-b-barrier"]
        assert [value["boot_id"] for _, value in alice.messages] == ["acl-barrier"]
        assert len(Store(settings).list_encounters("bob")) == 1
        assert len(Store(settings).list_encounters("alice")) == 1
    finally:
        alice.close()
        bob.close()
        worker.stop()
