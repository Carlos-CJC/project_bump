"""Deterministic matching regressions with synthetic packets and independent SQLite databases."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
BASELINE = "--demonstrate-baseline" in sys.argv
if BASELINE:
    sys.path.insert(0, str(HERE.parent / "server"))

from live_server import ingest, uplink_topic
from bump.live_store import LiveStore
from bump.v1_wire import WireError, parse_message


A, B = "bump-aa1100000001", "bump-bb2200000002"
A0, B0, B1 = "a0a0a0a0", "b0b0b0b0", "b1000000"
A_SHORT, B_SHORT, B_NEW = "0aa110000001", "0bb220000002", "0bb220000003"
T = 1_800_000_000.0
U32 = 2**32 - 1


def app(path):
    return LiveStore({
        "db_path": str(path),
        "bindings": {
            "alice": {"device_id": A, "alias": "挂件 A", "code16": "A30F"},
            "bob": {"device_id": B, "alias": "挂件 B", "code16": "C371"},
        },
        "users": [
            {"id": name, "password": "synthetic-test-only", "display_name": name}
            for name in ("alice", "bob")
        ],
    })


def encode(body):
    return json.dumps(body, separators=(",", ":")).encode()


def status(store, device, boot, short, stamp):
    body = {"schema": 1, "device_id": device, "boot_id": boot, "fw": "0.5.2-dual",
            "state": "online", "self_short_id": short, "uptime_s": 1,
            "queue_depth": 0, "events_dropped": 0}
    ingest(store, uplink_topic(device, "status"), encode(body), device, stamp, "mqtt")


def packet(device, boot, own, peer, stamp, *, seq=1, window=True, first=5, last=22,
           wait_s=1.0, start_uptime=128000):
    publish_delta_ms = 60000 + round(wait_s * 1000)
    item = {
        "event_id": f"ev_{device[5:]}_{boot}_{seq:08x}", "peer_short_id": peer,
        "rssi_avg": -60, "rssi_last": -61, "duration_s": last - first,
        "samples": 6, "flags": 1, "occurred_uptime_ms": (start_uptime + last * 1000) & U32,
    }
    if window:
        item["discovery_started_uptime_ms"] = start_uptime
        item["discovery_ended_uptime_ms"] = (start_uptime + 60000) & U32
    body = {
        "schema": 1, "device_id": device, "boot_id": boot, "fw": "0.5.3-dual" if window else "0.5.2-dual",
        "self_short_id": own, "time_source": "uptime",
        "batch_id": f"bt_{device[5:]}_{boot}_{seq:08x}_0001",
        "uptime_ms": (start_uptime + publish_delta_ms) & U32, "events": [item],
    }
    return body, stamp + publish_delta_ms / 1000


def send(store, device, boot, own, peer, stamp, **kwargs):
    body, received = packet(device, boot, own, peer, stamp, **kwargs)
    receipts = ingest(store, uplink_topic(device, "encounters"), encode(body), device, received, "mqtt")
    assert receipts == {body["events"][0]["event_id"]: "stored"}


def earlier_windows(store, window):
    send(store, A, A0, A_SHORT, B_SHORT, T, window=window)
    send(store, B, B0, B_SHORT, A_SHORT, T + 180, window=window)


def reboot_between(store, window):
    status(store, B, B0, B_SHORT, T)
    send(store, A, A0, A_SHORT, B_SHORT, T, window=window)
    send(store, B, B1, B_NEW, A_SHORT, T + 8, window=window)


def history_bleed(store, window):
    send(store, A, A0, A_SHORT, B_SHORT, T, window=window)
    send(store, B, B0, B_SHORT, A_SHORT, T, window=window)
    send(store, B, B0, B_SHORT, A_SHORT, T + 1080, seq=2, window=window)


def states(store, who):
    return [item["match_state"] for item in store.list_encounters(who)]


def current_issues(root):
    inputs = ((earlier_windows, "independent rounds"), (reboot_between, "cross-reboot"),
              (history_bleed, "historical label on newer observation"))
    for i, (scenario, symptom) in enumerate(inputs):
        store = app(root / f"baseline-{i}.sqlite3")
        scenario(store, window=False)
        cards = store.list_encounters("bob")
        assert len(cards) == 1 and cards[0]["mutual"], symptom
    print("Reproduced three false confirmations: separate rounds, cross-boot peer identity, and later card borrowing earlier mutuality.")


def old_schema(path):
    # Observations from the pre-window schema, retained exactly without copying real data.
    with closing(sqlite3.connect(path)) as con:
        with con:
            con.execute("""CREATE TABLE observations (
                event_id TEXT PRIMARY KEY, event_hash TEXT NOT NULL,
                card_key TEXT UNIQUE NOT NULL, device TEXT NOT NULL, boot TEXT NOT NULL,
                peer_short TEXT NOT NULL, start_at REAL NOT NULL, seen_at REAL NOT NULL,
                duration_s REAL NOT NULL, samples INTEGER NOT NULL, rssi_avg REAL NOT NULL,
                received_at REAL NOT NULL, transport TEXT NOT NULL, time_quality TEXT NOT NULL,
                hidden INTEGER NOT NULL DEFAULT 0
            )""")
            con.execute("""INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        ("legacy", "hash", "old-card", A, A0, B_SHORT, T, T + 20, 20, 6, -60,
                         T + 60, "mqtt", "estimated", 0))


def regressions(root):
    cases = [(earlier_windows, ["single"]), (reboot_between, ["single"]),
             (history_bleed, ["single", "confirmed"])]
    for i, (scenario, expected) in enumerate(cases):
        store = app(root / f"regression-{i}.sqlite3")
        scenario(store, window=True)
        assert states(store, "bob") == expected, scenario.__name__

    store = app(root / "two-phases.sqlite3")
    send(store, A, A0, A_SHORT, B_SHORT, T, window=True, first=5, last=22, wait_s=3600,
         start_uptime=U32 - 26000)
    send(store, B, B0, B_SHORT, A_SHORT, T + 7, window=True, first=28, last=52)
    alice, bob = [store.list_encounters(who)[0] for who in ("alice", "bob")]
    assert alice["end_utc"] < bob["start_utc"]
    assert alice["received_at"] > bob["received_at"]
    assert alice["mutual"] and bob["mutual"] and alice["can_invite"]
    replay, received = packet(A, A0, A_SHORT, B_SHORT, T, window=True, first=5, last=22,
                              wait_s=7200, start_uptime=U32 - 26000)
    assert ingest(store, uplink_topic(A, "encounters"), encode(replay), A, received + 1.2, "mqtt") == {
        replay["events"][0]["event_id"]: "duplicate"}
    with store._transaction() as db:
        event = db.execute("SELECT * FROM observations WHERE device=?", (A,)).fetchone()
        assert (event["discovery_start_at"], event["discovery_end_at"]) == (T, T + 60)
        assert event["received_at"] == T + 3660

    store = app(root / "review.sqlite3")
    send(store, A, A0, A_SHORT, B_SHORT, T, window=True)
    send(store, B, B0, B_SHORT, A_SHORT, T + 20, window=True)
    assert states(store, "alice") == states(store, "bob") == ["review"]
    assert not store.list_encounters("bob")[0]["can_invite"]

    store = app(root / "adjacent-round-edge.sqlite3")
    # A's samples preceded B's selected window; A may have seen a previous B round.
    send(store, A, A0, A_SHORT, B_SHORT, T, window=True, first=1, last=4)
    send(store, B, B0, B_SHORT, A_SHORT, T + 8, window=True, first=28, last=52)
    assert states(store, "alice") == states(store, "bob") == ["review"]

    store = app(root / "ambiguous.sqlite3")
    send(store, A, A0, A_SHORT, B_SHORT, T, window=True)
    send(store, A, A0, A_SHORT, B_SHORT, T, seq=2, window=True)
    send(store, B, B0, B_SHORT, A_SHORT, T, window=True)
    assert states(store, "alice") == ["review", "review"]
    assert states(store, "bob") == ["review"]

    store = app(root / "legacy.sqlite3")
    send(store, A, A0, A_SHORT, B_SHORT, T, window=False)
    send(store, B, B0, B_SHORT, A_SHORT, T + 7, window=False)
    assert states(store, "alice") == states(store, "bob") == ["review"]

    body, stamp = packet(A, A0, A_SHORT, B_SHORT, T)
    e = body["events"][0]
    e.pop("discovery_ended_uptime_ms")
    try:
        parse_message(uplink_topic(A, "encounters"), encode(body), observed_at=stamp)
    except WireError:
        pass
    else:
        raise AssertionError("accepted incomplete window timing")
    body, stamp = packet(A, A0, A_SHORT, B_SHORT, T)
    body["uptime_ms"] -= 2000
    try:
        parse_message(uplink_topic(A, "encounters"), encode(body), observed_at=stamp)
    except WireError:
        pass
    else:
        raise AssertionError("accepted pre-window-end publish time")

    path = root / "migration.sqlite3"
    old_schema(path)
    for _ in range(2):
        store = app(path)
        with store._transaction() as db:
            columns = {x["name"] for x in db.execute("PRAGMA table_info(observations)")}
            assert {"discovery_start_at", "discovery_end_at"} <= columns
            assert tuple(db.execute("SELECT event_id,discovery_start_at,discovery_end_at FROM observations").fetchone()) == (
                "legacy", None, None)
    print("PASS: separate/rebooted rounds and later cards stay single; unique staggered windows match; "
          "skew/ambiguity/legacy stay under review; delayed rollover upload preserves occurrence; schema migration is additive.")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="matching-test-", dir=HERE) as directory:
        (current_issues if BASELINE else regressions)(Path(directory))

