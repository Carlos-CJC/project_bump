"""Validation shared by the MQTT worker and durable event store."""

from datetime import datetime, timezone
import math


class EventValidationError(ValueError):
    """The event cannot be accepted under the shared protocol."""


class EventConflictError(ValueError):
    """An existing event key was reused with different content."""


EVENT_FIELDS = {
    "schema_version", "device_id", "boot_id", "seq", "session_id",
    "peer_alias", "start_ms", "end_ms", "start_utc", "end_utc",
    "time_quality", "rssi_median", "sample_count", "source",
}
MAX_INTEGER = 2**63 - 1


def _integer(value, name, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise EventValidationError(f"{name} must be an integer >= {minimum}")


def _timestamp(value, name):
    if not isinstance(value, str) or len(value) > 64:
        raise EventValidationError(f"{name} must be a timezone-aware ISO timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise EventValidationError(
            f"{name} must be a timezone-aware ISO timestamp"
        ) from None


def validate_event(event, topic_device_id, settings):
    """Return canonical content without modifying the supplied event."""
    if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
        raise EventValidationError("event fields do not match schema version 1")
    for field in ("device_id", "boot_id", "session_id", "peer_alias"):
        value = event[field]
        if not isinstance(value, str) or not value or len(value) > 128:
            raise EventValidationError(f"{field} must be a non-empty short string")
    if type(event["schema_version"]) is not int or event["schema_version"] != 1:
        raise EventValidationError("unsupported schema_version")
    if event["device_id"] != topic_device_id:
        raise EventValidationError("device_id does not match authenticated topic")
    for field in ("seq", "sample_count"):
        _integer(event[field], field, minimum=1)
    for field in ("start_ms", "end_ms"):
        _integer(event[field], field)
    if event["end_ms"] < event["start_ms"]:
        raise EventValidationError("end_ms precedes start_ms")
    rssi = event["rssi_median"]
    if (
        type(rssi) not in (int, float)
        or not -127 <= rssi <= 20
        or not math.isfinite(rssi)
    ):
        raise EventValidationError("rssi_median must be a finite number in -127..20")
    if event["source"] not in ("simulated", "hardware"):
        raise EventValidationError("source must be simulated or hardware")
    devices = settings["devices"]
    session = settings["sessions"].get(event["session_id"])
    if (
        event["device_id"] not in devices
        or session is None
        or event["device_id"] not in session.values()
    ):
        raise EventValidationError("unknown device or session")
    peer = session.get(event["peer_alias"])
    if peer not in devices or peer == event["device_id"]:
        raise EventValidationError("peer must be a different device in the session")
    normalized = dict(event)
    normalized["rssi_median"] = float(rssi)
    if event["time_quality"] == "unknown":
        if event["start_utc"] is not None or event["end_utc"] is not None:
            raise EventValidationError("unknown time requires null UTC values")
    elif event["time_quality"] == "synced":
        start = _timestamp(event["start_utc"], "start_utc")
        end = _timestamp(event["end_utc"], "end_utc")
        if end < start:
            raise EventValidationError("end_utc precedes start_utc")
        duration_ms = (end - start).total_seconds() * 1000
        if abs(duration_ms - (event["end_ms"] - event["start_ms"])) > 1000:
            raise EventValidationError("UTC and monotonic durations disagree")
        for field, stamp in (("start_utc", start), ("end_utc", end)):
            normalized[field] = stamp.isoformat(timespec="microseconds").replace(
                "+00:00", "Z"
            )
    else:
        raise EventValidationError("time_quality must be synced or unknown")
    return normalized
