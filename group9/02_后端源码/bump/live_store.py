"""Physical-badge timeline and consent, sharing one transactional SQLite database."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from .store import AccessDeniedError, NotFoundError, Store
from .v1_store import V1Store, _digest
from .v1_wire import WireError, _integer, _no_duplicate_keys, _no_float, _shape


IR_WINDOW_S = 120
_IR_FIELDS = frozenset(("device_id", "boot_id", "event_id", "self_code16",
                        "peer_code16", "uptime_ms"))


def code16(value):
    if type(value) is int and 1 <= value <= 65535:
        return f"{value:04X}"
    if type(value) is str and re.fullmatch(r"[0-9a-fA-F]{4}", value) and value != "0000":
        return value.upper()
    raise ValueError("invalid IR code16")


def iso(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def parse_ir(device, payload):
    if not 1 <= len(payload) <= 512:
        raise WireError("invalid IR payload length")
    try:
        body = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                          parse_float=_no_float, parse_constant=_no_float)
    except (ValueError, RecursionError) as exc:
        raise WireError("invalid IR JSON") from exc
    _shape(body, _IR_FIELDS)
    boot = body["boot_id"]
    if body["device_id"] != device or type(boot) is not str or not re.fullmatch(r"[0-9a-f]{8}", boot):
        raise WireError("invalid IR device or boot ID")
    event = body["event_id"]
    if type(event) is not str or not re.fullmatch(
        rf"(?:ir|ev)_{device[5:]}_{boot}_[0-9a-f]{{8}}", event
    ):
        raise WireError("invalid IR event ID")
    _integer(body["uptime_ms"], "uptime_ms", 0, 2**32 - 1)
    for field in ("self_code16", "peer_code16"):
        body[field] = code16(body[field])
    return body


class LiveStore(Store, V1Store):
    def __init__(self, settings):
        Store.__init__(self, settings)
        self.by_user = settings["bindings"]
        self.by_device = {item["device_id"]: (user, item)
                          for user, item in self.by_user.items()}
        self.by_code = {item["code16"]: item["device_id"] for item in self.by_user.values()}
        V1Store.__init__(self, Path(settings["db_path"]),
                         {key: item["alias"] for key, (_, item) in self.by_device.items()})
        with self._connection(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ir_events (
                event_id TEXT PRIMARY KEY, event_hash TEXT NOT NULL,
                device TEXT NOT NULL, boot TEXT NOT NULL, peer_device TEXT NOT NULL,
                uptime_ms INTEGER NOT NULL, self_code TEXT NOT NULL, peer_code TEXT NOT NULL,
                received_at REAL NOT NULL, transport TEXT NOT NULL,
                matched INTEGER NOT NULL DEFAULT 0, request_id INTEGER REFERENCES contact_requests(id)
            )""")
            db.execute("""CREATE INDEX IF NOT EXISTS ir_pair_receipt
                       ON ir_events(device, peer_device, matched, received_at)""")

    def ingest_ir(self, device, body, stamp, transport):
        sender, sender_binding = self.by_device[device]
        peer_device = self.by_code.get(body["peer_code16"])
        if body["self_code16"] != sender_binding["code16"] or peer_device in (None, device):
            raise WireError("IR codes must match two different provisioned badges")
        recipient, recipient_binding = self.by_device[peer_device]
        event = body["event_id"]
        fingerprint = _digest(body)
        with self._connection(write=True) as db:
            prior = db.execute("SELECT event_hash FROM ir_events WHERE event_id=?", (event,)).fetchone()
            if prior is not None:
                if prior["event_hash"] != fingerprint:
                    raise WireError("IR event ID already has different content")
                return {event: "duplicate"}
            db.execute("""INSERT INTO ir_events
                          (event_id,event_hash,device,boot,peer_device,uptime_ms,self_code,peer_code,
                           received_at,transport) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (event, fingerprint, device, body["boot_id"], peer_device, body["uptime_ms"],
                        body["self_code16"], body["peer_code16"], stamp, transport))
            reverse = db.execute("""SELECT * FROM ir_events
                       WHERE device=? AND peer_device=? AND matched=0 AND transport=?
                         AND received_at BETWEEN ? AND ?
                       ORDER BY received_at,event_id LIMIT 1""",
                       (peer_device, device, transport, stamp - IR_WINDOW_S,
                        stamp + IR_WINDOW_S)).fetchone()
            if reverse is None:
                return {event: "stored"}
            # Whichever physical board reported first supplies the sender account.
            # Browser acceptance remains the recipient's independent decision.
            if reverse["received_at"] <= stamp:
                sender, recipient = recipient, sender
                sender_binding, recipient_binding = recipient_binding, sender_binding
            relationship = self._relationship(db, sender, recipient)
            request_id = relationship["id"] if relationship and relationship["status"] in (
                "pending", "accepted") else self._new_request(
                    db, sender, recipient, sender_binding["alias"], recipient_binding["alias"], None)
            db.execute("UPDATE ir_events SET matched=1,request_id=? WHERE event_id IN (?,?)",
                       (request_id, event, reverse["event_id"]))
        return {event: "stored"}

    def _new_request(self, db, sender, recipient, sender_alias, recipient_alias, encounter_id):
        low, high = sorted((sender, recipient))
        return db.execute("""INSERT INTO contact_requests
            (sender_id,recipient_id,encounter_id,pair_low,pair_high,sender_alias,
             recipient_alias,status,created_at) VALUES(?,?,?,?,?,?,?,'pending',?)""",
            (sender, recipient, encounter_id, low, high, sender_alias, recipient_alias,
             time.time())).lastrowid

    def _request_view(self, db, row, user_id):
        result = super()._request_view(db, row, user_id)
        result["method"] = "ir" if row["encounter_id"] is None else "ble"
        binding = self.by_user.get(user_id)
        if binding:
            latest = db.execute("""SELECT event_id, received_at, COUNT(*) OVER () AS pair_count
                FROM ir_events WHERE request_id=? AND device=? AND matched=1
                ORDER BY received_at DESC, event_id DESC LIMIT 1""",
                (row["id"], binding["device_id"])).fetchone()
            if latest:
                result["last_ir"] = {"event_id": latest["event_id"],
                                     "received_at": iso(latest["received_at"]),
                                     "pair_count": latest["pair_count"]}
        return result

    def device_status(self, user_id):
        binding = self.by_user.get(user_id)
        if not binding:
            return None
        with self._connection() as db:
            status = db.execute("SELECT * FROM live_status WHERE device=?",
                                (binding["device_id"],)).fetchone()
            sync = db.execute("SELECT * FROM badge_sync WHERE device=?",
                              (binding["device_id"],)).fetchone()
            last = db.execute("""SELECT received_at,transport FROM observations WHERE device=?
                                 UNION ALL SELECT received_at,transport FROM ir_events WHERE device=?
                                 ORDER BY received_at DESC LIMIT 1""",
                              (binding["device_id"], binding["device_id"])).fetchone()
        # An older receiver can still open this DB and write the legacy row.
        # Keep the last proven completion while displaying the newer unknown report.
        current = sync if sync and (not status or sync["observed_at"] >= status["observed_at"]) else None
        source = current["source"] if current else "unknown"
        queue_depth = current["queue_depth"] if current else None
        if source in ("mqtt_will", "connection_lost"):
            sync_state = "interrupted"
        elif queue_depth is not None and queue_depth > 0:
            sync_state = "pending"
        elif source in ("device_offline", "verified_serial") and queue_depth == 0:
            sync_state = "synced"
        elif source == "device_online":
            sync_state = "in_progress"
        else:
            sync_state = "unknown"
        return {"alias": binding["alias"],
                "last_received_at": iso(last["received_at"]) if last else None,
                "last_source": "hardware" if last else None,
                "last_transport": last["transport"] if last else None,
                "last_status_at": iso(status["observed_at"]) if status else None,
                "sync_state": sync_state, "sync_source": source, "queue_depth": queue_depth,
                "sync_observed_at": iso(current["observed_at"] if current else
                                        status["observed_at"] if status else None),
                "last_synced_at": iso(sync["last_synced_at"]) if sync else None,
                "state": ("offline" if source in ("mqtt_will", "connection_lost", "incomplete")
                          else status["state"] if status else None)}

    def list_encounters(self, user_id):
        binding = self.by_user.get(user_id)
        if not binding:
            return []
        result = []
        with self._connection() as db:
            for group in self._groups(db, binding["device_id"]):
                first, latest = group[0], group[-1]
                peer = self.by_device.get(latest["peer_device"])
                mutual = any(row["mutual"] for row in group)
                relation = self._relationship(db, user_id, peer[0]) if peer else None
                accepted = relation and relation["status"] == "accepted"
                result.append({
                    "id": first["observation_id"],
                    "event_id": latest["event_id"],
                    "peer_alias": peer[1]["alias"] if peer else "身份待匹配",
                    "session_id": "BLE 相遇",
                    "start_utc": iso(latest["start_at"]), "end_utc": iso(latest["seen_at"]),
                    "received_at": iso(latest["received_at"]),
                    "time_quality": latest["time_quality"],
                    "duration_ms": round(latest["duration_s"] * 1000),
                    "rssi_median": latest["rssi_avg"], "sample_count": latest["samples"],
                    "source": "hardware", "simulated": False, "transport": latest["transport"],
                    "event_count": len(group), "mutual": mutual, "match_state": latest["match_state"],
                    "can_invite": bool(peer and mutual),
                    "contact_status": relation["status"] if relation else "none",
                    "request_id": relation["id"] if relation else None,
                    "profile": self._profile(db, peer[0]) if accepted else None,
                })
        return result

    def hide_encounter(self, user_id, encounter_id):
        binding = self.by_user.get(user_id)
        if binding:
            with self._connection(write=True) as db:
                for group in self._groups(db, binding["device_id"]):
                    if any(row["observation_id"] == encounter_id for row in group):
                        db.executemany("UPDATE observations SET hidden=1 WHERE rowid=?",
                                       ((row["observation_id"],) for row in group))
                        return
        raise NotFoundError("encounter not found")

    def create_request(self, user_id, encounter_id):
        binding = self.by_user.get(user_id)
        if binding:
            with self._connection(write=True) as db:
                for group in self._groups(db, binding["device_id"]):
                    if not any(row["observation_id"] == encounter_id for row in group):
                        continue
                    peer = self.by_device.get(group[-1]["peer_device"])
                    if not peer or not any(row["mutual"] for row in group):
                        raise AccessDeniedError("a two-sided encounter is required")
                    peer_user, peer_binding = peer
                    relation = self._relationship(db, user_id, peer_user)
                    if relation is None or (
                        relation["status"] not in ("pending", "accepted")
                        and (relation["sender_id"], relation["encounter_id"])
                        != (user_id, encounter_id)
                    ):
                        rid = self._new_request(db, user_id, peer_user, binding["alias"],
                                                peer_binding["alias"], encounter_id)
                        relation = db.execute("SELECT * FROM contact_requests WHERE id=?",
                                              (rid,)).fetchone()
                    return self._request_view(db, relation, user_id)
        raise NotFoundError("encounter not found")

