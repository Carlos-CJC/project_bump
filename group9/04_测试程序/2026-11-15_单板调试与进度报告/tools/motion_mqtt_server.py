"""Isolated motion telemetry: authenticated private-LAN MQTT plus loopback TLS."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

TASK_ROOT = Path(__file__).resolve().parents[1]
STARTER = TASK_ROOT.parent / "Bump_Starter"
sys.path.insert(0, str(STARTER))

from bump.broker import AuthenticatedBroker
from bump.config import init_runtime, load_settings
from bump.worker import Worker
import paho.mqtt.client as mqtt

DEFAULT_RUNTIME = TASK_ROOT / "private" / "motion_mqtt_runtime"
REQUIRED = {
    "v", "device_id", "boot_id", "seq", "event", "source", "transport",
    "uptime_ms", "accel_g",
}
OPTIONAL = {"gyro", "statistics", "state", "window_ms"}
MAX_INTEGER = 2**63 - 1


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def validate_motion(event, topic_device_id):
    if not isinstance(event, dict) or not REQUIRED <= event.keys():
        raise ValueError("Missing motion fields")
    if event.keys() - REQUIRED - OPTIONAL:
        raise ValueError("Unexpected motion fields; peer and RSSI data are not accepted")
    if type(event["v"]) is not int or event["v"] != 1:
        raise ValueError("Unsupported motion version")
    if event["device_id"] != "badge-a" or topic_device_id != "badge-a":
        raise ValueError("Device does not match the authenticated topic")
    boot_id = event["boot_id"]
    if not isinstance(boot_id, str) or len(boot_id) != 36:
        raise ValueError("boot_id must be a UUID")
    try:
        if str(uuid.UUID(boot_id)) != boot_id.lower():
            raise ValueError("boot_id must be a canonical UUID")
    except (ValueError, AttributeError) as exc:
        raise ValueError("boot_id must be a UUID") from exc
    for field, minimum in (("seq", 1), ("uptime_ms", 0), ("window_ms", 0)):
        if field in event:
            value = event[field]
            if type(value) is not int or not minimum <= value <= MAX_INTEGER:
                raise ValueError(f"Invalid {field}")
    if event["event"] not in ("imu_sample", "motion_trigger"):
        raise ValueError("Unsupported motion event")
    if event["source"] not in ("hardware", "injected_test"):
        raise ValueError("Unsupported source")
    if event["transport"] not in ("wifi_mqtt", "serial_bridge"):
        raise ValueError("Unsupported transport")
    for field in ("accel_g", "gyro"):
        if field in event:
            values = event[field]
            if not isinstance(values, list) or len(values) != 3 or not all(
                finite_number(value) for value in values
            ):
                raise ValueError(f"{field} must contain three finite numbers")
    if "state" in event and event["state"] not in ("IDLE", "DISCOVERY", "SYNC", "PAUSED"):
        raise ValueError("Invalid state")
    if "statistics" in event:
        values = event["statistics"]
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and 0 < len(key) <= 64
            and "peer" not in key.lower() and "rssi" not in key.lower()
            and finite_number(value) for key, value in values.items()
        ):
            raise ValueError("statistics must contain finite numeric measurements")
    return dict(event)


class MotionStore:
    """An independent telemetry table; never manufactures encounter records."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS telemetry (
                device_id TEXT NOT NULL, boot_id TEXT NOT NULL, seq INTEGER NOT NULL,
                event TEXT NOT NULL, source TEXT NOT NULL, transport TEXT NOT NULL,
                uptime_ms INTEGER NOT NULL, received_utc TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                PRIMARY KEY(device_id, boot_id, seq))""")

    def ingest_event(self, event, topic_device_id):
        data = validate_motion(event, topic_device_id)
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        key = tuple(data[field] for field in ("device_id", "boot_id", "seq"))
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT canonical_json FROM telemetry WHERE device_id=? AND boot_id=? AND seq=?",
                key,
            ).fetchone()
            if previous:
                if previous[0] != canonical:
                    raise ValueError("Conflicting content for an existing event key")
                status = "duplicate"
            else:
                db.execute(
                    "INSERT INTO telemetry VALUES (?,?,?,?,?,?,?,?,?)",
                    (*key, data["event"], data["source"], data["transport"],
                     data["uptime_ms"], utc_now(), canonical),
                )
                status = "stored"
        # Both the transaction and connection have finished before an ACK is returned.
        return dict(v=1, device_id=key[0], boot_id=key[1], seq=key[2], status=status)


class MotionWorker(Worker):
    """Reuse authenticated subscription/lifecycle, with motion-specific ACKs."""

    def _on_message(self, client, userdata, message):
        if message.topic != "bump/v1/device/badge-a/events" or message.retain:
            return
        self.stats["received"] += 1
        try:
            event = json.loads(message.payload)
            ack = self.store.ingest_event(event, "badge-a")
        except (ValueError, UnicodeDecodeError):
            self.stats["rejected"] += 1
            return
        except Exception as exc:
            self.stats["store_failures"] += 1
            self.last_error = f"Storage unavailable ({type(exc).__name__})"
            return
        self.stats["stored" if ack["status"] == "stored" else "duplicates"] += 1
        self.last_error = None
        published = client.publish(
            "bump/v1/device/badge-a/ack", json.dumps(ack, separators=(",", ":")),
            qos=1, retain=False,
        )
        if published.rc == mqtt.MQTT_ERR_SUCCESS:
            self.stats["acks_published"] += 1


def broker_config(settings, bind_ip, port):
    address = ipaddress.IPv4Address(bind_ip)
    private_networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if not address.is_loopback and not any(
        address in ipaddress.ip_network(network) for network in private_networks
    ):
        raise ValueError("Bind to a specific private IPv4 or loopback address")
    connection = settings["mqtt"]
    if not 1024 <= port <= 65535 or port == connection["port"]:
        raise ValueError("MQTT ports must be different unprivileged ports")
    return {
        # amqtt inherits fields equal to defaults. Keep the non-TLS listener default,
        # so its ssl=False cannot accidentally inherit ssl=True from another listener.
        "listeners": {
            "default": {"type": "tcp", "bind": f"{address}:{port}",
                        "ssl": False, "max_connections": 4},
            "loopback_tls": {
                "type": "tcp", "bind": f"127.0.0.1:{connection['port']}",
                "ssl": True, "max_connections": 4,
                "cafile": connection["ca_file"], "certfile": connection["cert_file"],
                "keyfile": connection["key_file"],
            },
        },
        "plugins": {
            "bump.broker.LocalAuthPlugin": {
                "accounts": {name: connection["accounts"][name]
                             for name in ("badge-a", "_worker")}
            },
            "bump.broker.LocalTopicPlugin": {"devices": ["badge-a"]},
        },
    }


def checked_runtime(path):
    runtime = Path(path).resolve()
    private_root = (TASK_ROOT / "private").resolve()
    if runtime == private_root or not runtime.is_relative_to(private_root):
        raise ValueError("Runtime must be a child directory of this task's private folder")
    return runtime


def initialize(runtime, bind_ip, port, tls_port):
    runtime = checked_runtime(runtime)
    settings = init_runtime(runtime, web_port=8766, mqtt_port=tls_port)
    if settings["mqtt"]["port"] != tls_port:
        raise ValueError("Existing runtime uses a different TLS port")
    broker_config(settings, bind_ip, port)
    description = {"bind_ip": bind_ip, "port": port, "tls_port": tls_port}
    target = runtime / "motion_server.json"
    if target.exists() and json.loads(target.read_text(encoding="utf-8")) != description:
        raise ValueError("Existing runtime has different listener settings; use a new runtime")
    target.write_text(json.dumps(description, indent=2), encoding="utf-8")
    return settings, description


def write_status(runtime, data):
    temporary = runtime / "ready.json.tmp"
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(runtime / "ready.json")


async def run_server(runtime):
    settings = load_settings(runtime)
    description = json.loads((runtime / "motion_server.json").read_text(encoding="utf-8"))
    broker = AuthenticatedBroker(broker_config(
        settings, description["bind_ip"], description["port"],
    ))
    lock = runtime / "server.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    stop_file = runtime / "stop.request"
    worker = None
    started = False
    try:
        stop_file.unlink(missing_ok=True)
        store = MotionStore(runtime / "telemetry.sqlite3")
        worker = MotionWorker(settings, store=store)
        await broker.start()
        started = True
        if not await asyncio.to_thread(worker.start, 8):
            raise RuntimeError("Telemetry worker did not become ready")
        print(json.dumps({"status": "ready", "pid": os.getpid(), **description}), flush=True)
        while not stop_file.exists():
            write_status(runtime, {
                "ready": worker.health, "pid": os.getpid(), "updated_utc": utc_now(),
                **description, "stats": dict(worker.stats),
            })
            await asyncio.sleep(0.5)
    finally:
        if worker is not None:
            await asyncio.to_thread(worker.stop)
        if started:
            await broker.shutdown()
        (runtime / "ready.json").unlink(missing_ok=True)
        stop_file.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
        print(json.dumps({"status": "stopped"}), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "run", "status", "stop"))
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--bind-ip", default="192.168.50.81")
    parser.add_argument("--port", type=int, default=18884)
    parser.add_argument("--tls-port", type=int, default=18885)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    try:
        runtime = checked_runtime(args.runtime)
        if args.command == "init":
            _, description = initialize(runtime, args.bind_ip, args.port, args.tls_port)
            print(json.dumps({"status": "initialized", "runtime": str(runtime), **description}))
        elif args.command == "run":
            asyncio.run(run_server(runtime))
        elif args.command == "stop":
            if (runtime / "server.lock").exists():
                (runtime / "stop.request").write_text("stop\n", encoding="utf-8")
                print(json.dumps({"status": "stop_requested"}))
            else:
                print(json.dumps({"status": "not_running"}))
        else:
            target = runtime / "ready.json"
            state = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
            state["ready"] = bool(state.get("ready") and time.time() - target.stat().st_mtime < 5)
            print(json.dumps(state))
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
