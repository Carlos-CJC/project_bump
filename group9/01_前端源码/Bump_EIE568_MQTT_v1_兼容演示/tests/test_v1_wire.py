"""Contract checks for the firmware v1 decoder; no network, broker or hardware."""

from datetime import datetime, timezone
import json

import pytest

from bump.v1_wire import MAX_PAYLOAD_BYTES, WireError, event_interval, parse_message


DEV_HEX = "a1b2c3d4e5f6"
DEV = "bump-" + DEV_HEX
BOOT = "9f3c1a02"
BASE = "bump/v1/device/" + DEV + "/"
NOW = datetime(2026, 11, 18, 12, tzinfo=timezone.utc).timestamp()


def pack(body):
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def example(count=1):
    return {
        "schema": 1, "device_id": DEV, "boot_id": BOOT, "fw": "0.4.0",
        "batch_id": f"bt_{DEV_HEX}_{BOOT}_00000007_{count:04d}",
        "self_short_id": "3f9a01c47b2e", "uptime_ms": 287431,
        "time_source": "uptime",
        "events": [
            {"event_id": f"ev_{DEV_HEX}_{BOOT}_{seq:08x}",
             "peer_short_id": "8c21d0a9e4f5", "rssi_avg": -65, "rssi_last": -63,
             "duration_s": 7, "samples": 2, "flags": 1, "peer_tx_power": -4,
             "occurred_uptime_ms": 214003}
            for seq in range(7, 7 + count)
        ],
    }


def status():
    return {
        "schema": 1, "device_id": DEV, "boot_id": BOOT, "fw": "0.4.0",
        "state": "online", "uptime_s": 287, "queue_depth": 2,
        "events_dropped": 0, "wifi_rssi": -58,
    }


def decode(body, kind="encounters", **options):
    return parse_message(BASE + kind, pack(body), observed_at=NOW, **options)


@pytest.mark.parametrize("count", [1, 2, 8])
def test_documented_batch_and_trusted_metadata(count):
    body = example(count)
    msg = decode(body, authenticated_device_id=DEV)
    assert msg == {"kind": "encounters", "device_id": DEV, "body": body,
                   "observed_at": NOW, "retained": False}
    assert event_interval(msg["body"], msg["body"]["events"][0], NOW) == pytest.approx(
        (NOW - 73.428 - 7, NOW - 73.428), rel=0, abs=1e-6
    )


@pytest.mark.parametrize("state,retained", [("offline", True), ("online", False), ("online", True)])
def test_status_live_or_retained_is_metadata_not_proof_of_presence(state, retained):
    body = {**status(), "state": state, "battery_mv": 3500}
    msg = decode(body, "status", retained=retained)
    assert msg["body"] == body and msg["retained"] is retained
    assert "self_short_id" not in msg["body"] and "source" not in msg


def test_no_source_claim_no_optional_values_required():
    body = example()
    del body["events"][0]["peer_tx_power"]
    assert decode(body)["body"] == body
    body["source"] = "hardware"
    with pytest.raises(WireError):
        decode(body)


@pytest.mark.parametrize("topic", [
    "legacy/whatever", BASE + "ack", BASE + "config", BASE + "encounters/",
    BASE + "status/extra", BASE.replace("v1", "v2") + "status",
    BASE.replace(DEV, "+") + "status", BASE.replace(DEV, DEV.upper()) + "status",
    BASE.replace(DEV, "bump-000000000000") + "status",
])
def test_rejects_wrong_topics_and_topic_payload_mismatch(topic):
    with pytest.raises(WireError):
        parse_message(topic, pack(status()), observed_at=NOW)


def test_checks_known_authenticated_principal_and_retain_flag():
    with pytest.raises(WireError):
        decode(status(), "status", authenticated_device_id="bump-000000000000")
    with pytest.raises(WireError):
        decode(example(), retained=True)
    with pytest.raises(WireError):
        decode(status(), "status", retained=1)


def test_actual_utf8_bytes_bound_includes_whitespace():
    body = {**example(), "fw": "\u56fa\u4ef6-0.4"}
    raw = pack(body)
    at_limit = raw + b" " * (MAX_PAYLOAD_BYTES - len(raw))
    assert len(at_limit) == 2048 and len((at_limit + b" ").decode()) < 2048
    assert parse_message(BASE + "encounters", at_limit, observed_at=NOW)["body"] == body
    with pytest.raises(WireError):
        parse_message(BASE + "encounters", at_limit + b" ", observed_at=NOW)


@pytest.mark.parametrize("raw", [
    b"", b"[]", b"not-json", b"{", b"null", b"\xff", b"\xef\xbb\xbf{}",
    b"[" * 1000 + b"]" * 1000,
    b'{"schema":1,"schema":1}', b'{"a":NaN}', b'{"a":Infinity}',
])
def test_unusable_framing_is_reported_as_validation_error(raw):
    with pytest.raises(WireError):
        parse_message(BASE + "encounters", raw, observed_at=NOW)


def test_duplicate_members_rejected_in_valid_event_and_envelope():
    raw = pack(example())
    for old, new in (
        (b'"flags":1', b'"flags":1,"flags":1'),
        (b'"schema":1', b'"schema":1,"sche\\u006da":1'),
    ):
        with pytest.raises(WireError, match="duplicate"):
            parse_message(BASE + "encounters", raw.replace(old, new), observed_at=NOW)


@pytest.mark.parametrize("field,value", [
    ("schema", True), ("schema", 2), ("schema", 1.0), ("schema", "1"),
    ("boot_id", "9F3C1A02"), ("self_short_id", "short"), ("fw", "x" * 33),
    ("fw", ""), ("fw", "\n"), ("time_source", None),
    ("uptime_ms", 4294967296), ("uptime_ms", -1), ("uptime_ms", False),
    ("batch_id", f"bt_000000000000_{BOOT}_00000007_0001"),
    ("batch_id", f"bt_{DEV_HEX}_10000000_00000007_0001"),
    ("batch_id", f"bt_{DEV_HEX}_{BOOT}_00000008_0001"),
    ("batch_id", f"bt_{DEV_HEX}_{BOOT}_00000007_0002"),
])
def test_envelope_values_are_typed_bounded_and_consistent(field, value):
    body = example()
    body[field] = value
    with pytest.raises(WireError):
        decode(body)


@pytest.mark.parametrize("field,value", [
    ("event_id", f"ev_000000000000_{BOOT}_00000007"),
    ("event_id", f"ev_{DEV_HEX}_00000000_00000007"),
    ("peer_short_id", "8C21D0A9E4F5"), ("rssi_avg", -128), ("rssi_last", 1),
    ("rssi_avg", True), ("rssi_last", -63.0), ("duration_s", -1),
    ("duration_s", 86401), ("samples", 0), ("samples", 65536), ("samples", True),
    ("flags", -1), ("flags", 256), ("peer_tx_power", -129), ("peer_tx_power", 128),
    ("occurred_uptime_ms", 4294967296), ("occurred_at", "2026-11-18T12:00:00Z"),
])
def test_event_values_are_typed_bounded_and_consistent(field, value):
    body = example()
    body["events"][0][field] = value
    with pytest.raises(WireError):
        decode(body)


def test_batch_count_distinct_event_ids_and_required_fields():
    for n in (0, 9):
        # Keep count=9 under 2KB as well: independently exercise the count limit.
        body = example(n)
        for ev in body["events"]:
            del ev["peer_tx_power"]
        assert len(pack(body)) < MAX_PAYLOAD_BYTES
        with pytest.raises(WireError):
            decode(body)
    body = example(2)
    body["events"][1]["event_id"] = body["events"][0]["event_id"]
    with pytest.raises(WireError):
        decode(body)
    body = example(2)
    body["events"][1].pop("flags")
    with pytest.raises(WireError):
        decode(body)


def test_sparse_event_numbers_are_not_mistaken_for_a_count():
    body = example(2)
    body["events"][1]["event_id"] = f"ev_{DEV_HEX}_{BOOT}_0000ffff"
    assert decode(body)["body"] == body


@pytest.mark.parametrize("field,value", [
    ("state", "idle"), ("uptime_s", -1), ("uptime_s", 2**32),
    ("queue_depth", -1), ("queue_depth", 256), ("events_dropped", 2**32),
    ("wifi_rssi", False), ("battery_mv", 2**63), ("boot_id", 12),
])
def test_status_bounds(field, value):
    body = {**status(), field: value}
    with pytest.raises(WireError):
        decode(body, "status")


@pytest.mark.parametrize("sent,seen,age", [
    (86400001, 0, 86400.0), (1, 500000, -300.0),
    (25, 2**32 - 25, 0.050), (0, 0, 0.0),
])
def test_uptime_clamp_and_rollover(sent, seen, age):
    body = example()
    body["uptime_ms"] = sent
    body["events"][0]["occurred_uptime_ms"] = seen
    ev = decode(body)["body"]["events"][0]
    start, end = event_interval(body, ev, NOW)
    assert end == pytest.approx(NOW - age, rel=0, abs=1e-6)
    assert start == end - ev["duration_s"]


def test_ntp_is_utc_future_bounded_and_omission_falls_back():
    body = {**example(), "time_source": "ntp"}
    assert decode(body)["body"] == body
    body["events"][0]["occurred_at"] = "2026-11-18T12:10:00+00:00"
    norm = decode(body)["body"]
    assert norm["events"][0]["occurred_at"] == "2026-11-18T12:10:00Z"
    assert event_interval(norm, norm["events"][0], NOW) == (NOW + 593, NOW + 600)
    for stamp in ("2026-11-18T12:10:00.001Z", "2026-11-18T12:00:00",
                  "2026-11-18T12:00:00+08:00", "2026-02-31T00:00:00Z", "",
                  "2026-11-18T12:00:00-00:00"):
        body["events"][0]["occurred_at"] = stamp
        with pytest.raises(WireError):
            decode(body)


@pytest.mark.parametrize("invalid", [True, None, float("nan"), float("inf"), 10**100])
def test_observation_clock_must_be_finite_trusted_epoch(invalid):
    with pytest.raises(WireError):
        parse_message(BASE + "status", pack(status()), observed_at=invalid)
