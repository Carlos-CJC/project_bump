"""Pure, bounded decoder for the Bump firmware MQTT v1 uplink.

The v1 wire format is distinct from the original Bump_Starter simulator format.
No device is provisioned here. The subscriber relies on the broker's per-device
publish ACL; the topic and JSON alone do not authenticate a physical badge.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re


MAX_PAYLOAD_BYTES = 2048
MAX_EVENTS = 8
_U32 = (1 << 32) - 1
_DB_INT_MIN, _DB_INT_MAX = -(1 << 63), (1 << 63) - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# Range representable by a four-digit Gregorian year and UTC date in SQLite/API.
_MIN_EPOCH, _MAX_EPOCH = -62135596800, 253402300800

_H12 = r"[0-9a-f]{12}"
_H8 = r"[0-9a-f]{8}"
_TOPIC = re.compile(rf"bump/v1/device/(bump-{_H12})/(encounters|status)")
_BOOT, _SHORT = re.compile(_H8), re.compile(_H12)
_EVENT_ID = re.compile(rf"ev_({_H12})_({_H8})_({_H8})")
# The document names a decimal count; its examples zero-pad it to four digits.
_BATCH_ID = re.compile(rf"bt_({_H12})_({_H8})_({_H8})_([0-9]{{1,4}})")
_UTC = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|\+00:00)"
)
_BASE = frozenset(("schema", "device_id", "boot_id", "fw"))
_BATCH = _BASE | {"batch_id", "self_short_id", "uptime_ms", "time_source", "events"}
_EVENT = frozenset((
    "event_id", "peer_short_id", "rssi_avg", "rssi_last", "duration_s",
    "samples", "flags", "occurred_uptime_ms",
))
_STATUS = _BASE | {"state", "uptime_s", "queue_depth", "events_dropped"}


class WireError(ValueError):
    """Invalid v1 delivery. Error text never echoes untrusted payload data."""


def _shape(obj, required, optional=frozenset()):
    if type(obj) is not dict or required - obj.keys() or obj.keys() - required - optional:
        raise WireError("missing or unsupported fields for v1 message")


def _integer(value, field, lower, upper):
    # bool is a Python int subclass, but true/false are not JSON integers.
    if type(value) is not int or not lower <= value <= upper:
        raise WireError(f"invalid integer for {field}")
    return value


def _match(value, pattern, field):
    match = pattern.fullmatch(value) if type(value) is str else None
    if match is None:
        raise WireError(f"invalid {field}")
    return match


def _no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise WireError("duplicate JSON member")
        obj[key] = value
    return obj


def _no_float(_value):
    # v1 defines no floating-point fields. NaN and infinity are not JSON.
    raise WireError("only finite JSON integers are supported")


def _epoch(value):
    if type(value) not in (int, float) or not _MIN_EPOCH <= value < _MAX_EPOCH:
        raise WireError("invalid trusted observation timestamp")
    return float(value)


def _parse_utc(value):
    if type(value) is not str or len(value) > 64 or _UTC.fullmatch(value) is None:
        raise WireError("occurred_at must be an RFC3339 UTC timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        raise WireError("invalid UTC date or time") from None


def event_interval(
    body: dict, event: dict, observed_at: int | float
) -> tuple[float, float]:
    """Return (started_at, ended_at) in UTC epoch seconds for a decoded event.

    The encounter decision is the end, not the start, of the dwell interval.
    A device with NTP may omit occurred_at. In that case use the envelope's
    monotonic clock, just as for an uptime-only device.
    """
    observed = _epoch(observed_at)
    duration = _integer(event.get("duration_s"), "duration_s", 0, 86400)
    src = body.get("time_source")
    if src not in ("uptime", "ntp"):
        raise WireError("invalid time_source")
    if "occurred_at" in event:
        if src != "ntp":
            raise WireError("occurred_at is permitted only with NTP")
        end = (_parse_utc(event["occurred_at"]) - _EPOCH).total_seconds()
        if end > observed + 600:
            raise WireError("NTP timestamp is more than ten minutes ahead")
    else:
        sent = _integer(body.get("uptime_ms"), "uptime_ms", 0, _U32)
        seen = _integer(event.get("occurred_uptime_ms"), "occurred_uptime_ms", 0, _U32)
        age_ms = sent - seen
        # Within the same boot a near-maximum seen count followed by a small
        # send count crosses millis() rollover. A small negative difference
        # instead follows the documented +5 minute future-skew clamp.
        if age_ms < -(1 << 31):
            age_ms += 1 << 32
        age_s = max(-300.0, min(86400.0, age_ms / 1000.0))
        end = observed - age_s
    start = end - duration
    if not _MIN_EPOCH <= start <= end < _MAX_EPOCH or not math.isfinite(end):
        raise WireError("event interval is outside the supported UTC date range")
    return start, end


def _validate_base(body, device_id):
    _integer(body["schema"], "schema", 1, 1)
    if body["device_id"] != device_id:
        raise WireError("body device_id does not match topic")
    _match(body["boot_id"], _BOOT, "boot_id")
    fw = body["fw"]
    if type(fw) is not str or not 1 <= len(fw) <= 32 or not fw.isprintable() or not fw.strip():
        raise WireError("invalid firmware version")


def _validate_status(body):
    if body["state"] not in ("online", "offline"):
        raise WireError("invalid status state")
    _integer(body["uptime_s"], "uptime_s", 0, _U32)
    _integer(body["queue_depth"], "queue_depth", 0, 255)
    _integer(body["events_dropped"], "events_dropped", 0, _U32)
    for field in ("wifi_rssi", "battery_mv"):
        if field in body:
            # The firmware draft gives no calibrated sensor bounds for these
            # optional integers. Do not invent one; do require SQLite range.
            _integer(body[field], field, _DB_INT_MIN, _DB_INT_MAX)


def _validate_batch(body, device_id, observed):
    _match(body["self_short_id"], _SHORT, "self_short_id")
    _integer(body["uptime_ms"], "uptime_ms", 0, _U32)
    if body["time_source"] not in ("uptime", "ntp"):
        raise WireError("invalid time_source")
    events = body["events"]
    if type(events) is not list or not 1 <= len(events) <= MAX_EVENTS:
        raise WireError("batch must contain 1 to 8 events")
    device_hex, boot = device_id[5:], body["boot_id"]
    batch_dev, batch_boot, first_seq, count = _match(
        body["batch_id"], _BATCH_ID, "batch_id"
    ).groups()
    if (batch_dev, batch_boot) != (device_hex, boot) or int(count) != len(events):
        raise WireError("batch_id does not identify its device, boot and count")
    ids = set()
    for index, ev in enumerate(events):
        _shape(ev, _EVENT, {"peer_tx_power", "occurred_at"})
        dev, ev_boot, seq = _match(ev["event_id"], _EVENT_ID, "event_id").groups()
        if (dev, ev_boot) != (device_hex, boot) or (index == 0 and seq != first_seq):
            raise WireError("event_id is inconsistent with batch identity")
        if seq in ids:
            raise WireError("repeated event_id within one batch")
        ids.add(seq)
        _match(ev["peer_short_id"], _SHORT, "peer_short_id")
        for field in ("rssi_avg", "rssi_last"):
            _integer(ev[field], field, -127, 0)
        _integer(ev["samples"], "samples", 1, 65535)
        _integer(ev["flags"], "flags", 0, 255)
        _integer(ev["occurred_uptime_ms"], "occurred_uptime_ms", 0, _U32)
        if "peer_tx_power" in ev:
            _integer(ev["peer_tx_power"], "peer_tx_power", -128, 127)
        # Validate ALL event times before a caller can write any of the batch.
        event_interval(body, ev, observed)
        if "occurred_at" in ev:
            ev["occurred_at"] = _parse_utc(ev["occurred_at"]).isoformat().replace("+00:00", "Z")


def parse_message(
    topic: str, payload: bytes, *, observed_at: int | float,
    retained: bool = False, authenticated_device_id: str | None = None,
) -> dict:
    """Decode one complete delivery or raise WireError without side effects.

    observed_at is trusted ingress UTC epoch seconds. retained is the MQTT
    delivery flag (on a subscription replay), not the device's online state.
    Live status on an existing subscription can arrive with RETAIN cleared.
    authenticated_device_id, when known from a trusted transport, must be the
    topic's device. Otherwise per-user broker ACLs MUST enforce the binding.
    Origin (simulated or hardware) is provisioned separately, never supplied
    by JSON. A syntactically valid message is not evidence of real BLE.
    """
    observed = _epoch(observed_at)
    if type(topic) is not str or len(topic) > 64:
        raise WireError("invalid v1 uplink topic")
    device_id, kind = _match(topic, _TOPIC, "v1 uplink topic").groups()
    if authenticated_device_id is not None and (
        type(authenticated_device_id) is not str or authenticated_device_id != device_id
    ):
        raise WireError("authenticated device does not match topic")
    if type(retained) is not bool or (retained and kind == "encounters"):
        raise WireError("invalid retain flag for this delivery")
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_PAYLOAD_BYTES:
        raise WireError("v1 payload must contain 1 to 2048 bytes")
    try:
        text = payload.decode("utf-8", "strict")
        body = json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_float=_no_float,
            parse_constant=_no_float,
        )
    except WireError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise WireError("invalid UTF-8 JSON") from None
    if kind == "status":
        _shape(body, _STATUS, {"wifi_rssi", "battery_mv", "self_short_id"})
        _validate_base(body, device_id)
        _validate_status(body)
        if "self_short_id" in body:
            _match(body["self_short_id"], _SHORT, "self_short_id")
    else:
        _shape(body, _BATCH)
        _validate_base(body, device_id)
        _validate_batch(body, device_id, observed)
    return {"kind": kind, "device_id": device_id, "body": body,
            "observed_at": observed, "retained": retained}

