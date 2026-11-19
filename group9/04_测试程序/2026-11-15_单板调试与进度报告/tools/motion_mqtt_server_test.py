"""Isolated software checks: no serial port, Bluetooth or private-LAN listener."""

import asyncio
from contextlib import closing
import json
import socket
import sqlite3
import threading
from types import SimpleNamespace
import uuid

import paho.mqtt.client as mqtt
import pytest

from motion_mqtt_server import (
    AuthenticatedBroker, MotionStore, MotionWorker, broker_config, init_runtime,
)


def sample():
    return {
        "v": 1, "device_id": "badge-a", "boot_id": str(uuid.uuid4()), "seq": 1,
        "event": "motion_trigger", "source": "injected_test", "transport": "wifi_mqtt",
        "uptime_ms": 1234, "accel_g": [0.0, 0.0, 2.2],
        "state": "DISCOVERY", "window_ms": 60000,
    }


def row_count(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT count(*) FROM telemetry").fetchone()[0]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_store_persists_deduplicates_and_rejects_conflicts_or_fabricated_fields(tmp_path):
    path = tmp_path / "telemetry.sqlite3"
    store = MotionStore(path)
    event = sample()
    assert store.ingest_event(event, "badge-a")["status"] == "stored"
    reopened = MotionStore(path)
    assert row_count(path) == 1
    assert reopened.ingest_event(dict(reversed(list(event.items()))), "badge-a")["status"] == "duplicate"
    for change in ({"accel_g": [0, 0, 3]}, {"transport": "serial_bridge"}):
        with pytest.raises(ValueError):
            reopened.ingest_event({**event, **change}, "badge-a")
    for change in (
        {"peer_alias": "B00002"}, {"rssi_median": -55}, {"seq": True},
        {"accel_g": [0, float("nan"), 1]}, {"boot_id": "not-a-uuid"},
        {"statistics": {"rssi": -55}}, {"source": "simulated"},
    ):
        with pytest.raises(ValueError):
            reopened.ingest_event({**event, **change}, "badge-a")
    with pytest.raises(ValueError):
        reopened.ingest_event(event, "badge-b")
    assert row_count(path) == 1


def test_worker_only_acknowledges_committed_data_and_never_acknowledges_failure(tmp_path):
    store = MotionStore(tmp_path / "telemetry.sqlite3")
    worker = MotionWorker.__new__(MotionWorker)
    worker.store = store
    worker.stats = dict.fromkeys(
        ("received", "stored", "duplicates", "rejected", "store_failures", "acks_published"), 0,
    )
    worker.last_error = None
    sent = []

    def publish(topic, payload, qos, retain):
        assert row_count(store.path) == 1  # A separate connection sees the committed row.
        sent.append((topic, json.loads(payload), qos, retain))
        return SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)

    client = SimpleNamespace(publish=publish)
    event = sample()
    message = SimpleNamespace(topic="bump/v1/device/badge-a/events",
                              payload=json.dumps(event).encode(), retain=False)
    worker._on_message(client, None, message)
    assert sent[0][1] == {key: event[key] for key in ("v", "device_id", "boot_id", "seq")} | {"status": "stored"}
    assert sent[0][2:] == (1, False)
    worker._on_message(client, None, message)
    assert sent[-1][1]["status"] == "duplicate"
    worker._on_message(client, None, SimpleNamespace(
        topic=message.topic, payload=json.dumps({**event, "uptime_ms": 9999}).encode(), retain=False,
    ))
    assert len(sent) == 2 and worker.stats["rejected"] == 1

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("Injected unavailable database")

    worker.store = SimpleNamespace(ingest_event=fail)
    worker._on_message(client, None, message)
    assert len(sent) == 2 and worker.stats["store_failures"] == 1
    worker._on_message(client, None, SimpleNamespace(
        topic=message.topic, payload=message.payload, retain=True,
    ))
    assert len(sent) == 2


def test_tcp_to_tls_worker_roundtrip_authentication_and_exact_ack(tmp_path):
    async def check():
        tcp_port, tls_port = free_port(), free_port()
        while tls_port == tcp_port:
            tls_port = free_port()
        settings = init_runtime(tmp_path / "runtime", web_port=8766, mqtt_port=tls_port)
        configuration = broker_config(settings, "127.0.0.1", tcp_port)
        broker = AuthenticatedBroker(configuration)
        assert broker.listeners_config["default"].ssl is False
        assert broker.listeners_config["loopback_tls"].ssl is True
        store = MotionStore(tmp_path / "telemetry.sqlite3")
        worker = MotionWorker(settings, store=store)
        publisher = None
        bad = None
        await broker.start()
        try:
            assert await asyncio.to_thread(worker.start, 5)
            credentials = settings["mqtt"]["accounts"]["badge-a"]
            connected, subscribed, received = threading.Event(), threading.Event(), threading.Event()
            messages = []
            publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="badge-a",
                                    clean_session=True, reconnect_on_failure=False)
            publisher.username_pw_set("badge-a", credentials["password"])

            def on_connect(client, userdata, flags, reason, properties):
                if reason == 0:
                    connected.set()
                    client.subscribe("bump/v1/device/badge-a/ack", qos=1)

            def on_subscribe(client, userdata, mid, reasons, properties):
                if reasons and all(reason.value == 1 for reason in reasons):
                    subscribed.set()

            def on_message(client, userdata, message):
                messages.append((json.loads(message.payload), message.retain))
                received.set()

            publisher.on_connect = on_connect
            publisher.on_subscribe = on_subscribe
            publisher.on_message = on_message
            publisher.connect("127.0.0.1", tcp_port, 10)
            publisher.loop_start()
            assert await asyncio.to_thread(connected.wait, 3)
            assert await asyncio.to_thread(subscribed.wait, 3)
            event = sample()
            publisher.publish("bump/v1/device/badge-a/events", json.dumps(event), qos=1, retain=False)
            assert await asyncio.to_thread(received.wait, 3)
            assert messages[-1] == (
                {key: event[key] for key in ("v", "device_id", "boot_id", "seq")} | {"status": "stored"},
                False,
            )
            assert row_count(store.path) == 1
            received.clear()
            publisher.publish("bump/v1/device/badge-a/events", json.dumps(event), qos=1, retain=False)
            assert await asyncio.to_thread(received.wait, 3)
            assert messages[-1][0]["status"] == "duplicate" and row_count(store.path) == 1

            denied = threading.Event()
            bad = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="badge-a",
                              clean_session=True, reconnect_on_failure=False)
            bad.username_pw_set("badge-a", "deliberately-wrong-test-password")
            bad.on_disconnect = lambda *args: denied.set()
            bad.connect("127.0.0.1", tcp_port, 10)
            bad.loop_start()
            assert await asyncio.to_thread(denied.wait, 3)
            assert not bad.is_connected()
            assert publisher.is_connected()
        finally:
            for client in (bad, publisher):
                if client is not None:
                    client.disconnect()
                    await asyncio.to_thread(client.loop_stop)
            await asyncio.to_thread(worker.stop)
            await broker.shutdown()

    asyncio.run(check())
