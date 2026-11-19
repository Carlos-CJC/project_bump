"""Drive existing Bump serial sessions and audit independent hardware receive rounds."""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4


BOARDS = ("A", "B")
DEVICE = re.compile(r"bump-[0-9a-f]{12}")
SHORT_ID = re.compile(r"[0-9a-f]{12}")
BOOT_ID = re.compile(r"[0-9a-f]{8}")
EVENT_ID = re.compile(r"ev_[0-9a-f]{12}_[0-9a-f]{8}_[0-9a-f]{8}")
ALLOWED_COMMANDS = {"discover", "status", "sync"}


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def send(inbox, command, side="both"):
    if command not in ALLOWED_COMMANDS or side not in (*BOARDS, "both"):
        raise ValueError("unsupported command or target")
    stage = inbox / f"{time.time_ns()}-{uuid4().hex[:7]}.part"
    stage.write_text(json.dumps({"action": "send", "side": side, "command": command}),
                     encoding="utf-8")
    os.replace(stage, stage.with_suffix(".json"))
    return stage.with_suffix(".done")


class SerialLog:
    def __init__(self, path):
        self.path = path
        self.offset = path.stat().st_size

    def read(self):
        with self.path.open("rb") as file:
            if os.fstat(file.fileno()).st_size < self.offset:
                raise RuntimeError("serial log was replaced during the run")
            file.seek(self.offset)
            while line := file.readline():
                if not line.endswith(b"\n"):
                    break
                self.offset = file.tell()
                row = json.loads(line)
                if row["board"] not in BOARDS:
                    continue
                if row["kind"] == "tx":
                    if row["data"] not in ALLOWED_COMMANDS:
                        continue
                elif row["kind"] != "rx":
                    continue
                if row["kind"] == "rx":
                    try:
                        row["data"] = json.loads(row["data"])
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(row["data"], dict):
                        continue
                yield row


def stored_events(db, ids):
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    marks = ",".join("?" for _ in wanted)
    try:
        rows = db.execute(f"""SELECT event_id,device,boot,peer_short,peer_device,received_at,
                                     rssi_avg,samples,duration_s,transport
                              FROM resolved WHERE event_id IN ({marks})""", wanted)
        return {row["event_id"]: dict(row) for row in rows}
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        return {}


def board_identity(status, expected):
    return (status.get("firmware") == "BumpDualBadge" and
            (expected is None or status.get("fw") == expected) and
            DEVICE.fullmatch(status.get("device_id", "")) and
            BOOT_ID.fullmatch(status.get("boot_id", "")) and
            SHORT_ID.fullmatch(status.get("self_short_id", "")) and
            status.get("imu_ready") is True)


def restore_network(inbox, board, paths):
    helper = Path(__file__).resolve().parents[2] / "ops" / "board_session.py"
    hotspot, runtime = paths
    subprocess.run([sys.executable, "-B", str(helper), "configure", str(inbox.parent), board,
                    "--hotspot", str(hotspot), "--runtime", str(runtime)], check=True)


def wait_ready(session, inbox, args):
    deadline = time.monotonic() + args.ready_timeout
    next_status = 0
    statuses = {}
    sync_requested = set()
    configured_boots = set()
    while time.monotonic() < deadline:
        for row in session.read():
            if row["kind"] != "rx" or row["data"].get("type") != "status":
                continue
            board, data = row["board"], row["data"]
            statuses[board] = data
            identity = (board, data.get("boot_id"))
            if (args.restore_network_from and board_identity(data, args.expect_fw) and
                    data.get("network_configured") is False and data.get("discovery") is False and
                    identity not in configured_boots):
                restore_network(inbox, board, args.restore_network_from)
                configured_boots.add(identity)
                print(f"{board} {identity[1]}: local settings requested between trials", flush=True)
            if (board not in sync_requested and data.get("queued", 0) > 0 and
                    data.get("network_configured") is True and not data.get("discovery")):
                send(inbox, "sync", board)
                sync_requested.add(board)
        if len(statuses) == 2 and all(
            board_identity(s, args.expect_fw) and
            s.get("transport") == "wifi_mqtt_tcp" and s.get("network_configured") is True and
            s.get("discovery") is False and s.get("paused") is False and s.get("queued") == 0
            for s in statuses.values()
        ):
            for key in ("device_id", "self_short_id"):
                if statuses["A"][key] == statuses["B"][key]:
                    raise RuntimeError(f"boards have identical {key}")
            return {board: {field: statuses[board][field] for field in
                    ("fw", "device_id", "boot_id", "self_short_id")} for board in BOARDS}
        if time.monotonic() >= next_status:
            send(inbox, "status")
            next_status = time.monotonic() + 8
        time.sleep(0.15)
    details = {board: {field: row.get(field) for field in
               ("fw", "paused", "discovery", "queued", "network_configured", "transport")}
               for board, row in statuses.items()}
    raise RuntimeError(f"both boards did not become idle, empty and connected in time: {details}")


def direction(other_identity):
    return {
        "expected_peer_short_id": other_identity["self_short_id"],
        "input_tx_at": None, "start": None, "end": None, "samples_preview": [],
        "queued_observations": [], "transmits": [], "broker_puback": [],
        "application_ack": [], "db_rows": [], "resets": [], "reasons": [],
    }


def single_encounter(data):
    payload = data.get("payload")
    if not isinstance(payload, dict) or len(payload.get("events", [])) != 1:
        return None
    event = payload["events"][0]
    if not isinstance(event, dict) or not EVENT_ID.fullmatch(event.get("event_id", "")):
        return None
    return payload, event


class Trial:
    def __init__(self, number, identity, source="serial_diagnostic"):
        self.number = number
        self.identity = identity
        self.source = source
        self.sides = {"A": direction(identity["B"]), "B": direction(identity["A"])}
        self._awaiting_puback = {}

    def apply(self, row):
        board = row["board"]
        data = row["data"]
        side = self.sides[board]
        when = row["utc"]
        if self.source == "serial_diagnostic" and row["kind"] == "tx" and data == "discover":
            side["input_tx_at"] = when
        if row["kind"] != "rx":
            return
        if self.source == "hardware":
            if (side["start"] is None and data.get("type") == "motion_trigger" and
                    data.get("detail") == "hardware"):
                side["motion_trigger"] = {"at": when, **data}
            if not side.get("motion_trigger"):
                return
        elif side["input_tx_at"] is None:
            return
        kind = data.get("type")
        if kind == "watchdog_recovery" or (
            kind == "status" and data.get("boot_id") and
            data["boot_id"] != self.identity[board]["boot_id"]
        ):
            if not side["resets"]:
                side["reasons"].append("board_reset_during_round")
            side["resets"].append({"at": when, "stage":
                data.get("stage") or data.get("last_watchdog_stage"),
                "pre_reset_ram_queued": data.get("pre_reset_ram_queued")})
            return
        if kind == "discovery_start" and data.get("source") == self.source:
            if side["start"] is None:
                side["start"] = {"at": when, **data}
            else:
                side["reasons"].append("more_than_one_discovery_window")
        if side["start"] is None:
            return
        if kind == "discovery_end" and data.get("source") == self.source:
            side["end"] = {"at": when, **data}
        elif kind == "ble_observation" and side["end"] is None:
            side["samples_preview"].append({"at": when, **data})
        elif kind == "wire" and data.get("topic") == (
            f"bump/v1/device/{self.identity[board]['device_id']}/encounters"
        ):
            result = single_encounter(data)
            if result is None:
                return
            payload, event = result
            key = event["event_id"]
            if (data.get("phase") == "queued" and data.get("source") == self.source and
                    side["end"] is None):
                side["queued_observations"].append(
                    {"at": when, **event, "device_id": payload.get("device_id"),
                     "boot_id": payload.get("boot_id"), "self_short_id": payload.get("self_short_id")})
            elif data.get("phase") == "transmit" and key in self.event_ids(board):
                side["transmits"].append({"event_id": key, "at": when})
                self._awaiting_puback[board] = key
        elif kind == "mqtt_puback" and board in self._awaiting_puback:
            key = self._awaiting_puback.pop(board)
            side["broker_puback"].append(
                {"event_id": key, "at": when, "received": data.get("detail") == "received"})
        elif kind == "queue_removed_after_application_ack" and data.get("origin") == "wifi_mqtt":
            key = data.get("event_id")
            if key in self.event_ids(board):
                side["application_ack"].append({"event_id": key, "at": when})

    def event_ids(self, board=None):
        sides = [self.sides[board]] if board else self.sides.values()
        return {event["event_id"] for side in sides for event in side["queued_observations"]}

    def peer_candidates(self, board):
        side = self.sides[board]
        return [event for event in side["queued_observations"]
                if event["peer_short_id"] == side["expected_peer_short_id"]]

    def delivery_complete(self, stored):
        return all(key in stored and
                   any(item["event_id"] == key and item["received"]
                       for item in side["broker_puback"]) and
                   any(item["event_id"] == key for item in side["application_ack"])
                   for board, side in self.sides.items() for key in self.event_ids(board))

    def report(self, stored, seen, max_skew, timed_out):
        anchors = [self.sides[board]["start"] for board in BOARDS]
        skew = None
        if all(anchors):
            skew = abs((datetime.fromisoformat(anchors[0]["at"]) -
                        datetime.fromisoformat(anchors[1]["at"])).total_seconds())
        for board, side in self.sides.items():
            identity = self.identity[board]
            other = self.identity["B" if board == "A" else "A"]
            reasons = side["reasons"]
            side["db_rows"] = [stored[key] for key in sorted(self.event_ids(board)) if key in stored]
            if not side["start"] or side["start"]["window_ms"] != 60000:
                reasons.append("missing_60_second_discovery_start")
            elif side["start"].get("self_short_id") != identity["self_short_id"]:
                reasons.append("board_identity_changed")
            if not side["end"] or side["end"]["complete"] is not True:
                reasons.append("discovery_incomplete")
            elif side["start"] and side["end"]["uptime_ms"] - side["start"]["uptime_ms"] < 60000:
                reasons.append("discovery_duration_under_60_seconds")
            candidates = self.peer_candidates(board)
            if len(candidates) != 1:
                reasons.append("expected_exactly_one_peer_encounter")
                continue
            event = candidates[0]
            key = event["event_id"]
            if key in seen:
                reasons.append("event_id_already_seen")
            if any(event.get(name) != identity[name]
                   for name in ("device_id", "boot_id", "self_short_id")):
                reasons.append("event_identity_mismatch")
            if (event["samples"] < 2 or event["duration_s"] < 3 or
                    event["rssi_avg"] < -70 or event["rssi_last"] < -70):
                reasons.append("encounter_below_original_gate")
            if not any(row["event_id"] == key and row["received"] for row in side["broker_puback"]):
                reasons.append("broker_puback_not_observed")
            if not any(row["event_id"] == key for row in side["application_ack"]):
                reasons.append("matching_application_ack_not_observed")
            saved = stored.get(key)
            if not saved:
                reasons.append("encounter_not_in_database")
            elif (saved["device"] != identity["device_id"] or saved["boot"] != identity["boot_id"] or
                  saved["peer_device"] != other["device_id"] or
                  saved["peer_short"] != other["self_short_id"] or saved["transport"] != "mqtt"):
                reasons.append("database_direction_or_transport_mismatch")
        both_observed = all(len(self.peer_candidates(board)) == 1 for board in BOARDS)
        both_saw_peer = all(any(sample["peer_short_id"] == side["expected_peer_short_id"]
                                for sample in side["samples_preview"])
                            for side in self.sides.values())
        passed = (skew is not None and skew <= max_skew and not timed_out and
                  all(not side["reasons"] for side in self.sides.values()))
        if len(self.event_ids()) != sum(len(self.event_ids(board)) for board in BOARDS):
            passed = False
        return {"record": "round", "round": self.number, "source": self.source,
                "start_skew_s": skew, "max_start_skew_s": max_skew, "timeout": timed_out,
                "both_saw_peer": both_saw_peer, "both_observed": both_observed,
                "passed": passed, "directions": self.sides}


def run_round(number, session, db, inbox, identity, args, seen):
    trial = Trial(number, identity, args.source)
    receipts = []
    now = time.monotonic()
    delayed_at = None
    if args.source == "serial_diagnostic":
        if args.b_leads_by:
            receipts.append(send(inbox, "discover", "B"))
            delayed_at = now + args.b_leads_by
        else:
            receipts.append(send(inbox, "discover"))
    start_deadline = now + args.start_timeout + args.b_leads_by
    window_deadline = None
    ack_deadline = None
    stored = {}
    timeout = None
    next_database_check = 0
    while True:
        for row in session.read():
            trial.apply(row)
        now = time.monotonic()
        if delayed_at is not None and now >= delayed_at:
            receipts.append(send(inbox, "discover", "A"))
            delayed_at = None
        starts = all(side["start"] for side in trial.sides.values())
        ends = all(side["end"] for side in trial.sides.values())
        if starts and window_deadline is None:
            window_deadline = now + args.window_timeout
        if not starts and now >= start_deadline:
            timeout = "discovery_start"
            break
        if not ends and window_deadline is not None and now >= window_deadline:
            timeout = "discovery_end"
            break
        if ends and ack_deadline is None:
            ack_deadline = now + args.ack_timeout
        if ends and (now >= next_database_check or now >= ack_deadline):
            stored = stored_events(db, trial.event_ids())
            next_database_check = now + 0.5
            if trial.delivery_complete(stored):
                break
            if now >= ack_deadline:
                timeout = "database_or_application_ack"
                break
        time.sleep(0.1)
    if any(not path.exists() for path in receipts):
        timeout = "command_receipt"
    stored.update(stored_events(db, trial.event_ids()))
    report = trial.report(stored, seen, args.max_start_skew + args.b_leads_by, timeout)
    if args.b_leads_by:
        report["requested_b_lead_s"] = args.b_leads_by
        starts = [trial.sides[b]["start"] for b in BOARDS]
        if all(starts):
            delta = (datetime.fromisoformat(starts[0]["at"]) -
                     datetime.fromisoformat(starts[1]["at"])).total_seconds()
            report["actual_b_lead_s"] = delta
            if abs(delta - args.b_leads_by) > args.max_start_skew:
                report["passed"] = False
    return report


def write_record(report, value):
    report.write(json.dumps(value, ensure_ascii=False) + "\n")
    report.flush()


def run(args):
    args.session = args.session.resolve(strict=True)
    database = args.db.resolve(strict=True)
    inbox = (args.session / "commands").resolve(strict=True)
    if inbox.parent != args.session or not inbox.is_dir():
        raise RuntimeError("session commands must be an existing directory under --session")
    session = SerialLog(args.session / "serial.jsonl")
    successes = attempts = observed = 0
    stop_reason = "completed"
    with args.output.open("x", encoding="utf-8") as output, closing(
            sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.5)) as db:
        db.row_factory = sqlite3.Row
        seen = {row[0] for row in db.execute("SELECT event_id FROM resolved")}
        write_record(output, {"record": "start", "at": utc(), "requested_rounds": args.rounds,
                              "source": args.source, "preexisting_event_count": len(seen)})
        try:
            for number in range(1, args.rounds + 1):
                identity = wait_ready(session, inbox, args)
                write_record(output, {"record": "round_start", "at": utc(),
                                      "round": number, "identity": identity})
                result = run_round(number, session, db, inbox, identity, args, seen)
                attempts += 1
                successes += int(result["passed"])
                observed += int(result["both_observed"])
                for side in result["directions"].values():
                    seen.update(event["event_id"] for event in side["queued_observations"])
                write_record(output, result)
                print(f"round {number}: {'PASS' if result['passed'] else 'FAIL'}; "
                      f"both observed {result['both_observed']}; timeout {result['timeout']}",
                      flush=True)
                if result["timeout"] == "command_receipt":
                    stop_reason = "unconsumed_discover_command"
                    break
                if number == 2 and successes == 0 and not args.complete_series:
                    stop_reason = "no_verified_dual_direction_in_first_two_rounds"
                    break
        except (RuntimeError, ValueError, OSError, sqlite3.Error, KeyboardInterrupt) as exc:
            stop_reason = f"{type(exc).__name__}: {exc}"
            print(stop_reason, flush=True)
        write_record(output, {"record": "summary", "at": utc(), "attempted": attempts,
                              "verified_dual_direction": successes, "both_observed": observed,
                              "requested": args.rounds, "stop_reason": stop_reason})
    print(f"{successes}/{attempts} rounds verified ({args.rounds} requested). {stop_reason}",
          flush=True)
    return 0 if attempts == successes == args.rounds else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True, help="running board_session directory")
    parser.add_argument("--db", type=Path, required=True, help="existing live SQLite file")
    parser.add_argument("--output", type=Path, required=True, help="new JSONL report")
    parser.add_argument("--expect-fw", default="0.5.1-dual")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--complete-series", action="store_true",
                        help="retain failures and continue through the planned number of rounds")
    parser.add_argument("--restore-network-from", nargs=2, type=Path, metavar=("HOTSPOT", "RUNTIME"),
                        help="restore local RAM settings on identified idle boards between rounds")
    parser.add_argument("--source", choices=("serial_diagnostic", "hardware"),
                        default="serial_diagnostic", help="hardware waits for physical IMU gestures")
    parser.add_argument("--b-leads-by", type=float, default=0, metavar="SECONDS",
                        help="open B first, then A after a measured offset during a radio test")
    parser.add_argument("--ready-timeout", type=float, default=100)
    parser.add_argument("--start-timeout", type=float, default=12)
    parser.add_argument("--window-timeout", type=float, default=82)
    parser.add_argument("--ack-timeout", type=float, default=65)
    parser.add_argument("--max-start-skew", type=float, default=2)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 100:
        parser.error("--rounds must be 1 to 100")
    if not 0 <= args.b_leads_by < 60 or (args.b_leads_by and args.source != "serial_diagnostic"):
        parser.error("--b-leads-by requires diagnostic windows and must be below 60 seconds")
    if not 65 <= args.window_timeout <= 150:
        parser.error("--window-timeout must be 65 to 150 seconds")
    if not 0 < args.ready_timeout <= 900:
        parser.error("--ready-timeout must be above 0 and at most 900 seconds")
    for option in ("start_timeout", "ack_timeout", "max_start_skew"):
        if not 0 < getattr(args, option) <= 180:
            parser.error(f"--{option.replace('_', '-')} must be above 0 and at most 180")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
