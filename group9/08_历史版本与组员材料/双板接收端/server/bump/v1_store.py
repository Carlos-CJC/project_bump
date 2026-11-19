"""Bounded hardware-v1 observations and short-ID resolution."""

from contextlib import contextmanager
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sqlite3
import uuid

from .v1_wire import event_interval


REGISTRATION_WINDOW_S = 24 * 3600
MUTUAL_WINDOW_S = 900
DISPLAY_WINDOW_S = 8 * 3600
STATUS_FRESH_S = 600


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode("ascii")).hexdigest()


def _time(value):
    if type(value) not in (float, int) or not isfinite(value) or value < 0:
        raise ValueError("invalid server timestamp")
    return float(value)


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS bindings (
        device TEXT NOT NULL, boot TEXT NOT NULL, short TEXT NOT NULL,
        first_seen REAL NOT NULL, last_seen REAL NOT NULL, PRIMARY KEY(device,boot)
    )""",
    "CREATE INDEX IF NOT EXISTS binding_short ON bindings(short)",
    # A retransmitted envelope is not a fresh sighting. Separate observation
    # points also avoid treating an entire multi-day reporting gap as evidence.
    """CREATE TABLE IF NOT EXISTS sightings (
        device TEXT NOT NULL, boot TEXT NOT NULL, envelope_hash TEXT NOT NULL,
        observed_at REAL NOT NULL, PRIMARY KEY(device,boot,envelope_hash),
        FOREIGN KEY(device,boot) REFERENCES bindings(device,boot)
    )""",
    "CREATE INDEX IF NOT EXISTS sight_time ON sightings(device,boot,observed_at)",
    """CREATE TABLE IF NOT EXISTS observations (
        event_id TEXT PRIMARY KEY, event_hash TEXT NOT NULL,
        card_key TEXT UNIQUE NOT NULL, device TEXT NOT NULL, boot TEXT NOT NULL,
        peer_short TEXT NOT NULL, start_at REAL NOT NULL, seen_at REAL NOT NULL,
        duration_s REAL NOT NULL, samples INTEGER NOT NULL, rssi_avg REAL NOT NULL,
        received_at REAL NOT NULL, transport TEXT NOT NULL, time_quality TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY(device,boot) REFERENCES bindings(device,boot)
    )""",
    "CREATE INDEX IF NOT EXISTS obs_owner_time ON observations(device,hidden,seen_at)",
    """CREATE TABLE IF NOT EXISTS live_status (
        device TEXT PRIMARY KEY, boot TEXT NOT NULL, state TEXT NOT NULL,
        observed_at REAL NOT NULL, uptime_s INTEGER NOT NULL,
        queue_depth INTEGER, events_dropped INTEGER
    )""",
    # Evaluate identity from current evidence, never from "there are only two
    # badges". A new collision invalidates a formerly unique match. Multiple
    # boots in range, even with the same owner, stay unresolved. A self-match
    # cannot qualify as a peer.
    f"""CREATE VIEW IF NOT EXISTS resolved AS
        SELECT o.rowid AS observation_id, o.*,
          (SELECT CASE WHEN COUNT(*)=1 AND MIN(b.device)<>o.device
                       THEN MIN(b.device) END
           FROM bindings AS b
           WHERE b.short=o.peer_short AND EXISTS (
             SELECT 1 FROM sightings AS s
             WHERE s.device=b.device AND s.boot=b.boot
               AND s.observed_at BETWEEN o.seen_at-{REGISTRATION_WINDOW_S}
                                     AND o.seen_at+{REGISTRATION_WINDOW_S}
           )) AS peer_device
        FROM observations AS o""",
)


class V1Store:
    def __init__(self, path: Path, aliases):
        self.aliases = aliases
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction(write=True) as db:
            for sql in _SCHEMA:
                db.execute(sql)

    @contextmanager
    def _transaction(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=8, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _register(db, device, body, observed_at):
        boot, short = body["boot_id"], body["self_short_id"]
        previous = db.execute("SELECT short FROM bindings WHERE device=? AND boot=?",
                              (device, boot)).fetchone()
        if previous is not None and previous["short"] != short:
            raise ValueError("one boot cannot register two self short IDs")
        if previous is None:
            db.execute("INSERT INTO bindings VALUES(?,?,?,?,?)",
                       (device, boot, short, observed_at, observed_at))
        point = db.execute(
            "INSERT OR IGNORE INTO sightings VALUES(?,?,?,?)",
            (device, boot, _digest(body), observed_at),
        )
        if point.rowcount:
            db.execute("""UPDATE bindings SET first_seen=MIN(first_seen,?),
                                 last_seen=MAX(last_seen,?) WHERE device=? AND boot=?""",
                       (observed_at, observed_at, device, boot))

    @staticmethod
    def _status(db, device, body, observed_at, retained):
        state, boot = body["state"], body["boot_id"]
        if state not in ("online", "offline"):
            raise ValueError("unknown status state")
        if retained:
            # An initial retained online packet has no trusted receipt time.
            # A retained last-will may clear its own boot but cannot refresh it.
            if state == "offline":
                db.execute("""UPDATE live_status SET state='offline'
                              WHERE device=? AND boot=? AND observed_at<=?""",
                           (device, boot, observed_at))
            return
        previous = db.execute("SELECT * FROM live_status WHERE device=?", (device,)).fetchone()
        if previous is not None:
            if observed_at < previous["observed_at"]:
                return
            if (observed_at == previous["observed_at"] and previous["state"] == "offline"
                    and state == "online"):
                return
            if (boot == previous["boot"] and state == "online"
                    and body["uptime_s"] <= previous["uptime_s"]):
                return  # An old/duplicated heartbeat is not proof of fresh presence.
        db.execute("""INSERT INTO live_status VALUES(?,?,?,?,?,?,?)
                      ON CONFLICT(device) DO UPDATE SET
                      boot=excluded.boot, state=excluded.state,
                      observed_at=excluded.observed_at, uptime_s=excluded.uptime_s,
                      queue_depth=excluded.queue_depth, events_dropped=excluded.events_dropped""",
                   (device, boot, state, observed_at, body["uptime_s"],
                    body.get("queue_depth"), body.get("events_dropped")))

    def ingest(self, parsed: dict, *, received_at: float):
        """Commit a wire-validated message, or reject the entire message.

        Summary counts concern the distinct event IDs in this incoming batch
        (duplicates counts every exact repeat). Event identity does not include
        batch_id, server receipt time, publish time, or the other batch members.
        """
        received_at = _time(received_at)
        observed_at = _time(parsed["observed_at"])
        device, body = parsed["device_id"], parsed["body"]
        if device not in self.aliases or body.get("device_id") != device:
            raise ValueError("device not provisioned")
        retained = parsed.get("retained", False)
        if type(retained) is not bool:
            raise ValueError("invalid retained flag")
        inserted = duplicates = pending = 0
        receipts = {}
        with self._transaction(write=True) as db:
            if parsed["kind"] == "status":
                self._status(db, device, body, observed_at, retained)
                if not retained and body["state"] == "online" and "self_short_id" in body:
                    self._register(db, device, body, observed_at)
            elif parsed["kind"] == "encounters" and not retained:
                self._register(db, device, body, observed_at)
                ids = set()
                for event in body["events"]:
                    event_id = event["event_id"]
                    fingerprint = _digest([device, body["boot_id"], event])
                    ids.add(event_id)
                    prior = db.execute("SELECT event_hash FROM observations WHERE event_id=?",
                                       (event_id,)).fetchone()
                    if prior is not None:
                        if prior["event_hash"] != fingerprint:
                            raise ValueError("event_id already has different content")
                        duplicates += 1
                        receipts[event_id] = "duplicate"
                        continue
                    start, end = event_interval(body, event, observed_at)
                    if not (isfinite(start) and isfinite(end) and start <= end):
                        raise ValueError("invalid event interval")
                    db.execute("""INSERT INTO observations
                                  (event_id,event_hash,card_key,device,boot,peer_short,start_at,
                                   seen_at,duration_s,samples,rssi_avg,received_at,transport,time_quality)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (event_id, fingerprint, uuid.uuid4().hex, device, body["boot_id"],
                                event["peer_short_id"], start, end, event["duration_s"],
                                event["samples"], event["rssi_avg"], received_at, parsed["transport"],
                                "synced" if "occurred_at" in event else "estimated"))
                    inserted += 1
                    receipts[event_id] = "stored"
                if ids:
                    marks = ",".join("?" for _ in ids)
                    pending = db.execute(
                        f"SELECT COUNT(*) FROM resolved WHERE event_id IN ({marks}) AND peer_device IS NULL",
                        tuple(ids),
                    ).fetchone()[0]
            else:
                raise ValueError("unsupported message kind or retained encounter")
        return {"inserted": inserted, "duplicates": duplicates, "pending": pending, "receipts": receipts}

    def contains_event_ids(self, ids):
        """Internal post-commit transport receipt. It is not a public search API."""
        if isinstance(ids, (str, bytes)):
            return False
        ids = tuple(ids)
        if not ids or any(type(x) is not str for x in ids) or len(set(ids)) != len(ids):
            return False
        # No unbounded SQLite IN expression; the broker normally verifies two.
        with self._transaction() as db:
            return all(db.execute("SELECT 1 FROM observations WHERE event_id=?", (eid,)).fetchone()
                       is not None for eid in ids)

    @staticmethod
    def _groups(db, device):
        # Only an authenticated owner's visible direction is projected. A
        # hidden reverse observation is not used as evidence of mutuality.
        rows = db.execute(
            """SELECT e.*, EXISTS (
                 SELECT 1 FROM resolved AS r
                 WHERE r.device=e.peer_device AND r.peer_device=e.device AND r.hidden=0
                   AND r.transport=e.transport
                   AND r.seen_at BETWEEN e.seen_at-? AND e.seen_at+?
               ) AS mutual
               FROM resolved AS e WHERE e.device=? AND e.hidden=0
               ORDER BY e.seen_at,e.event_id""",
            (MUTUAL_WINDOW_S, MUTUAL_WINDOW_S, device),
        )
        groups, last_by_peer = [], {}
        for row in rows:
            peer = row["peer_device"]
            # Anchor the eight-hour span to the FIRST event, not the latest.
            # Never group different unknown peers under a single "unknown".
            group_key = (peer, row["transport"])
            group = last_by_peer.get(group_key) if peer is not None else None
            if group is None or row["seen_at"] > group[0]["seen_at"] + DISPLAY_WINDOW_S:
                group = []
                groups.append(group)
                if peer is not None:
                    last_by_peer[group_key] = group
            group.append(row)
        return sorted(groups, key=lambda g: (g[-1]["seen_at"], g[-1]["event_id"]), reverse=True)



