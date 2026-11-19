"""Bounded hardware-v1 observations and short-ID resolution."""

from contextlib import contextmanager
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sqlite3
import uuid

from .v1_wire import discovery_interval, event_interval


REGISTRATION_WINDOW_S = 24 * 3600
MAX_WINDOW_START_SKEW_S = 12
MIN_WINDOW_OVERLAP_S = 48
SAMPLE_TIME_TOLERANCE_S = 2
LEGACY_REVIEW_WINDOW_S = 60 + MAX_WINDOW_START_SKEW_S


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
        received_at REAL NOT NULL, transport TEXT NOT NULL, time_quality TEXT NOT NULL,
        hidden INTEGER NOT NULL DEFAULT 0, discovery_start_at REAL, discovery_end_at REAL,
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
            previous = {row["name"] for row in db.execute("PRAGMA table_info(observations)")}
            for column in ("discovery_start_at", "discovery_end_at"):
                if column not in previous:
                    db.execute(f"ALTER TABLE observations ADD COLUMN {column} REAL")

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
                    discovery_start, discovery_end = discovery_interval(body, event, end)
                    if not (isfinite(start) and isfinite(end) and start <= end):
                        raise ValueError("invalid event interval")
                    db.execute("""INSERT INTO observations
                                  (event_id,event_hash,card_key,device,boot,peer_short,start_at,
                                   seen_at,duration_s,samples,rssi_avg,received_at,transport,time_quality,
                                   discovery_start_at,discovery_end_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (event_id, fingerprint, uuid.uuid4().hex, device, body["boot_id"],
                                event["peer_short_id"], start, end, event["duration_s"],
                                event["samples"], event["rssi_avg"], received_at, parsed["transport"],
                                "synced" if "occurred_at" in event else "estimated", discovery_start, discovery_end))
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
        # An observation remains its own card. A different round cannot inherit
        # a historical match for these same two physical devices.
        rows = db.execute(
            """WITH identified AS (
                 SELECT r.*, b.short AS own_short
                 FROM resolved AS r JOIN bindings AS b ON b.device=r.device AND b.boot=r.boot
                 WHERE r.hidden=0
               ), candidates AS (
                 SELECT e.event_id AS mine, r.event_id AS theirs,
                   e.discovery_start_at IS NOT NULL AND r.discovery_start_at IS NOT NULL
                   AND ABS(e.discovery_start_at-r.discovery_start_at)<=:start_skew
                   AND MIN(e.discovery_end_at,r.discovery_end_at)
                       - MAX(e.discovery_start_at,r.discovery_start_at)>=:overlap
                   AND e.start_at>=r.discovery_start_at-:sample_slack
                   AND e.seen_at<=r.discovery_end_at+:sample_slack
                   AND r.start_at>=e.discovery_start_at-:sample_slack
                   AND r.seen_at<=e.discovery_end_at+:sample_slack AS close_windows
                 FROM identified AS e JOIN identified AS r
                   ON r.device=e.peer_device AND r.peer_device=e.device
                    AND r.peer_short=e.own_short AND r.own_short=e.peer_short
                    AND r.transport=e.transport
                 WHERE e.device=:device AND (
                   ((e.discovery_start_at IS NULL OR r.discovery_start_at IS NULL)
                     AND ABS(e.seen_at-r.seen_at)<=:legacy_reach)
                    OR MAX(e.discovery_start_at,r.discovery_start_at)
                       < MIN(e.discovery_end_at,r.discovery_end_at)
                 )
               ), with_counts AS (
                 SELECT mine, close_windows, COUNT(*) OVER (PARTITION BY mine) AS own_options,
                   COUNT(*) OVER (PARTITION BY theirs) AS peer_options
                 FROM candidates
               ), confirmed AS (
                 SELECT mine FROM with_counts WHERE close_windows AND own_options=1 AND peer_options=1
               )
               SELECT e.*, EXISTS(SELECT 1 FROM confirmed c WHERE c.mine=e.event_id) AS mutual,
                 CASE
                   WHEN EXISTS(SELECT 1 FROM confirmed c WHERE c.mine=e.event_id) THEN 'confirmed'
                   WHEN EXISTS(SELECT 1 FROM candidates c WHERE c.mine=e.event_id) THEN 'review'
                   ELSE 'single'
                 END AS match_state
               FROM identified AS e WHERE e.device=:device
               ORDER BY e.seen_at DESC,e.event_id DESC""",
            {"device": device, "start_skew": MAX_WINDOW_START_SKEW_S,
             "overlap": MIN_WINDOW_OVERLAP_S, "sample_slack": SAMPLE_TIME_TOLERANCE_S,
             "legacy_reach": LEGACY_REVIEW_WINDOW_S},
        )
        return [[row] for row in rows]
