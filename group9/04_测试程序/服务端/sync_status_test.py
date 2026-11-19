"""Exercise device sync outcomes on loopback and prove conservative legacy recovery."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sqlite3
from tempfile import TemporaryDirectory

from backfill_sync import verify
from bump.live_store import LiveStore
from live_server import ingest, load, uplink_topic
from smoke_test import (A, B, BOOT_A, SHORT_A, HERE, Client, command, http, opener,
                        select_ports, status_packet, wait_state, with_server)


def login(config, url):
    session = opener()
    account = next(user for user in config["users"] if user["id"] == "alice")
    http(session, url, "/api/login", method="POST",
         data={"username": "alice", "password": account["password"]})
    return lambda: http(session, url, "/api/device")["device"]


def packet(uptime, state="online", queued=0):
    return {**status_packet(A, BOOT_A, SHORT_A, state), "fw": "0.5.8-dual",
            "queue_depth": queued, "uptime_s": uptime}


def exercise(config, url):
    view = login(config, url)
    assert view()["sync_state"] == "unknown" and view()["last_synced_at"] is None
    with Client(config, A) as sender:
        sender.publish("status", packet(1, queued=3))
        pending = wait_state(view, lambda row: row["sync_state"] == "pending")
        assert pending["queue_depth"] == 3 and pending["last_synced_at"] is None
        sender.publish("status", packet(2, "offline", 2))
        pending = wait_state(view, lambda row: row["sync_source"] == "device_offline")
        assert pending["sync_state"] == "pending" and pending["queue_depth"] == 2
    with Client(config, A) as sender:
        sender.publish("status", packet(10))
        wait_state(view, lambda row: row["sync_state"] == "in_progress")
        sender.publish("status", packet(11, "offline"))
        done = wait_state(view, lambda row: row["sync_state"] == "synced")
        assert done["queue_depth"] == 0 and done["last_received_at"] is None
        success = done["last_synced_at"]
        assert success and success == done["sync_observed_at"]
    assert view()["sync_state"] == "synced"
    for uptime, source in [(20, "mqtt_will"), (30, "connection_lost"), (40, "incomplete")]:
        client = Client(config, A)
        if source == "mqtt_will":
            # Captured when connecting, this payload claims the queue was empty.
            client.client.will_set(uplink_topic(A, "status"), json.dumps(packet(uptime, "offline")),
                                   qos=1, retain=True)
        with client:
            client.publish("status", packet(uptime, queued=1))
            wait_state(view, lambda row: row["sync_state"] == "pending")
            if source != "incomplete":
                client.client.socket().shutdown(socket.SHUT_RDWR)
        closed = wait_state(view, lambda row: row["sync_source"] == source)
        assert closed["sync_state"] == ("unknown" if source == "incomplete" else "interrupted")
        assert closed["last_synced_at"] == success and closed["queue_depth"] is None
    return success


def legacy_test(runtime):
    store = LiveStore(load(runtime))
    db_path = store.path
    # An old service may leave offline+0 with no record of packet provenance.
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("DELETE FROM badge_sync WHERE device=?", (A,))
        db.execute("UPDATE live_status SET state='offline', queue_depth=0, uptime_s=50,"
                   " events_dropped=0,observed_at=100 WHERE device=?", (A,))
    unknown = store.device_status("alice")
    assert unknown["sync_state"] == "unknown" and unknown["last_synced_at"] is None
    status = packet(50, "offline")
    sent = {"type": "wire", "phase": "transmit", "source": "presence",
            "topic": uplink_topic(A, "status"), "payload": status}
    ack = {"type": "sync_stage", "stage": "presence_offline", "result": 1,
           "elapsed_ms": 50, "uptime_ms": 50100}
    end = {"type": "sync_complete", "queued": 0, "transport": "wifi_mqtt_tcp"}
    log = runtime / "synthetic-serial.jsonl"

    def write_log():
        items = zip((99.96,100.02,101), (sent,ack,end))
        log.write_text("".join(json.dumps({
            "utc": datetime.fromtimestamp(at, timezone.utc).isoformat(), "board": "A", "kind": "rx",
            "data": json.dumps(item),
        }) + "\n" for at, item in items), encoding="utf-8")

    for obj, key, wrong in ((status,"boot_id","000000ff"), (status,"queue_depth",1),
                            (status,"uptime_s",55), (ack,"result",0), (end,"queued",1)):
        original = obj[key]
        obj[key] = wrong
        write_log()
        assert not verify(db_path, log, True) and store.device_status("alice") == unknown
        obj[key] = original
    write_log()
    assert len(verify(db_path, log)) == 1 and store.device_status("alice") == unknown
    assert len(verify(db_path, log, True)) == 1 and len(verify(db_path, log, True)) == 1
    recovered = store.device_status("alice")
    assert recovered["sync_state"] == "synced" and recovered["sync_source"] == "verified_serial"
    # A receiver restart with an unfinished connection cannot create a success.
    body = json.dumps(packet(60, queued=1)).encode()
    ingest(store, uplink_topic(A, "status"), body, A, 110, "mqtt", mqtt_session_at=109)
    store.finish_mqtt_session(A, 108, 111, "mqtt_will")  # stale, superseded session
    assert store.device_status("alice")["sync_state"] == "pending"
    store.expire_open_reports(112)
    expired = store.device_status("alice")
    assert expired["sync_state"] == "unknown" and expired["last_synced_at"] == recovered["last_synced_at"]
    # USB dumps can carry an offline body, but cannot prove direct MQTT completion.
    ingest(store, uplink_topic(A, "status"), json.dumps(packet(61, "offline")).encode(), A, 113, "usb")
    assert store.device_status("alice")["sync_state"] == "unknown"


def main():
    mqtt_port, web_port = select_ports()
    url = f"http://127.0.0.1:{web_port}"
    with TemporaryDirectory(prefix="test-sync-", dir=HERE) as folder:
        runtime = Path(folder)
        command("init", "--runtime", runtime, "--device-a", A, "--device-b", B,
                "--mqtt-port", mqtt_port, "--web-port", web_port, capture_output=True)
        config = load(runtime)
        with_server(runtime / "service.log", runtime, url, lambda: exercise(config, url))
        def reopened():
            row = login(config, url)()
            assert row["sync_state"] == "unknown" and row["last_synced_at"]
        with_server(runtime / "reopened.log", runtime, url, reopened)
        legacy_test(runtime)
    print("PASS: loopback device queue, completion, LWT and no-will interruption, incomplete close,"
          " persisted completion, legacy evidence checks and replay, stale sessions, restart, USB.")


if __name__ == "__main__":
    main()
