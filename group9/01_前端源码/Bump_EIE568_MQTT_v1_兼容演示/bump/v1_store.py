"""SQLite persistence for two fictional software badges, not a device registry.

Only the authenticated, loopback TLS worker calls ingest, after wire validation.
The payload cannot opt into hardware mode, assign an account, or grant consent.
This database has no profiles, contact details, friend requests or login data.
"""

from contextlib import contextmanager
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import sqlite3
import uuid

from .config import V1_DEMO_DEVICES as DEMO_IDS
from .v1_wire import event_interval


_ALIASES = {DEMO_IDS["alice"]: "A00001", DEMO_IDS["bob"]: "B00002"}
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
        received_at REAL NOT NULL, hidden INTEGER NOT NULL DEFAULT 0,
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
        SELECT o.*,
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
    def __init__(self, path: Path):
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
        if device not in _ALIASES or body.get("device_id") != device:
            raise ValueError("not an authenticated simulation badge")
        if any(v.get("source", "simulated") != "simulated" for v in (parsed, body)):
            raise ValueError("this isolated store accepts simulation only")
        retained = parsed.get("retained", False)
        if type(retained) is not bool:
            raise ValueError("invalid retained flag")
        inserted = duplicates = pending = 0
        with self._transaction(write=True) as db:
            if parsed["kind"] == "status":
                self._status(db, device, body, observed_at, retained)
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
                        continue
                    start, end = event_interval(body, event, observed_at)
                    if not (isfinite(start) and isfinite(end) and start <= end):
                        raise ValueError("invalid event interval")
                    db.execute("""INSERT INTO observations
                                  (event_id,event_hash,card_key,device,boot,peer_short,start_at,
                                   seen_at,duration_s,samples,rssi_avg,received_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (event_id, fingerprint, uuid.uuid4().hex, device, body["boot_id"],
                                event["peer_short_id"], start, end, event["duration_s"],
                                event["samples"], event["rssi_avg"], received_at))
                    inserted += 1
                if ids:
                    marks = ",".join("?" for _ in ids)
                    pending = db.execute(
                        f"SELECT COUNT(*) FROM resolved WHERE event_id IN ({marks}) AND peer_device IS NULL",
                        tuple(ids),
                    ).fetchone()[0]
            else:
                raise ValueError("unsupported message kind or retained encounter")
        return {"inserted": inserted, "duplicates": duplicates, "pending": pending}

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
            group = last_by_peer.get(peer) if peer is not None else None
            if group is None or row["seen_at"] > group[0]["seen_at"] + DISPLAY_WINDOW_S:
                group = []
                groups.append(group)
                if peer is not None:
                    last_by_peer[peer] = group
            group.append(row)
        return sorted(groups, key=lambda g: (g[-1]["seen_at"], g[-1]["event_id"]), reverse=True)

    def snapshot(self, user_id: str, *, now: float):
        now = _time(now)
        device = DEMO_IDS.get(user_id)
        if device is None:
            return {"items": [], "device": None}
        with self._transaction() as db:
            status = db.execute("SELECT * FROM live_status WHERE device=?", (device,)).fetchone()
            observed = None if status is None else status["observed_at"]
            station = {
                "alias": _ALIASES[device], "source": "simulated", "last_observed_at": observed,
                "online": bool(status and status["state"] == "online"
                               and 0 <= now - observed <= STATUS_FRESH_S),
                "queue_depth": status["queue_depth"] if status else None,
                "events_dropped": status["events_dropped"] if status else None,
            }
            items = []
            for group in self._groups(db, device):
                first, latest = group[0], group[-1]
                # Metrics describe the latest individual observation; durations
                # are NOT summed over a gap or represented as continuous contact.
                items.append({
                    "card_id": first["card_key"], "source": "simulated",
                    "peer_alias": _ALIASES.get(latest["peer_device"]),
                    "first_seen_at": first["seen_at"], "seen_at": latest["seen_at"],
                    "received_at": latest["received_at"], "duration_s": latest["duration_s"],
                    "sample_count": latest["samples"], "rssi_avg": latest["rssi_avg"],
                    "event_count": len(group), "mutual": any(r["mutual"] for r in group),
                })
        return {"items": items, "device": station}

    def hide(self, user_id: str, card_id: str):
        """Tombstone this owner's observations in a visible card, not its peer's.

        Deduplication records survive. A genuinely new event can form a new
        card. No relationship or consent state is read or changed by this API.
        """
        device = DEMO_IDS.get(user_id)
        if device is None or type(card_id) is not str or len(card_id) != 32:
            return False
        with self._transaction(write=True) as db:
            for group in self._groups(db, device):
                # Also accept a prior key in the group after harmless regrouping.
                if not any(row["card_key"] == card_id for row in group):
                    continue
                db.executemany("UPDATE observations SET hidden=1 WHERE device=? AND card_key=?",
                               ((device, row["card_key"]) for row in group))
                return True
            return False
