"""Firmware-v1 uplink on the private loopback broker, separate from legacy /events.

The subscriber is read-only. A QoS 1 PUBACK is not an application ACK; the
software demo confirms its own two IDs by reading the committed local store.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import secrets
import threading
import time

import paho.mqtt.client as mqtt

from .config import V1_DEMO_DEVICES, V1_MQTT_DEVICES, V1_TOPIC_FILTERS, V1_WORKER_ACCOUNT
from .device import make_mqtt_client
from .simulator import DemoBusy, _exclusive_run
from .v1_wire import parse_message


class TransportUnavailable(RuntimeError):
    """The local path could not confirm the simulation's committed events."""


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise TransportUnavailable("Local v1 confirmation timed out.")
    return left


@contextmanager
def _publisher(settings, device, deadline):
    """One short-lived TLS client using this provisioned device's fixed identity."""
    client = make_mqtt_client(settings, device)
    completed = threading.Event()
    accepted = False

    def on_connect(client, userdata, flags, reason_code, properties):
        nonlocal accepted
        accepted = reason_code == 0
        completed.set()

    client.on_connect = on_connect
    started = False
    try:
        client.connect_timeout = min(2.0, _remaining(deadline))
        client.connect("127.0.0.1", settings["mqtt"]["port"], keepalive=60)
        if client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
            raise TransportUnavailable("Local v1 publisher could not start.")
        started = True
        if not completed.wait(_remaining(deadline)) or not accepted:
            raise TransportUnavailable("Local v1 publisher could not connect.")
        yield client
    finally:
        try:
            client.disconnect()
        finally:
            if started:
                client.loop_stop()


def _publish(client, device, kind, body, deadline):
    packet = client.publish(f"bump/v1/device/{device}/{kind}",
                            json.dumps(body, separators=(",", ":")).encode("utf-8"),
                            qos=1, retain=(kind == "status"))
    if packet.rc != mqtt.MQTT_ERR_SUCCESS:
        raise TransportUnavailable("Local v1 publish failed.")
    packet.wait_for_publish(timeout=_remaining(deadline))
    if not packet.is_published():
        raise TransportUnavailable("Local v1 broker did not confirm receipt.")
    # Intentionally no application success return here.


def _software_packets(device, self_short, peer_short, boot):
    """Two-sided synthetic uptime-clock observations; no sensor, board or NTP."""
    mac = device.removeprefix("bump-")
    seq = "00000001"
    base = {"schema": 1, "device_id": device, "boot_id": boot, "fw": "0.4.0-sim"}
    status = {**base, "uptime_s": 287, "events_dropped": 0}
    event = {
        "event_id": f"ev_{mac}_{boot}_{seq}", "peer_short_id": peer_short,
        "rssi_avg": -65, "rssi_last": -63, "duration_s": 7, "samples": 2,
        "flags": 1, "peer_tx_power": -4, "occurred_uptime_ms": 284000,
    }
    batch = {**base, "batch_id": f"bt_{mac}_{boot}_{seq}_0001",
             "self_short_id": self_short, "uptime_ms": 287431,
             "time_source": "uptime", "events": [event]}
    return ({**status, "state": "online", "queue_depth": 1}, batch,
            {**status, "state": "offline", "queue_depth": 0})


class V1Transport:
    """A clean-session, read-only QoS 1 subscriber and two local mock senders.

    An old runtime with no v1 accounts stays untouched: start() returns False.
    No persistent MQTT backend session or LWT is enabled by this local build.
    """

    _DEMO_TIMEOUT = 8.0

    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        accounts = settings["mqtt"]["accounts"]
        self.enabled = all(user in accounts for user in (*V1_MQTT_DEVICES, V1_WORKER_ACCOUNT))
        self._client = None
        self._lifecycle = threading.Lock()
        self._ready = threading.Event()
        self._notice = threading.Condition()
        self._last_message_at = None
        self._live_status = {}  # Only the two provisioned synthetic devices.
        self._subscription_mid = None

    def health(self):
        with self._notice:
            return {"connected": self._ready.is_set(), "last_message_at": self._last_message_at}

    def start(self, timeout=8.0):
        if not self.enabled:
            return False
        with self._lifecycle:
            if self._client is None:
                client = make_mqtt_client(self.settings, V1_WORKER_ACCOUNT)
                client.on_connect = self._on_connect
                client.on_subscribe = self._on_subscribe
                client.on_disconnect = self._on_disconnect
                client.on_message = self._on_message
                client.connect_async("127.0.0.1", self.settings["mqtt"]["port"], keepalive=60)
                if client.loop_start() != mqtt.MQTT_ERR_SUCCESS:
                    return False
                self._client = client
        return self._ready.wait(max(0, timeout))

    def stop(self):
        with self._lifecycle:
            self._ready.clear()
            client, self._client = self._client, None
            if client is not None:
                try:
                    client.disconnect()
                finally:
                    client.loop_stop()
            self._ready.clear()
        with self._notice:
            self._notice.notify_all()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self._ready.clear()
        self._subscription_mid = None
        if reason_code == 0:
            rc, mid = client.subscribe([(topic, 1) for topic in V1_TOPIC_FILTERS])
            if rc == mqtt.MQTT_ERR_SUCCESS:
                self._subscription_mid = mid

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if (mid == self._subscription_mid and len(reason_codes) == len(V1_TOPIC_FILTERS)
                and all(code == 1 for code in reason_codes)):
            self._ready.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._ready.clear()
        with self._notice:
            self._notice.notify_all()

    def _on_message(self, client, userdata, message):
        if message.qos != 1:
            return
        stamp = time.time()
        try:
            # A subscriber does not receive the publisher's identity. The
            # broker enforces credentials + fixed client ID + exact publish ACL.
            parsed = parse_message(message.topic, message.payload, observed_at=stamp,
                                   retained=bool(message.retain))
            if parsed["device_id"] not in V1_MQTT_DEVICES:
                return
            self.store.ingest(parsed, received_at=stamp)
        except Exception:
            # Neither a malformed batch nor a failed transaction is a
            # successful storage observation. Never echo payloads or secrets.
            return
        with self._notice:
            self._last_message_at = stamp
            if parsed["kind"] == "status" and not parsed["retained"]:
                body = parsed["body"]
                self._live_status[parsed["device_id"]] = (body["boot_id"], body["state"])
            self._notice.notify_all()

    def run_demo(self, user_id):
        if user_id not in V1_DEMO_DEVICES:
            raise PermissionError("Not a participant in the local v1 simulation.")
        if not self._ready.is_set():
            raise TransportUnavailable("Local v1 subscriber is not ready.")
        try:
            # Same cross-process SQLite lease as the legacy web/CLI simulation.
            # Concurrent callers cannot take over the fixed MQTT client IDs.
            with _exclusive_run(self.settings):
                return self._run_locked()
        except (DemoBusy, TransportUnavailable):
            raise
        except Exception:
            raise TransportUnavailable("Local v1 simulation could not be confirmed.") from None

    def _run_locked(self):
        deadline = time.monotonic() + self._DEMO_TIMEOUT
        devices = V1_MQTT_DEVICES
        # Short IDs are per boot, not permanent device nicknames. Reusing one
        # across software runs would correctly make the resolver distrust it.
        short_ids = {device: secrets.token_hex(6) for device in devices}
        if len(set(short_ids.values())) != len(devices):
            raise TransportUnavailable("Local v1 simulated short identities collided.")
        packets, boots, ids = {}, {}, []
        for device, peer in zip(devices, reversed(devices)):
            # A rare random 32-bit boot collision must not let an old row
            # satisfy this request's durable-storage check.
            for _ in range(8):
                boot = secrets.token_hex(4)
                candidate = _software_packets(device, short_ids[device], short_ids[peer], boot)
                event_id = candidate[1]["events"][0]["event_id"]
                if not self.store.contains_event_ids([event_id]):
                    packets[device], boots[device] = candidate, boot
                    ids.append(event_id)
                    break
            else:
                raise TransportUnavailable("No fresh local v1 event identity available.")
        with ExitStack() as stack:
            clients = {d: stack.enter_context(_publisher(self.settings, d, deadline)) for d in devices}
            for index, kind in enumerate(("status", "encounters", "status")):
                for d in devices:
                    _publish(clients[d], d, kind, packets[d][index], deadline)
            while True:
                with self._notice:
                    offline = all(self._live_status.get(d) == (boots[d], "offline") for d in devices)
                if offline and self.store.contains_event_ids(ids):
                    return {"persisted": True, "event_count": 2}
                if not self._ready.is_set():
                    raise TransportUnavailable("Local v1 subscriber disconnected.")
                with self._notice:
                    self._notice.wait(min(0.05, _remaining(deadline)))
