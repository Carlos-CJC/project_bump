"""Capture both identified USB boards with one owner and timestamp each observation."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid

RUN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUN.parent / "2026-11-15_单板调试与进度报告" / "tools" / "pydeps"))
import serial
from serial.tools.list_ports import comports

USB = {"A": "0617355018835E82", "B": "416523501C5F96C8"}
ACCOUNTS = {"A": "alice", "B": "bob"}


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def record(log, side, kind, data):
    row = {"utc": utc(), "board": side, "kind": kind, "data": data}
    log.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.flush()


def configure(side, request):
    ap = read_json(Path(request["hotspot"]) / "session.json")
    if not ap["ready"]:
        raise ValueError("hotspot unavailable")
    conf = read_json(Path(request["runtime"]) / "live-config.json")
    device_id = conf["bindings"][ACCOUNTS[side]]["device_id"]
    return [
        "SET ssid " + ap["ssid"],
        "SET wifi_password " + ap["password"],
        "SET broker_host " + request["host"],
        "SET broker_port " + str(request["port"]),
        "SET mqtt_password " + conf["mqtt"]["accounts"][device_id]["password"],
    ]


class BoardLink:
    def __init__(self, side, log, secrets):
        self.side, self.log, self.secrets = side, log, secrets
        self.port = None
        self.opened_before = False
        self.waiting_status = False
        self.retry_at = 0.0
        self.status_at = 0.0
        self.pending = bytearray()

    def log_event(self, kind, data):
        record(self.log, self.side, kind, data)

    def close(self):
        if self.port is not None:
            try:
                self.port.close()
            except (serial.SerialException, OSError):
                pass
        self.port = None
        self.pending.clear()

    def lost(self, exc):
        self.log_event("lost", {"port": self.port.port, "reason": str(exc)})
        self.close()
        self.retry_at = time.monotonic() + 0.4

    def connect(self):
        now = time.monotonic()
        if self.port is not None or now < self.retry_at:
            return
        self.retry_at = now + 0.6
        try:
            matches = [p.device for p in comports() if p.serial_number == USB[self.side]
                       and (p.vid, p.pid) == (0x2341, 0x005E)]
            if len(matches) != 1:
                return
            self.port = serial.Serial(matches[0], 115200, timeout=0, write_timeout=0.6)
        except (serial.SerialException, OSError):
            return
        self.log_event("reopen" if self.opened_before else "open", self.port.port)
        self.opened_before = True
        self.waiting_status = True
        self.status_at = now + 0.2

    def transmit(self, text, label, request_id):
        if self.port is None:
            self.log_event("tx_skipped", {"command": label, "request": request_id, "reason": "offline"})
            return False
        payload = (text + "\n").encode("ascii")
        try:
            if self.port.write(payload) != len(payload):
                raise serial.SerialTimeoutException("partial serial write")
        except (serial.SerialException, OSError) as exc:
            self.log_event("tx_uncertain", {"command": label, "request": request_id})
            self.lost(exc)
            return False
        self.log_event("tx", label)
        return True

    def poll(self):
        self.connect()
        if self.port is None:
            return
        try:
            count = self.port.in_waiting
            if count:
                self.pending.extend(self.port.read(count))
        except (serial.SerialException, OSError) as exc:
            self.lost(exc)
            return
        while b"\n" in self.pending:
            raw, _, self.pending = self.pending.partition(b"\n")
            line = raw.rstrip(b"\r").decode("utf-8", "replace")
            for value in self.secrets:
                line = line.replace(value, "[private]")
            self.log_event("rx", line)
            if self.waiting_status and line.startswith("{"):
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict) and data.get("type") == "status":
                    self.log_event("first_status", data)
                    self.waiting_status = False
        if self.waiting_status and time.monotonic() >= self.status_at:
            self.status_at = time.monotonic() + 1
            self.transmit("status", "status", "automatic_status")


class CommandFiles:
    """Retry the FIFO head before dispatch and retry result markers without dispatching again."""

    def __init__(self, inbox, log):
        self.inbox = inbox
        self.log = log
        self.head_blocked = None
        self.head_retry_at = 0.0
        self.pending = {}
        self.blocked_markers = set()
        self.marker_retry_at = 0.0

    def take(self):
        if time.monotonic() < self.head_retry_at:
            return None
        paths = sorted(self.inbox.glob("*.json"))
        if not paths:
            return None
        path = paths[0]
        try:
            request = read_json(path)
            claimed = path.with_suffix(".working")
            path.rename(claimed)
        except PermissionError:
            if self.head_blocked != path:
                record(self.log, "both", "command_claim_deferred", path.name)
            self.head_blocked = path
            self.head_retry_at = time.monotonic() + 0.2
            return None
        self.head_blocked = None
        return claimed, request

    def finish(self, claimed, success):
        target = claimed.with_suffix(".done" if success else ".failed")
        # Log the terminal outcome before touching the marker. A surviving .working file
        # is never dispatched, even if the collector exits before its rename succeeds.
        record(self.log, "both", "command_result", {"request": claimed.stem, "marker": target.suffix})
        self.pending[claimed] = target

    def flush_markers(self):
        if not self.pending or time.monotonic() < self.marker_retry_at:
            return
        for claimed, target in list(self.pending.items()):
            try:
                claimed.rename(target)
            except PermissionError:
                if claimed not in self.blocked_markers:
                    record(self.log, "both", "command_marker_deferred", claimed.name)
                    self.blocked_markers.add(claimed)
                continue
            self.blocked_markers.discard(claimed)
            del self.pending[claimed]
        self.marker_retry_at = time.monotonic() + 0.2 if self.pending else 0.0


def listen(folder):
    folder.mkdir(parents=True, exist_ok=False)
    inbox = folder / "commands"
    inbox.mkdir()
    with ExitStack() as stack:
        log = stack.enter_context((folder / "serial.jsonl").open("x", encoding="utf-8"))
        secrets = set()
        boards = {side: BoardLink(side, log, secrets) for side in USB}
        command_files = CommandFiles(inbox, log)
        stopping = None
        for board in boards.values():
            stack.callback(board.close)
        while True:
            for board in boards.values():
                board.poll()
            command_files.flush_markers()
            if stopping is not None:
                request_id, deadline = stopping
                if not command_files.pending or time.monotonic() >= deadline:
                    if command_files.pending:
                        record(log, "both", "command_markers_pending_at_stop",
                               [p.name for p in command_files.pending])
                    record(log, "both", "stop", request_id)
                    return
            else:
                item = command_files.take()
                if item is not None:
                    claimed, request = item
                    if request["action"] == "stop":
                        command_files.finish(claimed, True)
                        stopping = claimed.stem, time.monotonic() + 2.0
                    else:
                        sides = USB if request["side"] == "both" else [request["side"]]
                        complete = True
                        for side in sides:
                            if request["action"] == "configure":
                                commands = configure(side, request)
                                for command in commands:
                                    if "_password " in command or command.startswith("SET ssid "):
                                        secrets.add(command.split(" ", 2)[2])
                            else:
                                commands = [request["command"]]
                            for command in commands:
                                label = command.rsplit(" ", 1)[0] + " [private]" if request["action"] == "configure" else command
                                if not boards[side].transmit(command, label, claimed.stem):
                                    complete = False
                                    break
                                time.sleep(0.05)
                        command_files.finish(claimed, complete)
            time.sleep(0.025)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["listen", "send", "configure", "stop"])
    parser.add_argument("folder", type=Path)
    parser.add_argument("side", nargs="?", choices=["A", "B", "both"], default="both")
    parser.add_argument("command", nargs="?")
    parser.add_argument("--hotspot")
    parser.add_argument("--runtime")
    parser.add_argument("--host", default="192.168.137.1")
    parser.add_argument("--port", type=int, default=18883)
    args = parser.parse_args()
    if args.action == "listen":
        return listen(args.folder)
    if args.action == "send" and (not args.command or "password" in args.command.lower()):
        parser.error("Use configure for credentials; send needs a non-secret command.")
    if args.action == "configure" and not (args.hotspot and args.runtime):
        parser.error("configure requires --hotspot and --runtime.")
    packet = vars(args).copy()
    packet.pop("folder")
    name = f"{time.time_ns()}-{uuid.uuid4().hex[:7]}"
    stage = args.folder / "commands" / (name + ".part")
    stage.write_text(json.dumps(packet), encoding="utf-8")
    os.replace(stage, stage.with_suffix(".json"))


if __name__ == "__main__":
    main()


