"""Verify a fresh serial -> PC -> TLS MQTT -> SQLite telemetry run."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import sqlite3
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "pydeps"))
sys.path.insert(0, str(ROOT.parent / "Bump_Starter"))

import serial
from serial.tools import list_ports
import paho.mqtt.client as mqtt
from bump.config import load_settings
from bump.device import make_mqtt_client
from motion_mqtt_server import validate_motion

RUNTIME = ROOT / "private" / "motion_mqtt_runtime"
TARGET_SERIAL = "416523501C74AAC8"
ACK_FIELDS = {"v", "device_id", "boot_id", "seq", "status"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class BridgeRun:
    def __init__(self, port):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
        self.out = ROOT / "evidence" / "motion_mqtt" / "serial_bridge" / run_id
        self.out.mkdir(parents=True, exist_ok=False)
        self.port = port
        self.serial = None
        self.client = None
        self.pending = b""
        self.records = []
        self.trace_lock = threading.Lock()
        self.acks = queue.Queue()
        self.connected = threading.Event()
        self.subscribed = threading.Event()
        self.summary = {
            "started_utc": utc_now(), "boot_id": str(uuid.uuid4()),
            "program": "BumpSingleBoardMotionMqtt", "expected_firmware_version": 2,
            "transport": "serial_bridge", "mqtt_transport": "pc_loopback_tls",
            "board_wifi_upload_tested": False, "physical_motion_controlled": False,
            "external_modules_tested": False, "two_badges_tested": False,
        }
        self.save()

    def save(self):
        temporary = self.out / "summary.json.tmp"
        temporary.write_text(json.dumps(self.summary, indent=2), encoding="utf-8")
        temporary.replace(self.out / "summary.json")

    def stage(self, name):
        self.summary["stage"] = name
        self.save()
        print(json.dumps({"stage": name, "output_dir": str(self.out)}), flush=True)

    def trace(self, kind, **data):
        item = {"host_utc": utc_now(), "type": kind, **data}
        with self.trace_lock, (self.out / "mqtt_traces.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item) + "\n")

    def capture(self, label, seconds=3, until=None):
        found = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.pending += self.serial.read(self.serial.in_waiting or 1)
            while b"\n" in self.pending:
                raw, self.pending = self.pending.split(b"\n", 1)
                if not raw.strip():
                    continue
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeError):
                    data = {"type": "unparsed", "byte_count": len(raw)}
                if not isinstance(data, dict):
                    data = {"type": "unparsed", "byte_count": len(raw)}
                record = {"stage": label, "host_utc": utc_now(), "data": data}
                self.records.append(record)
                with (self.out / "serial_records.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record) + "\n")
                found.append(data)
                if until and data.get("type") == until:
                    return found
        if until:
            raise TimeoutError(f"{label}: no {until} received within {seconds:g} s")
        return found

    def command(self, command, label, until, seconds=3):
        self.serial.write((command + "\n").encode("ascii"))
        return self.capture(label, seconds, until)

    def status(self, label):
        data = self.command("status", label, "status")[-1]
        if data.get("firmware") != "BumpSingleBoardMotionMqtt" or data.get("firmware_version") != 2:
            raise RuntimeError("Expected the freshly uploaded motion firmware version 2")
        return data

    def setting(self, key, value):
        data = self.command(f"SET {key} {value}", "configuration_" + key, "configuration_set")[-1]
        if data.get("key") != key or data.get("ok") is not True:
            raise RuntimeError("Configuration was not accepted: " + key)

    def connect_serial(self):
        matches = [
            p for p in list_ports.comports()
            if p.device.upper() == self.port.upper()
            and p.vid == 0x2341 and p.pid in (0x005E, 0x805E, 0x015E, 0x025E)
            and (p.serial_number or "").upper() == TARGET_SERIAL
        ]
        if len(matches) != 1:
            raise RuntimeError("The specified port is not the uniquely verified Nano board")
        self.serial = serial.Serial(matches[0].device, 115200, timeout=0.1, write_timeout=2.5)
        self.capture("initial_boot", 0.5)
        initial = self.status("initial_status")
        self.summary["initial_status"] = initial
        self.summary["port"] = self.serial.port
        if initial.get("queued") != 0 or initial.get("state") != "IDLE":
            raise RuntimeError("Board is not a fresh idle run; existing queue is preserved")
        self.setting("boot_id", self.summary["boot_id"])
        self.setting("transport", "serial_bridge")
        self.command("q", "initial_quiet", "telemetry")
        configured = self.status("configured_status")
        self.summary["configured_status"] = configured
        if configured.get("transport") != "serial_bridge" or not configured.get("boot_id_set"):
            raise RuntimeError("Board did not enter the fresh serial_bridge configuration")

    def connect_mqtt(self):
        settings = load_settings(RUNTIME)
        if settings["mqtt"]["host"] != "localhost" or settings["mqtt"]["port"] != 18885:
            raise RuntimeError("Expected the existing loopback TLS service on port 18885")
        self.client = make_mqtt_client(settings, "badge-a")

        def on_connect(client, userdata, flags, reason, properties):
            ok = reason == 0
            self.trace("connect", connected=ok, tls=True, host="localhost", port=18885)
            if ok:
                self.connected.set()
                client.subscribe("bump/v1/device/badge-a/ack", qos=1)

        def on_subscribe(client, userdata, mid, reasons, properties):
            qos = [reason.value for reason in reasons]
            self.trace("subscribe", granted_qos=qos)
            if qos == [1]:
                self.subscribed.set()

        def on_message(client, userdata, message):
            try:
                ack = json.loads(message.payload)
                valid = (
                    message.topic == "bump/v1/device/badge-a/ack" and not message.retain
                    and isinstance(ack, dict) and set(ack) == ACK_FIELDS
                    and type(ack["v"]) is int and ack["v"] == 1
                    and ack["device_id"] == "badge-a"
                    and ack["boot_id"] == self.summary["boot_id"]
                    and type(ack["seq"]) is int and ack["seq"] > 0
                    and ack["status"] in ("stored", "duplicate")
                )
            except (ValueError, UnicodeError):
                valid = False
            if valid:
                self.trace("application_ack", payload=ack, retain=False)
                self.acks.put(ack)
            else:
                self.trace("ignored_ack", payload_bytes=len(message.payload), retain=message.retain)

        self.client.on_connect = on_connect
        self.client.on_subscribe = on_subscribe
        self.client.on_message = on_message
        self.client.connect("localhost", 18885, 15)
        self.client.loop_start()
        if not self.connected.wait(3) or not self.subscribed.wait(3):
            raise TimeoutError("Authenticated TLS MQTT connection or SUBACK was not observed")
        self.summary["mqtt_connected"] = True
        self.summary["mqtt_suback_qos"] = 1

    def database_rows(self):
        path = RUNTIME / "telemetry.sqlite3"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
            rows = db.execute(
                "SELECT canonical_json FROM telemetry WHERE device_id=? AND boot_id=? ORDER BY seq",
                ("badge-a", self.summary["boot_id"]),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def check_event(self, event, seq, kind, source):
        validate_motion(event, "badge-a")
        expected = {
            "boot_id": self.summary["boot_id"], "seq": seq, "event": kind,
            "source": source, "transport": "serial_bridge",
        }
        if any(event.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Unexpected queued event identity, source or transport")
        return event

    def dump(self, label):
        data = self.command("dump", label, "queue_dump_complete")
        events = [item["payload"] for item in data if item.get("type") == "queued_event"]
        if data[-1].get("queued") != len(events):
            raise RuntimeError("Incomplete queue dump; queue is preserved")
        return events

    def publish(self, event, expected_status):
        payload = json.dumps(event, separators=(",", ":"), allow_nan=False)
        self.trace("publish", payload=event, qos=1, retain=False, transport="serial_bridge")
        result = self.client.publish("bump/v1/device/badge-a/events", payload, qos=1, retain=False)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("MQTT publish was not accepted locally")
        result.wait_for_publish(timeout=3)
        if not result.is_published():
            raise TimeoutError("MQTT PUBACK was not observed; board queue is preserved")
        self.trace("puback", seq=event["seq"])
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                ack = self.acks.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if ack["seq"] == event["seq"] and ack["status"] == expected_status:
                rows = self.database_rows()  # Fresh read-only connection, after the application ACK.
                matching = [row for row in rows if row["seq"] == event["seq"]]
                if matching != [event]:
                    raise RuntimeError("Stored payload differs or is missing; board queue is preserved")
                self.trace("database_verified", seq=event["seq"], boot_rows=len(rows), exact_payload=True)
                return ack
        raise TimeoutError("Matching storage ACK was not observed; board queue is preserved")

    def forward_ack(self, ack, expected_remaining):
        data = self.command(
            "ACK " + json.dumps(ack, separators=(",", ":")),
            "forward_ack_" + str(ack["seq"]), "queue_removed_after_application_ack",
        )
        matched = any(
            item.get("type") == "application_ack"
            and item.get("ack_origin") == "serial_bridge" and item.get("matched") is True
            for item in data
        )
        removed = data[-1]
        if not matched or removed.get("seq") != ack["seq"] or removed.get("queued") != expected_remaining:
            raise RuntimeError("Board did not confirm the expected queue removal")

    def execute(self):
        self.stage("Checking the isolated TLS service and fresh board")
        self.connect_mqtt()
        self.connect_serial()
        if self.database_rows():
            raise RuntimeError("Fresh UUID unexpectedly already exists in the database")
        data = self.command("sample", "real_sensor_sample", "event_queued")
        first = self.check_event(data[-1]["payload"], 1, "imu_sample", "hardware")
        self.summary["real_sensor_event"] = first
        self.stage("Opening the discovery window with explicitly injected motion")
        data = self.command("inject_shake", "injected_motion", "injection_complete")
        events = [item["payload"] for item in data if item.get("type") == "event_queued"]
        if len(events) != 1:
            raise RuntimeError("Expected exactly one injected event")
        second = self.check_event(events[0], 2, "motion_trigger", "injected_test")
        self.summary["injected_event"] = second
        start = self.status("discovery_start")
        self.summary["discovery_start"] = start
        if start.get("state") != "DISCOVERY" or start.get("window_remaining_ms", 0) <= 0:
            raise RuntimeError("Injected motion did not open the discovery window")
        self.command("i", "natural_window_telemetry", "telemetry")
        self.stage("Waiting for natural 60-second expiry; no early sync command")
        natural = self.capture("natural_expiry", 65, until="sync_complete")
        self.command("q", "after_expiry_quiet", "telemetry")
        self.summary["natural_expiry"] = [
            item for item in natural if item.get("type") in (
                "discovery_complete", "sync_started", "serial_bridge_waiting", "sync_complete", "error",
            )
        ]
        if not any(item.get("type") == "discovery_complete" for item in natural):
            raise RuntimeError("Natural discovery completion was not observed")
        if not any(
            item.get("type") == "serial_bridge_waiting" and item.get("wifi_attempted") is False
            for item in natural
        ):
            raise RuntimeError("Expected serial bridge waiting state without a Wi-Fi attempt")
        if natural[-1].get("queued") != 2 or natural[-1].get("delivered") != 0:
            raise RuntimeError("Unexpected queue state at natural expiry")
        frozen = self.dump("immutable_queue_dump")
        self.summary["dumped_events"] = frozen
        if frozen != [first, second]:
            raise RuntimeError("Dumped payloads changed or unexpected events were queued")
        self.stage("Checking wrong-boot ACK rejection before any queue release")
        wrong = dict(v=1, device_id="badge-a", boot_id=str(uuid.uuid4()), seq=1, status="stored")
        rejected = self.command("ACK " + json.dumps(wrong), "wrong_boot_ack", "application_ack")[-1]
        after_wrong = self.dump("queue_after_wrong_ack")
        if rejected.get("matched") is not False or after_wrong != frozen:
            raise RuntimeError("Wrong-boot ACK was not safely rejected")
        self.summary["wrong_ack_rejected_queue_unchanged"] = True
        self.stage("Uploading immutable events through PC TLS MQTT and checking durable storage")
        first_ack = self.publish(first, "stored")
        before_duplicate = self.database_rows()
        duplicate_ack = self.publish(first, "duplicate")
        after_duplicate = self.database_rows()
        if before_duplicate != after_duplicate or len(after_duplicate) != 1:
            raise RuntimeError("Duplicate upload changed the database")
        self.summary["duplicate_check"] = {
            "ack": duplicate_ack, "rows_before": len(before_duplicate),
            "rows_after": len(after_duplicate), "unchanged": True,
        }
        self.forward_ack(first_ack, 1)
        second_ack = self.publish(second, "stored")
        self.forward_ack(second_ack, 0)
        self.summary["storage_acks"] = [first_ack, second_ack]
        self.summary["stored_rows"] = self.database_rows()
        self.summary["final_queue"] = self.dump("final_queue")
        self.summary["final_status"] = self.status("final_status")
        self.summary["payloads_stored_exactly"] = self.summary["stored_rows"] == frozen
        self.summary["success"] = (
            self.summary["payloads_stored_exactly"] and not self.summary["final_queue"]
            and self.summary["final_status"].get("queued") == 0
        )
        if not self.summary["success"]:
            raise RuntimeError("Final storage/queue verification did not pass")

    def run(self):
        try:
            self.execute()
        except Exception as exc:
            self.summary["success"] = False
            self.summary["error"] = str(exc)
            self.summary["failure_policy"] = "No cleanup reset, queue clear or unverified success ACK; remaining entries retained"
            self.summary["observed_queued_events"] = [
                item["data"]["payload"] for item in self.records
                if item["data"].get("type") == "event_queued"
            ]
        finally:
            self.summary["finished_utc"] = utc_now()
            self.save()
            if self.serial is not None:
                self.serial.close()
            if self.client is not None:
                self.client.disconnect()
                self.client.loop_stop()
        print(json.dumps(self.summary), flush=True)
        return 0 if self.summary.get("success") else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM12")
    args = parser.parse_args()
    return BridgeRun(args.port).run()


if __name__ == "__main__":
    raise SystemExit(main())
