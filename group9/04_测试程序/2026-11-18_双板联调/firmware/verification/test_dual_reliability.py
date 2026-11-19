"""Offline checks. Uses synthetic logs and a temporary SQLite file."""

from argparse import Namespace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

import run_dual_reliability as driver


IDENTITY = {
    "A": {"device_id": "bump-112233445566", "boot_id": "a1b2c3d4",
          "self_short_id": "abbccddeeff0", "fw": "0.5.1-dual"},
    "B": {"device_id": "bump-aabbccddeeff", "boot_id": "b1c2d3e4",
          "self_short_id": "123456abcdef", "fw": "0.5.1-dual"},
}
EPOCH = datetime(2026, 11, 18, 15, tzinfo=timezone.utc)


def row(board, stamp, data, kind="rx"):
    return {"board": board, "utc": (EPOCH + timedelta(seconds=stamp)).isoformat(),
            "data": data, "kind": kind}


def fixture(*, acknowledged=True, skew=0.05, repeat_id=False, source="serial_diagnostic",
            physical_input=True):
    trial = driver.Trial(1, IDENTITY, source)
    db_rows = {}
    seen = set()
    for board, other, offset in (("A", "B", 0), ("B", "A", skew)):
        identity = IDENTITY[board]
        peer = IDENTITY[other]
        key = f"ev_{identity['device_id'][5:]}_{identity['boot_id']}_00000001"
        payload = {**identity, "events": [
            {"event_id": key, "peer_short_id": peer["self_short_id"], "rssi_avg": -63,
             "rssi_last": -65, "samples": 8, "duration_s": 6, "occurred_uptime_ms": 21000}]}
        common_wire = {"type": "wire", "source": source, "payload": payload,
                       "topic": f"bump/v1/device/{identity['device_id']}/encounters"}
        serial = [
            row(board, offset + 0.2, {"type": "discovery_start", "source": source,
                                     "uptime_ms": 11000, "window_ms": 60000,
                                     "self_short_id": identity["self_short_id"]}),
            row(board, offset + 2, {"type": "ble_observation",
                                   "peer_short_id": peer["self_short_id"], "samples": 1}),
            row(board, offset + 60.2, {**common_wire, "phase": "queued"}),
            row(board, offset + 61, {"type": "discovery_end", "source": source,
                                    "uptime_ms": 71200, "complete": True}),
            row(board, offset + 63, {**common_wire, "phase": "transmit"}),
            row(board, offset + 64, {"type": "mqtt_puback", "detail": "received"}),
        ]
        if source == "hardware" and physical_input:
            serial.insert(0, row(board, offset, {"type": "motion_trigger", "detail": "hardware"}))
        else:
            serial.insert(0, row(board, offset, "discover", "tx"))
        if acknowledged or board == "B":
            serial.append(row(board, offset + 65, {
                "type": "queue_removed_after_application_ack", "origin": "wifi_mqtt",
                "event_id": key}))
        for item in serial:
            trial.apply(item)
        db_rows[key] = {"event_id": key, "device": identity["device_id"], "boot": identity["boot_id"],
                        "peer_short": peer["self_short_id"], "peer_device": peer["device_id"],
                        "transport": "mqtt"}
        if repeat_id and board == "A":
            seen.add(key)
    return trial, db_rows, seen


class Reliability(TestCase):
    def test_buffers_positive_ack_before_database_poll(self):
        trial, db_rows, seen = fixture()
        self.assertTrue(trial.delivery_complete(db_rows))
        result = trial.report(db_rows, seen, 2, None)
        self.assertTrue(result["passed"])
        self.assertTrue(result["both_saw_peer"])
        self.assertEqual(len(trial.event_ids()), 2)

    def test_missing_ack_rejects_even_when_both_rows_stored(self):
        trial, db_rows, seen = fixture(acknowledged=False)
        self.assertFalse(trial.delivery_complete(db_rows))
        result = trial.report(db_rows, seen, 2, "database_or_application_ack")
        self.assertTrue(result["both_observed"])
        self.assertFalse(result["passed"])
        self.assertIn("matching_application_ack_not_observed",
                      result["directions"]["A"]["reasons"])

    def test_reused_id_rejects_replayed_round(self):
        trial, db_rows, seen = fixture(repeat_id=True)
        result = trial.report(db_rows, seen, 2, None)
        self.assertIn("event_id_already_seen", result["directions"]["A"]["reasons"])
        self.assertFalse(result["passed"])

    def test_distinguishes_simultaneous_rounds_from_time_shifted_windows(self):
        trial, db_rows, seen = fixture(skew=6)
        result = trial.report(db_rows, seen, 2, None)
        self.assertEqual(result["start_skew_s"], 6.0)
        self.assertFalse(result["passed"])

    def test_rejects_middle_offload_and_wrong_peer(self):
        trial, db_rows, seen = fixture()
        next(iter(db_rows.values()))["transport"] = "usb_serial"
        result = trial.report(db_rows, seen, 2, None)
        self.assertFalse(result["passed"])
        self.assertIn("database_direction_or_transport_mismatch",
                      result["directions"]["A"]["reasons"])

    def test_does_not_pass_a_round_with_an_observed_reset(self):
        trial, db_rows, seen = fixture()
        trial.apply(row("A", 64.5, {"type": "watchdog_recovery",
                                    "stage": "ble_poll", "pre_reset_ram_queued": 1}))
        result = trial.report(db_rows, seen, 2, None)
        self.assertFalse(result["passed"])
        self.assertEqual(result["directions"]["A"]["resets"][0]["stage"], "ble_poll")

    def test_bounded_stop_after_first_two_failures(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as volume:
            temp = Path(volume)
            (temp / "commands").mkdir()
            (temp / "serial.jsonl").touch()
            db = temp / "live.sqlite3"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE resolved (event_id TEXT)")
            conn.close()
            output = temp / "output.jsonl"
            args = Namespace(session=temp, db=db, output=output, rounds=20, source="serial_diagnostic",
                             complete_series=False)
            attempts = [
                {"record": "round", "round": number, "passed": False, "both_observed": False,
                 "timeout": None, "directions": {"A": {"queued_observations": []}}}
                for number in (1, 2)]
            with patch.object(driver, "wait_ready", return_value=IDENTITY), \
                 patch.object(driver, "run_round", side_effect=attempts) as run_round:
                self.assertEqual(driver.run(args), 1)
                self.assertEqual(run_round.call_count, 2)
            summary = json.loads(output.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(summary["verified_dual_direction"], 0)
            self.assertEqual(summary["attempted"], 2)
            self.assertFalse(list((temp / "commands").iterdir()))

    def test_command_allowlist_without_accessing_a_live_session(self):
        with TemporaryDirectory(dir=Path(__file__).resolve().parent) as volume:
            temp = Path(volume)
            with self.assertRaises(ValueError):
                driver.send(temp, "SET transport serial_bridge")
            result = driver.send(temp, "discover")
            packet = json.loads(result.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(packet, {"action": "send", "side": "both", "command": "discover"})

    def test_physical_trial_requires_motion_on_each_device(self):
        trial, rows, seen = fixture(source="hardware")
        report = trial.report(rows, seen, 2, None)
        self.assertTrue(report["passed"])
        self.assertEqual(report["source"], "hardware")
        for side in report["directions"].values():
            self.assertIsNone(side["input_tx_at"])
            self.assertEqual(side["motion_trigger"]["detail"], "hardware")
        trial, rows, seen = fixture(source="hardware", physical_input=False)
        self.assertFalse(trial.report(rows, seen, 2, None)["passed"])


if __name__ == "__main__":
    main()
