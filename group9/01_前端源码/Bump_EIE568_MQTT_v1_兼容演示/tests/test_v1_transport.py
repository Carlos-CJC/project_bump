"""The compatibility lane on real loopback TLS. Only generated fictional identities."""

import asyncio
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace

import paho.mqtt.client as mqtt
import pytest
from amqtt.contexts import Action

from bump.broker import BrokerThread, LocalAuthPlugin, LocalTopicPlugin
from bump.config import V1_DEMO_DEVICES, V1_MQTT_DEVICES, V1_TOPIC_FILTERS, V1_WORKER_ACCOUNT, init_runtime
from bump.device import make_mqtt_client
from bump.simulator import _exclusive_run
from bump.v1_store import V1Store
from bump.v1_transport import DemoBusy, TransportUnavailable, V1Transport, _publisher, _software_packets


A, B = V1_MQTT_DEVICES


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def settings(tmp_path):
    return init_runtime(tmp_path / "private", mqtt_port=free_port())


class Audit:
    """Test-only trace of actual subscriber callbacks, never a publisher shortcut."""

    def __init__(self, path, *, fail_events=False):
        self.db = V1Store(path)
        self.trace = []
        self.changed = threading.Condition()
        self.fail_events = fail_events

    def contains_event_ids(self, ids):
        return self.db.contains_event_ids(ids)

    def ingest(self, parsed, *, received_at):
        with self.changed:
            self.trace.append(parsed)
            self.changed.notify_all()
        if self.fail_events and parsed["kind"] == "encounters":
            raise OSError("test-only local storage outage")
        return self.db.ingest(parsed, received_at=received_at)

    def wait_for(self, predicate):
        with self.changed:
            assert self.changed.wait_for(lambda: predicate(self.trace), timeout=4)


@contextmanager
def running(settings, store):
    transport = V1Transport(settings, store)
    try:
        assert transport.start(timeout=5)
        yield transport
    finally:
        transport.stop()
    assert transport.health()["connected"] is False


def send_packet(client, device, kind, body, *, retain, qos=1):
    info = client.publish(f"bump/v1/device/{device}/{kind}", json.dumps(body), qos=qos, retain=retain)
    assert info.rc == mqtt.MQTT_ERR_SUCCESS
    info.wait_for_publish(3)  # broker transport only, never the storage assertion.
    assert info.is_published()


def test_v1_credentials_per_install_and_old_config_stays_read_only(settings, tmp_path):
    other = init_runtime(tmp_path / "also-private", mqtt_port=free_port())
    accounts = settings["mqtt"]["accounts"]
    assert set(accounts) == {"badge-a", "badge-b", "_worker", V1_WORKER_ACCOUNT, A, B}
    assert len({p["client_id"] for p in accounts.values()}) == len(accounts)
    assert len({p["password"] for p in accounts.values()}) == len(accounts)
    for user in (*V1_MQTT_DEVICES, V1_WORKER_ACCOUNT):
        assert bool(accounts[user]["password"] != other["mqtt"]["accounts"][user]["password"])
    assert all(accounts[d]["client_id"] == d for d in (A, B))

    # Model an existing legacy install without provisioning or reading real user data.
    old_dir = Path(other["runtime_dir"])
    file = old_dir / "config.json"
    legacy = json.loads(file.read_bytes())
    for key in (*V1_MQTT_DEVICES, V1_WORKER_ACCOUNT):
        del legacy["mqtt"]["accounts"][key]
    file.write_text(json.dumps(legacy), encoding="utf-8")
    before = sha256(file.read_bytes()).digest()
    old = V1Transport(init_runtime(old_dir), object())
    assert not old.enabled and not old.start(timeout=0)
    old.stop()
    assert sha256(file.read_bytes()).digest() == before


def test_v1_auth_and_topic_permissions_are_disjoint(settings):
    accounts = settings["mqtt"]["accounts"]
    auth = LocalAuthPlugin(SimpleNamespace(config=LocalAuthPlugin.Config(accounts=accounts)))
    acl = LocalTopicPlugin(SimpleNamespace(config=LocalTopicPlugin.Config(devices=["badge-a", "badge-b"])))

    async def check():
        for user in (A, B, V1_WORKER_ACCOUNT):
            good = dict(username=user, password=accounts[user]["password"],
                        client_id=accounts[user]["client_id"], clean_session=True, will_flag=False)
            assert await auth.authenticate(session=SimpleNamespace(**good))
            for changes in ({"client_id": "not-registered"}, {"clean_session": False},
                            {"will_flag": True}, {"password": "unprovisioned-test-value"}):
                assert not await auth.authenticate(session=SimpleNamespace(**(good | changes)))
        for user in (A, B, V1_WORKER_ACCOUNT, "badge-a", "_worker"):
            session = SimpleNamespace(username=user)
            for d in (A, B, "badge-a", "unprovisioned"):
                for suffix in ("encounters", "status", "ack", "events", "config"):
                    topic = f"bump/v1/device/{d}/{suffix}"
                    pub = await acl.topic_filtering(session=session, topic=topic, action=Action.PUBLISH)
                    receive = await acl.topic_filtering(session=session, topic=topic, action=Action.RECEIVE)
                    assert pub == (user == d and d in (A, B) and suffix in ("encounters", "status")
                                   or user == "badge-a" and d == "badge-a" and suffix == "events"
                                   or user == "_worker" and d == "badge-a" and suffix == "ack")
                    assert receive == (user == V1_WORKER_ACCOUNT and d in (A, B) and suffix in ("encounters", "status")
                                       or user == "badge-a" and d == "badge-a" and suffix == "ack"
                                       or user == "_worker" and d == "badge-a" and suffix == "events")
            for topic in (*V1_TOPIC_FILTERS, "bump/v1/device/+/events", "bump/#"):
                allowed = await acl.topic_filtering(session=session, topic=topic, action=Action.SUBSCRIBE)
                assert allowed == (user == V1_WORKER_ACCOUNT and topic in V1_TOPIC_FILTERS
                                    or user == "_worker" and topic == "bump/v1/device/+/events")
    asyncio.run(check())


def test_two_v1_runs_reopen_mutual_private_aggregates_and_report_only_storage(settings, tmp_path):
    path = tmp_path / "v1.sqlite3"
    audit = Audit(path)
    with BrokerThread(settings), running(settings, audit) as transport:
        assert transport.health() == {"connected": True, "last_message_at": None}
        with pytest.raises(PermissionError):
            transport.run_demo("charlie")
        with _exclusive_run(settings), pytest.raises(DemoBusy):
            transport.run_demo("alice")
        assert audit.trace == []
        for user in V1_DEMO_DEVICES:
            assert transport.run_demo(user) == {"persisted": True, "event_count": 2}
        assert transport.health()["last_message_at"] is not None
    batches = [item["body"] for item in audit.trace if item["kind"] == "encounters"]
    assert len(batches) == 4
    ids = [body["events"][0]["event_id"] for body in batches]
    assert len(set(ids)) == 4 and V1Store(path).contains_event_ids(ids)
    assert len({body["self_short_id"] for body in batches}) == 4
    for pair in (batches[:2], batches[2:]):
        first, second = sorted(pair, key=lambda p: p["device_id"])
        assert first["self_short_id"] == second["events"][0]["peer_short_id"]
        assert second["self_short_id"] == first["events"][0]["peer_short_id"]
    for owner, peer_alias in (("alice", "B00002"), ("bob", "A00001")):
        snapshot = V1Store(path).snapshot(owner, now=time.time())
        assert len(snapshot["items"]) == 1
        card = snapshot["items"][0]
        assert (card["event_count"], card["source"], card["peer_alias"], card["mutual"]) == (
            2, "simulated", peer_alias, True)
        assert snapshot["device"]["online"] is False and snapshot["device"]["queue_depth"] == 0
    assert V1Store(path).snapshot("charlie", now=time.time()) == {"device": None, "items": []}


def test_retained_and_qos0_encounters_never_reach_store_and_only_status_replays(settings, tmp_path):
    initial, later = Audit(tmp_path / "initial.sqlite3"), Audit(tmp_path / "later.sqlite3")
    online, denied, _ = _software_packets(A, "3f9a01c47b2e", "8c21d0a9e4f5", "01020304")
    _, too_low, _ = _software_packets(A, "51bb01c47b2e", "8c21d0a9e4f5", "02220304")
    _, allowed, _ = _software_packets(A, "52bb01c47b2e", "8c21d0a9e4f5", "03330304")
    with BrokerThread(settings):
        with running(settings, initial), _publisher(settings, A, time.monotonic() + 5) as badge:
            send_packet(badge, A, "status", online, retain=True)
            send_packet(badge, A, "encounters", denied, retain=True)
            send_packet(badge, A, "encounters", too_low, retain=False, qos=0)
            send_packet(badge, A, "encounters", allowed, retain=False)
            initial.wait_for(lambda t: any(p["body"].get("boot_id") == "03330304" for p in t))
        assert [p["kind"] for p in initial.trace] == ["status", "encounters"]
        assert not initial.trace[0]["retained"]  # LIVE delivery clears RETAIN.
        assert initial.contains_event_ids([allowed["events"][0]["event_id"]])
        assert not initial.contains_event_ids([denied["events"][0]["event_id"]])
        assert not initial.contains_event_ids([too_low["events"][0]["event_id"]])
        with running(settings, later):
            later.wait_for(lambda t: t)
        assert [(p["kind"], p["retained"], p["body"]["state"]) for p in later.trace] == [
            ("status", True, "online")]
        assert later.db.snapshot("alice", now=time.time())["device"]["online"] is False


def test_live_broker_never_sends_retained_status_to_connected_badges(settings, tmp_path):
    with BrokerThread(settings), running(settings, Audit(tmp_path / "v1.sqlite3")) as transport:
        assert transport.run_demo("alice")["persisted"]
        for account in ("badge-a", A):
            client = make_mqtt_client(settings, account)
            connected, suback = threading.Event(), threading.Event()
            messages, grants = [], []
            client.on_connect = lambda c, u, f, rc, p: connected.set() if rc == 0 else None
            client.on_message = lambda c, u, m: messages.append(m.topic)
            def on_subscribe(c, u, mid, codes, props):
                grants.append([code.value for code in codes])
                suback.set()
            client.on_subscribe = on_subscribe
            try:
                client.connect("127.0.0.1", settings["mqtt"]["port"], 60)
                client.loop_start()
                assert connected.wait(3)
                for _ in range(2):
                    suback.clear()
                    assert client.subscribe([(t, 1) for t in V1_TOPIC_FILTERS])[0] == mqtt.MQTT_ERR_SUCCESS
                    assert suback.wait(3)
                assert grants == [[128, 128], [128, 128]]
                assert messages == []
            finally:
                client.disconnect()
                client.loop_stop()


def test_puback_alone_is_not_durable_success(settings, tmp_path):
    audit = Audit(tmp_path / "unavailable.sqlite3", fail_events=True)
    with BrokerThread(settings), running(settings, audit) as transport:
        transport._DEMO_TIMEOUT = 2.0
        with pytest.raises(TransportUnavailable):
            transport.run_demo("alice")
        batches = [p["body"] for p in audit.trace if p["kind"] == "encounters"]
        assert len(batches) == 2  # Real QoS 1 delivery was insufficient.
        assert not audit.contains_event_ids([p["events"][0]["event_id"] for p in batches])
