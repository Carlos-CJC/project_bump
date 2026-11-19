"""Short, reproducible scenarios over real TLS MQTT, with synthetic sensor input."""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from .device import DeviceSimulator


class DemoBusy(RuntimeError):
    """Another local caller already holds the fixed simulator identities."""


@contextmanager
def _exclusive_run(settings):
    # A short-lived SQLite write reservation in a *separate* file coordinates
    # web tabs, web processes and the legacy CLI without blocking the event DB.
    # OS locks are released if a process exits; the empty lock file may remain.
    lock_db = Path(settings["db_path"]).with_suffix(".demo-lock.sqlite3")
    with closing(sqlite3.connect(lock_db, timeout=0)) as lease:
        try:
            lease.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise DemoBusy("another local discovery simulation is running") from None
            raise
        try:
            yield
        finally:
            lease.rollback()


def run_demo(settings, scenario="normal"):
    """Use the running local broker/worker and serialize the two demo identities."""
    with _exclusive_run(settings):
        return _run_demo(settings, scenario)


def _run_demo(settings, scenario):
    if scenario not in {"normal", "offline", "retry"}:
        raise ValueError("Scenario must be normal, offline or retry.")
    devices = [
        DeviceSimulator(settings, "badge-a", drop_ack_count=int(scenario == "retry")),
        DeviceSimulator(settings, "badge-b"),
    ]
    events = []
    offline_pending = None
    connected_before_outage = None
    utc_start = datetime.now(timezone.utc) - timedelta(seconds=6)
    try:
        if scenario == "offline":
            connected_before_outage = [device.connect(timeout=5.0) for device in devices]
            for device in devices:
                device.disconnect()
        for device, peer in zip(devices, ["badge-b", "badge-a"]):
            device.shake(now=0)
            event = device.observe(
                settings["devices"][peer]["alias"], start_utc=utc_start
            )
            events.append(event)
            device.tick(now=60)  # Virtual time, not a measured 60-second hardware trial.
        if scenario == "offline":
            # Explicit transport outage; virtual observations remain in RAM.
            offline_pending = [device.queue_size for device in devices]
            for device in devices:
                device.pause()
        completed = [device.sync(timeout=8.0) for device in devices]
        return {
            "scenario": scenario,
            "simulation_mode": True,
            "source": "simulated",
            "transport": "real MQTT over TLS on loopback",
            "clock": "60-second discovery window advanced by scenario driver",
            "ok": all(completed) and (
                connected_before_outage is None or all(connected_before_outage)
            ),
            "offline_pending": offline_pending,
            "connected_before_outage": connected_before_outage,
            "devices": [
                {
                    "device_id": device.device_id,
                    "event_key": [event["device_id"], event["boot_id"], event["seq"]],
                    "state": device.state,
                    "queue_size": device.queue_size,
                    "publish_count": device.publish_count,
                    "acks": device.ack_history,
                    "dropped_ack_count": device.dropped_ack_count,
                    "overflow_count": device.overflow_count,
                }
                for device, event in zip(devices, events)
            ],
            "hardware_verified": False,
        }
    finally:
        for device in devices:
            device.close()
