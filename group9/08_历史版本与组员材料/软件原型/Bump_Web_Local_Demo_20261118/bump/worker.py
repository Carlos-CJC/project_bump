"""MQTT ingestion: application ACKs follow durable Store.ingest_event()."""

from __future__ import annotations

import json
import threading

import paho.mqtt.client as mqtt

from .device import make_mqtt_client
from .store import Store


class Worker:
    def __init__(self, settings, store=None, drop_ack_count=0, fail_store_count=0):
        if any(type(n) is not int or n < 0 for n in (drop_ack_count, fail_store_count)):
            raise ValueError("Fault counts must be non-negative integers.")
        self.settings = settings
        self.store = Store(settings) if store is None else store
        self.ready = threading.Event()
        self.last_error = None
        self.stats = {
            "received": 0, "stored": 0, "duplicates": 0, "rejected": 0,
            "store_failures": 0, "acks_dropped": 0, "acks_published": 0,
        }
        self._drop_ack_remaining = drop_ack_count
        self._fail_store_remaining = fail_store_count
        self._started = False
        self._subscription_mid = None
        self._client = make_mqtt_client(settings, "_worker")
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    @property
    def health(self):
        return self.ready.is_set()

    def start(self, timeout=5.0):
        if not self._started:
            connection = self.settings["mqtt"]
            self._client.connect_async(connection["host"], connection["port"], 15)
            self._client.loop_start()
            self._started = True
        return self.ready.wait(max(0, timeout))

    def stop(self):
        self.ready.clear()
        if self._started:
            self._client.disconnect()
            self._client.loop_stop()
            self._started = False

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        self.ready.clear()
        if reason_code != 0:
            self.last_error = "MQTT worker connection refused."
            return
        result, mid = client.subscribe("bump/v1/device/+/events", qos=1)
        self._subscription_mid = mid if result == mqtt.MQTT_ERR_SUCCESS else None

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if mid == self._subscription_mid and reason_codes and all(
            code == 1 for code in reason_codes
        ):
            self.ready.set()
            self.last_error = None
        else:
            self.last_error = "Event subscription was not granted at QoS 1."

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.ready.clear()

    def _on_message(self, client, userdata, message):
        parts = message.topic.split("/")
        if (
            len(parts) != 5 or parts[:3] != ["bump", "v1", "device"]
            or parts[4] != "events" or parts[3] not in self.settings["devices"]
            or message.retain
        ):
            return
        self.stats["received"] += 1
        try:
            event = json.loads(message.payload)
        except (ValueError, UnicodeDecodeError):
            self.stats["rejected"] += 1
            return
        if self._fail_store_remaining:
            self._fail_store_remaining -= 1
            self.stats["store_failures"] += 1
            self.last_error = "Injected test-only storage failure."
            return
        try:
            ack = self.store.ingest_event(event, topic_device_id=parts[3])
        except ValueError:
            self.stats["rejected"] += 1
            # Return a rejection only when the original complete key is usable.
            if (
                not isinstance(event, dict) or event.get("device_id") != parts[3]
                or not isinstance(event.get("boot_id"), str)
                or type(event.get("seq")) is not int
            ):
                return
            ack = {
                "schema_version": 1, "device_id": parts[3],
                "boot_id": event["boot_id"], "seq": event["seq"],
                "status": "rejected", "reason": "invalid_or_conflicting_event",
            }
        except Exception as error:
            # Database outages must never turn into a success acknowledgement.
            self.stats["store_failures"] += 1
            self.last_error = f"Storage unavailable ({type(error).__name__})."
            return
        else:
            counter = "stored" if ack["status"] == "stored" else "duplicates"
            self.stats[counter] += 1
            self.last_error = None
            if self._drop_ack_remaining:
                self._drop_ack_remaining -= 1
                self.stats["acks_dropped"] += 1
                return
        info = client.publish(
            f"bump/v1/device/{parts[3]}/ack",
            json.dumps(ack, separators=(",", ":")), qos=1, retain=False,
        )
        if info.rc == mqtt.MQTT_ERR_SUCCESS:
            self.stats["acks_published"] += 1
