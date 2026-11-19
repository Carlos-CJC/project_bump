"""Persistence, matching and privacy tests. All devices, clocks and DBs are fake."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import re
import sqlite3

import pytest

from bump.v1_store import DEMO_IDS, V1Store
from bump.v1_wire import parse_message


T = 1_800_000_000.0
SHORT = {"alice": "3f9a01c47b2e", "bob": "8c21d0a9e4f5"}
BOOT = {"alice": "aa000001", "bob": "bb000002"}
OTHER = {"alice": "bob", "bob": "alice"}
UNKNOWN = "ee0000000000"


def ev(user="alice", seq=1, *, boot=None, **changes):
    boot = boot or BOOT[user]
    data = {
        "event_id": f"ev_{DEMO_IDS[user][5:]}_{boot}_{seq:08x}",
        "peer_short_id": SHORT[OTHER[user]], "rssi_avg": -65, "rssi_last": -63,
        "duration_s": 7, "samples": 2, "flags": 1, "peer_tx_power": -4,
        "occurred_uptime_ms": 120000,
    }
    data.update(changes)
    return data


def decode(kind, body, at, *, retained=False):
    dev = body["device_id"]
    return parse_message(f"bump/v1/device/{dev}/{kind}", json.dumps(body).encode(),
                         observed_at=at, retained=retained, authenticated_device_id=dev)


def batch(user="alice", *events, at=T, boot=None, own_short=None, **changes):
    device, boot = DEMO_IDS[user], boot or BOOT[user]
    events = list(events) or [ev(user, boot=boot)]
    first = events[0]["event_id"].rsplit("_", 1)[1]
    body = {
        "schema": 1, "device_id": device, "boot_id": boot, "fw": "0.4.0",
        "batch_id": f"bt_{device[5:]}_{boot}_{first}_{len(events):04d}",
        "self_short_id": own_short or SHORT[user], "uptime_ms": 120000,
        "time_source": "uptime", "events": events,
    }
    body.update(changes)
    return decode("encounters", body, at)


def status(user="alice", *, at=T, retained=False, state="online", **changes):
    body = {
        "schema": 1, "device_id": DEMO_IDS[user], "boot_id": BOOT[user],
        "fw": "0.4.0", "state": state, "uptime_s": 100, "queue_depth": 3,
        "events_dropped": 2, "wifi_rssi": -60,
    }
    body.update(changes)
    return decode("status", body, at, retained=retained)


def put(store, msg, *, received_at=None):
    return store.ingest(msg, received_at=msg["observed_at"] if received_at is None else received_at)


@pytest.fixture
def store(tmp_path):
    return V1Store(tmp_path / "private-test-v1.sqlite3")


def cards(store, user="alice", at=T):
    return store.snapshot(user, now=at)["items"]


def station(store, user="alice", at=T):
    return store.snapshot(user, now=at)["device"]


def test_late_registration_only_from_encounters_and_no_profile_even_if_mutual(store):
    assert store.snapshot("charlie", now=T) == {"items": [], "device": None}
    result = put(store, batch(at=T), received_at=T+3)
    assert result == {"inserted": 1, "duplicates": 0, "pending": 1}
    old = cards(store)[0]
    assert old["peer_alias"] is None and not old["mutual"] and old["received_at"] == T+3
    put(store, status("bob"))
    assert cards(store) == [old] and cards(store, "bob") == []
    put(store, batch("bob", at=T+5))
    a, b = cards(store)[0], cards(store, "bob")[0]
    assert (a["peer_alias"], b["peer_alias"]) == ("B00002", "A00001")
    assert a["mutual"] and b["mutual"] and a["card_id"] == old["card_id"]
    assert a["source"] == b["source"] == station(store)["source"] == "simulated"
    assert re.fullmatch(r"[0-9a-f]{32}", a["card_id"]) and a["card_id"] != b["card_id"]
    # Browser object is an allowlist, not the stored observation/registry row.
    public = json.dumps(store.snapshot("alice", now=T))
    for private in [*DEMO_IDS, *DEMO_IDS.values(), *BOOT.values(), *SHORT.values(),
                    "ev_", "bt_", "user_id", "display_name", "avatar", "contact", "bio", "mac"]:
        assert private not in public
    other_cards = cards(V1Store(store.path))
    assert other_cards == [a] and station(store)["last_observed_at"] is None


def test_event_id_is_global_not_batch_id_and_original_times_are_stable(store):
    one = ev()
    put(store, batch("alice", one, at=T))
    original = cards(store)[0]
    result = put(store, batch("alice", one, ev(seq=2), at=T+137, uptime_ms=130000))
    assert result == {"inserted": 1, "duplicates": 1, "pending": 2}
    items = cards(store)
    assert len(items) == 2  # unresolved identities are not merged
    assert next(item for item in items if item["card_id"] == original["card_id"]) == original
    ids = [one["event_id"], ev(seq=2)["event_id"]]
    assert store.contains_event_ids(ids)
    for invalid in ([], [ids[0], ids[0]], ids[0], [ids[0], "missing"]):
        assert not store.contains_event_ids(invalid)


def test_same_id_different_content_rolls_back_whole_batch_and_registration(store):
    first = ev(seq=9)
    put(store, batch("alice", first))
    good, conflict = ev(seq=1), dict(first, rssi_last=-56)
    def counts():
        db = sqlite3.connect(store.path)
        try:
            return (db.execute("SELECT COUNT(*) FROM sightings").fetchone()[0],
                    db.execute("SELECT last_seen FROM bindings").fetchone()[0])
        finally:
            db.close()
    before = counts()
    with pytest.raises(ValueError, match="different content"):
        put(store, batch("alice", good, conflict, at=T+300))
    assert not store.contains_event_ids([good["event_id"]])
    assert counts() == before
    assert store.contains_event_ids([first["event_id"]]) and len(cards(store)) == 1
    # Reusing one boot with a new self short ID is not a new identity.
    with pytest.raises(ValueError):
        put(store, batch("alice", good, own_short=UNKNOWN))
    assert counts() == before and not store.contains_event_ids([good["event_id"]])


def test_concurrent_redelivery_only_commits_once(store):
    msg = batch()
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: put(store, msg), range(4)))
    assert sum(r["inserted"] for r in replies) == 1
    assert sum(r["duplicates"] for r in replies) == 3
    assert len(cards(V1Store(store.path))) == 1


@pytest.mark.parametrize(("offset", "found"),
                         [(-86401, False), (-86400, True), (86400, True), (86401, False)])
def test_only_actual_registration_within_24h_resolves_pending(store, offset, found):
    put(store, batch())
    put(store, batch("bob", at=T+offset))
    a = cards(store)[0]
    assert (a["peer_alias"] == "B00002") is found
    assert not a["mutual"]


def test_replaying_same_envelope_does_not_freshen_an_old_short_id(store):
    old = batch("bob", at=T-90_000)
    put(store, old)
    put(store, batch("alice", at=T))
    assert cards(store)[0]["peer_alias"] is None
    retry = decode("encounters", old["body"], T)
    assert put(store, retry)["duplicates"] == 1
    assert cards(store)[0]["peer_alias"] is None


def test_registration_gap_is_not_an_observation_and_self_is_not_a_peer(store):
    put(store, batch("bob", ev("bob", seq=1), at=T-90000))
    put(store, batch("bob", ev("bob", seq=2), at=T+90000))
    put(store, batch("alice", ev("alice", seq=1), at=T))
    put(store, batch("alice", ev("alice", seq=2, peer_short_id=SHORT["alice"]), at=T))
    assert all(c["peer_alias"] is None and not c["mutual"] for c in cards(store))


def test_collision_revokes_previous_resolution_instead_of_selecting_first(store):
    put(store, batch())
    put(store, batch("bob"))
    old = cards(store)[0]
    assert old["mutual"]
    boot = "aa000003"
    put(store, batch("alice", ev("alice", boot=boot, peer_short_id=UNKNOWN),
                     boot=boot, own_short=SHORT["bob"], at=T+1))
    previous = next(row for row in cards(store) if row["card_id"] == old["card_id"])
    assert previous["peer_alias"] is None and not previous["mutual"]
    assert not cards(store, "bob")[0]["mutual"]


@pytest.mark.parametrize(("offset", "mutual"), [(0, True), (900, True), (900.001, False),
                                                 (-900, True), (-900.001, False)])
def test_mutual_requires_reverse_direction_within_900_seconds(store, offset, mutual):
    put(store, batch())
    put(store, batch("bob", at=T+offset))
    for user in ("alice", "bob"):
        assert cards(store, user)[0]["mutual"] is mutual


def test_sender_registration_alone_is_not_reciprocal_evidence(store):
    put(store, batch())
    put(store, batch("bob", ev("bob", peer_short_id=UNKNOWN)))
    assert cards(store)[0]["peer_alias"] == "B00002"
    assert not cards(store)[0]["mutual"] and not cards(store, "bob")[0]["mutual"]


def test_repeated_two_badge_demo_rotates_both_shorts_without_losing_history(store):
    put(store, batch())
    put(store, batch("bob"))
    before = cards(store)[0]
    new_shorts = {"alice": "f00111111111", "bob": "f00222222222"}
    for user, boot in [("alice", "a1111111"), ("bob", "b2222222")]:
        item = ev(user, boot=boot, peer_short_id=new_shorts[OTHER[user]])
        put(store, batch(user, item, boot=boot, own_short=new_shorts[user], at=T+60))
    a, b = cards(store)[0], cards(store, "bob")[0]
    assert a["card_id"] == before["card_id"]
    assert a["peer_alias"] == "B00002" and b["peer_alias"] == "A00001"
    assert a["event_count"] == b["event_count"] == 2 and a["mutual"] and b["mutual"]


def test_fixed_eight_hour_window_uses_latest_metrics_without_inventing_contact(store):
    put(store, batch())
    put(store, batch("bob"))
    put(store, batch("alice", ev(seq=2), at=T+7*3600))
    put(store, batch("alice", ev(seq=3, duration_s=12, samples=4, rssi_avg=-48), at=T+8*3600))
    put(store, batch("alice", ev(seq=4), at=T+9*3600))
    latest, aggregate = cards(store)
    assert (latest["event_count"], latest["mutual"]) == (1, False)
    assert aggregate["event_count"] == 3 and aggregate["mutual"]
    assert (aggregate["first_seen_at"], aggregate["seen_at"]) == (T, T+8*3600)
    assert (aggregate["duration_s"], aggregate["rssi_avg"], aggregate["sample_count"]) == (12, -48, 4)
    assert store.hide("alice", aggregate["card_id"])
    assert cards(store) == [latest]
    assert store.contains_event_ids(ev(seq=i)["event_id"] for i in range(1, 5))
    assert len(cards(store, "bob")) == 1


def test_owner_hide_does_not_delete_opposite_card_or_resurrect_on_qos_retry(store):
    put(store, batch())
    put(store, batch("bob"))
    alice_key, bob_key = cards(store)[0]["card_id"], cards(store, "bob")[0]["card_id"]
    assert not store.hide("bob", alice_key)
    assert not store.hide("alice", bob_key)
    assert not store.hide("charlie", alice_key)
    assert not store.hide("alice", ev()["event_id"])
    assert store.hide("alice", alice_key) and not store.hide("alice", alice_key)
    put(store, batch(at=T+20))
    reopened = V1Store(store.path)
    assert cards(reopened) == []
    assert reopened.contains_event_ids([ev()["event_id"]])
    b = cards(reopened, "bob")
    assert len(b) == 1 and b[0]["card_id"] == bob_key
    assert not b[0]["mutual"]  # a hidden direction is not reused as reverse proof


def test_retained_replay_never_claims_presence_or_refreshes_last_seen(store):
    put(store, status(retained=True))
    assert not station(store)["online"] and station(store)["last_observed_at"] is None
    put(store, status())
    s = station(store, at=T+600)
    assert s["online"] and s["last_observed_at"] == T and s["queue_depth"] == 3
    assert not station(store, at=T+600.001)["online"]
    put(store, status(at=T+650, retained=True))
    assert not station(store, at=T+650)["online"]
    assert station(store)["last_observed_at"] == T
    put(store, status(at=T+700, uptime_s=800))
    put(store, status(at=T+710, retained=True, state="offline"))
    assert station(store, at=T+710)["last_observed_at"] == T+700
    assert not station(store, at=T+710)["online"]
    put(store, status(at=T+715, retained=True))
    assert not station(store, at=T+715)["online"]
    assert station(store, "bob")["last_observed_at"] is None


def test_old_or_duplicate_live_status_cannot_extend_freshness(store):
    put(store, status(at=T, uptime_s=100))
    put(store, status(at=T+1000, uptime_s=100))
    put(store, status(at=T-20, state="offline", uptime_s=50))
    assert station(store, at=T+601)["last_observed_at"] == T
    assert not station(store, at=T+601)["online"]
    put(store, status(at=T+602, boot_id="aa000007", uptime_s=0))
    put(store, status(at=T+603, retained=True, state="offline"))  # old boot
    assert station(store, at=T+604)["online"]


def test_known_simulators_only_no_hardware_or_retained_encounters(store):
    original = batch()
    for mutate in ("foreign", "spoof", "hardware", "retain"):
        msg = deepcopy(original)
        if mutate == "foreign":
            msg["device_id"] = msg["body"]["device_id"] = "bump-c30000000003"
        elif mutate == "spoof":
            msg["device_id"] = DEMO_IDS["bob"]
        elif mutate == "hardware":
            msg["source"] = "hardware"
        else:
            msg["retained"] = True
        with pytest.raises(ValueError):
            put(store, msg)
    assert cards(store) == [] and not store.contains_event_ids([ev()["event_id"]])
