"""Owner-scoped browser control for the fixed two-simulator rehearsal.

This module never reads a serial port, discovers BLE peers, or takes an event,
a path, a device or a scenario from an HTTP request.
"""

import sqlite3

from .simulator import DemoBusy, run_demo
from .store import AccessDeniedError, StateConflictError


# Only these two supplied, fictional participants can run the paired rehearsal.
_PARTICIPANTS = {"alice": "badge-a", "bob": "badge-b"}


class DiscoveryUnavailable(RuntimeError):
    pass


def _matched_ack(device):
    """The matching application ACK, not a broker-level publish/PUBACK."""
    key = tuple(device.get("event_key") or ())
    if len(key) != 3 or key[0] != device.get("device_id") or device.get("queue_size") != 0:
        return None
    for ack in device.get("acks", ()):
        if ((ack.get("device_id"), ack.get("boot_id"), ack.get("seq")) == key
                and ack.get("status") in {"stored", "duplicate"}):
            return key, ack["status"]
    return None


class Discovery:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store

    def _own_device(self, user_id):
        device_id = _PARTICIPANTS.get(user_id)
        device = self.settings["devices"].get(device_id, {})
        return device_id if device.get("owner") == user_id else None

    def _pair_configured(self):
        aliases = self.settings["sessions"].get("demo-001", {})
        devices = self.settings["devices"]
        return all(self._own_device(user) == device_id and
                   aliases.get(devices[device_id].get("alias")) == device_id
                   for user, device_id in _PARTICIPANTS.items())

    def status(self, user_id):
        own_id = self._own_device(user_id)
        device = None if not own_id else {
            "alias": self.settings["devices"][own_id]["alias"],
            **self.store.last_device_receipt(user_id, own_id),
        }
        return {"mode": "simulation", "available": bool(own_id and self._pair_configured()),
                "hardware_telemetry": "not_integrated", "device": device}

    def run(self, user_id):
        own_id = self._own_device(user_id)
        if not own_id or not self._pair_configured():
            raise AccessDeniedError("this account cannot run the fixed discovery simulation")
        try:
            result = run_demo(self.settings, "normal")
        except DemoBusy as exc:
            raise StateConflictError(str(exc)) from None
        except (OSError, RuntimeError, sqlite3.Error, ValueError, KeyError) as exc:
            raise DiscoveryUnavailable() from exc

        if (result.get("ok") is not True or result.get("source") != "simulated" or
                result.get("simulation_mode") is not True or result.get("hardware_verified") is not False):
            raise DiscoveryUnavailable()
        devices = result.get("devices", [])
        confirmed = {device["device_id"]: _matched_ack(device) for device in devices}
        if (len(devices) != 2 or set(confirmed) != set(_PARTICIPANTS.values())
                or not all(confirmed.values())):
            raise DiscoveryUnavailable()
        key, ack = confirmed[own_id]
        encounter = self.store.own_simulated_receipt(user_id, key)
        if not encounter:
            raise DiscoveryUnavailable()
        # No device IDs, boot IDs, other participant's record, profile or diagnostics.
        return {"mode": "simulation", "simulated": True, "source": "simulated",
                "created": ack == "stored", "ack_status": ack,
                "encounter_id": encounter["id"], "peer_alias": encounter["peer_alias"]}
