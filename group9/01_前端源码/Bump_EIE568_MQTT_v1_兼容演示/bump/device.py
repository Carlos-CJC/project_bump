"""Finite, in-memory badge model. Inputs here are simulated, never IMU/BLE data."""

from __future__ import annotations

import copy
import json
import ssl
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt


def make_mqtt_client(settings: dict, account: str) -> mqtt.Client:
    """Use the generated CA and the account's fixed MQTT identity."""
    connection = settings["mqtt"]
    if connection["host"] not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("This starter supports loopback MQTT only.")
    credentials = connection["accounts"][account]
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=credentials["client_id"],
        clean_session=True,
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(account, credentials["password"])
    context = ssl.create_default_context(cafile=connection["ca_file"])
    client.tls_set_context(context)  # CERT_REQUIRED and hostname checking stay enabled.
    client.connect_timeout = 2.0
    client.reconnect_delay_set(min_delay=1, max_delay=4)
    client.max_queued_messages_set(128)
    return client


class DeviceSimulator:
    """The scenario driver calls tick() and sync(); no hardware is accessed."""

    WINDOW_SECONDS = 60.0

    def __init__(self, settings, device_id, capacity=128, drop_ack_count=0):
        if device_id not in settings["devices"]:
            raise ValueError("Unknown device.")
        if type(capacity) is not int or capacity < 1:
            raise ValueError("Queue capacity must be a positive integer.")
        if type(drop_ack_count) is not int or drop_ack_count < 0:
            raise ValueError("drop_ack_count must be a non-negative integer.")
        self.settings = settings
        self.device_id = device_id
        self.capacity = capacity
        self.boot_id = str(uuid.uuid4())
        self.seq = 0
        self.state = "IDLE"
        self.overflow_count = 0
        self.publish_count = 0
        self.ignored_ack_count = 0
        self.dropped_ack_count = 0
        self.ack_history = []
        self.last_error = None
        self._drop_ack_remaining = drop_ack_count
        self._queue = OrderedDict()
        self._lock = threading.RLock()
        self._transport_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._changed = threading.Event()
        self.ready = threading.Event()
        self._window_deadline = None
        self._subscription_mid = None
        self._loop_started = False
        self._closed = False
        self._client = make_mqtt_client(settings, device_id)
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    @property
    def queue(self):
        with self._lock:
            return copy.deepcopy(list(self._queue.values()))

    @property
    def queue_size(self):
        with self._lock:
            return len(self._queue)

    @property
    def health(self):
        return self.ready.is_set()

    @property
    def events_topic(self):
        return f"bump/v1/device/{self.device_id}/events"

    @property
    def ack_topic(self):
        return f"bump/v1/device/{self.device_id}/ack"

    def connect(self, timeout=5.0):
        if self._closed:
            raise RuntimeError("Device simulator is closed.")
        with self._transport_lock:
            if not self._loop_started:
                connection = self.settings["mqtt"]
                self._client.connect_async(connection["host"], connection["port"], 15)
                self._client.loop_start()
                self._loop_started = True
        return self.ready.wait(max(0, timeout))

    def disconnect(self):
        """Disconnect the transport without discarding the RAM queue."""
        self.ready.clear()
        with self._transport_lock:
            if self._loop_started:
                self._client.disconnect()
                self._client.loop_stop()
                self._loop_started = False
        self._changed.set()

    def close(self):
        self._closed = True
        self.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.ready.clear()
        if reason_code != 0:
            self.last_error = "MQTT connection refused."
            return
        result, mid = client.subscribe(self.ack_topic, qos=1)
        self._subscription_mid = mid if result == mqtt.MQTT_ERR_SUCCESS else None

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if mid == self._subscription_mid and reason_codes and all(
            code == 1 for code in reason_codes
        ):
            self.ready.set()
            self.last_error = None
        else:
            self.last_error = "ACK subscription was not granted at QoS 1."
        self._changed.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.ready.clear()
        self._changed.set()

    def _on_message(self, client, userdata, message):
        if message.topic != self.ack_topic or message.retain:
            return
        try:
            self.handle_ack(json.loads(message.payload))
        except (ValueError, TypeError, UnicodeDecodeError):
            self.ignored_ack_count += 1

    def handle_ack(self, ack):
        """Only a complete, correctly typed success key can clear an event."""
        if not isinstance(ack, dict):
            self.ignored_ack_count += 1
            return False
        if (
            type(ack.get("schema_version")) is not int
            or ack["schema_version"] != 1
            or ack.get("device_id") != self.device_id
            or not isinstance(ack.get("boot_id"), str)
            or type(ack.get("seq")) is not int
            or ack["seq"] < 1
            or not isinstance(ack.get("status"), str)
            or ack.get("status") not in {"stored", "duplicate"}
        ):
            self.ignored_ack_count += 1
            return False
        key = (ack["device_id"], ack["boot_id"], ack["seq"])
        with self._lock:
            if key not in self._queue:
                self.ignored_ack_count += 1
                return False
            if self._drop_ack_remaining:
                self._drop_ack_remaining -= 1
                self.dropped_ack_count += 1
                return False
            del self._queue[key]
            self.ack_history.append(copy.deepcopy(ack))
            self._changed.set()
            return True

    def enqueue(self, event):
        event = copy.deepcopy(event)
        if (
            event.get("device_id") != self.device_id
            or event.get("source") != "simulated"
            or not isinstance(event.get("boot_id"), str)
            or not event["boot_id"]
            or type(event.get("seq")) is not int
            or event["seq"] < 1
        ):
            raise ValueError("A simulator event needs its device ID, key and source=simulated.")
        key = (event["device_id"], event["boot_id"], event["seq"])
        with self._lock:
            if key in self._queue:
                if self._queue[key] != event:
                    raise ValueError("An enqueued event cannot change its content.")
                return True
            if len(self._queue) >= self.capacity:
                self.overflow_count += 1
                return False
            self._queue[key] = event
        return True

    def shake(self, now=None):
        """Inject one already-recognized simulated gesture."""
        with self._lock:
            if self.state != "IDLE":
                return False
            self.state = "DISCOVERY"
            self._window_deadline = (
                time.monotonic() if now is None else now
            ) + self.WINDOW_SECONDS
            return True

    def pause(self):
        with self._lock:
            self.state = "PAUSED"
            self._window_deadline = None
        self._changed.set()

    def resume(self):
        with self._lock:
            if self.state == "PAUSED":
                self.state = "IDLE"

    def tick(self, now=None):
        with self._lock:
            instant = time.monotonic() if now is None else now
            if self.state == "DISCOVERY" and instant >= self._window_deadline:
                self.state = "SYNC" if self._queue else "IDLE"
                self._window_deadline = None
            return self.state

    def observe(
        self, peer_alias, *, session_id="demo-001", start_ms=1000, end_ms=7000,
        start_utc=None, rssi_median=-55, sample_count=12, time_quality="synced",
    ):
        """Inject an explicitly synthetic aggregate; this performs no BLE sampling."""
        with self._lock:
            if self.state != "DISCOVERY":
                return None
            if time_quality not in {"synced", "unknown"}:
                raise ValueError("Invalid time quality.")
            if time_quality == "synced":
                start = start_utc or datetime.now(timezone.utc)
                if isinstance(start, str):
                    start = datetime.fromisoformat(start.replace("Z", "+00:00"))
                if start.tzinfo is None:
                    raise ValueError("UTC start must be timezone-aware.")
                start_text = start.astimezone(timezone.utc).isoformat()
                end_text = (start + timedelta(milliseconds=end_ms - start_ms)).astimezone(
                    timezone.utc
                ).isoformat()
            else:
                start_text = end_text = None
            self.seq += 1
            event = {
                "schema_version": 1, "device_id": self.device_id,
                "boot_id": self.boot_id, "seq": self.seq,
                "session_id": session_id, "peer_alias": peer_alias,
                "start_ms": start_ms, "end_ms": end_ms,
                "start_utc": start_text, "end_utc": end_text,
                "time_quality": time_quality, "rssi_median": rssi_median,
                "sample_count": sample_count, "source": "simulated",
            }
            return event if self.enqueue(event) else None

    def sync(self, timeout=5.0):
        """Try the backlog within a deadline, including while manually paused."""
        deadline = time.monotonic() + max(0, timeout)
        if not self._sync_lock.acquire(blocking=False):
            return False
        try:
            with self._lock:
                if self.state == "DISCOVERY" or self._closed:
                    return False
                if self.state != "PAUSED":
                    self.state = "SYNC"
            delay = 0.2
            while self.queue_size and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if not self.connect(timeout=min(remaining, 0.5)):
                    continue
                with self._lock:
                    if not self._queue:
                        break
                    event = copy.deepcopy(next(iter(self._queue.values())))
                self._changed.clear()
                info = self._client.publish(
                    self.events_topic, json.dumps(event, separators=(",", ":")),
                    qos=1, retain=False,
                )
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    self.publish_count += 1
                else:
                    self.last_error = "MQTT publish is waiting for reconnection."
                self._changed.wait(max(0, min(delay, deadline - time.monotonic())))
                delay = min(delay * 2, 1.0)
            return self.queue_size == 0
        finally:
            with self._lock:
                if self.state == "SYNC":
                    self.state = "IDLE"
            self._sync_lock.release()
