"""Short-lived SQLite connections, durable events and private demo accounts."""

from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import time

from .protocol import EventConflictError, EventValidationError, validate_event


class StoreError(RuntimeError):
    """Storage failed; the worker must leave the event unacknowledged."""


class AuthenticationError(PermissionError):
    pass


class AccessDeniedError(PermissionError):
    pass


class NotFoundError(LookupError):
    pass


class StateConflictError(RuntimeError):
    pass


def _password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return salt.hex() + ":" + digest.hex()


def _password_matches(password, encoded):
    salt = bytes.fromhex(encoded.split(":")[0])
    return hmac.compare_digest(_password_hash(password, salt), encoded)


class Store:
    def __init__(self, settings):
        self.settings = settings
        self.path = Path(settings["db_path"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._dummy_password = _password_hash(secrets.token_urlsafe(32))
        with self._connection(write=True) as db:
            statements = [
                """CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL, bio TEXT NOT NULL,
                    contact TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS browser_sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    csrf_token TEXT NOT NULL, expires_at REAL NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, device_id TEXT NOT NULL,
                    boot_id TEXT NOT NULL, seq INTEGER NOT NULL,
                    owner_id TEXT NOT NULL REFERENCES users(id),
                    peer_device_id TEXT NOT NULL,
                    peer_owner_id TEXT NOT NULL REFERENCES users(id),
                    peer_alias TEXT NOT NULL, own_alias TEXT NOT NULL,
                    session_id TEXT NOT NULL, start_utc TEXT, end_utc TEXT,
                    time_quality TEXT NOT NULL, duration_ms INTEGER NOT NULL,
                    rssi_median REAL NOT NULL, sample_count INTEGER NOT NULL,
                    source TEXT NOT NULL, canonical_json TEXT NOT NULL,
                    received_at REAL NOT NULL, hidden INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(device_id, boot_id, seq))""",
                """CREATE INDEX IF NOT EXISTS event_pair
                    ON events(session_id, device_id, peer_device_id, source)""",
                """CREATE TABLE IF NOT EXISTS contact_requests (
                    id INTEGER PRIMARY KEY, sender_id TEXT NOT NULL REFERENCES users(id),
                    recipient_id TEXT NOT NULL REFERENCES users(id),
                    encounter_id INTEGER NOT NULL REFERENCES events(id),
                    pair_low TEXT NOT NULL, pair_high TEXT NOT NULL,
                    sender_alias TEXT NOT NULL, recipient_alias TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(
                        status IN ('pending','accepted','rejected','revoked')),
                    created_at REAL NOT NULL)""",
                """CREATE UNIQUE INDEX IF NOT EXISTS active_relationship
                    ON contact_requests(pair_low, pair_high)
                    WHERE status IN ('pending', 'accepted')""",
            ]
            for statement in statements:
                db.execute(statement)
            for user in settings["users"]:
                old = db.execute(
                    "SELECT password_hash FROM users WHERE id=?", (user["id"],)
                ).fetchone()
                encoded = old["password_hash"] if old else None
                if not encoded or not _password_matches(user["password"], encoded):
                    encoded = _password_hash(user["password"])
                db.execute(
                    """INSERT INTO users VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash,
                       display_name=excluded.display_name, bio=excluded.bio,
                       contact=excluded.contact""",
                    (user["id"], encoded, user["display_name"],
                     user.get("bio", ""), user.get("contact", "")),
                )

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _connection(self, write=False):
        db = None
        try:
            db = self._connect()
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except sqlite3.Error as exc:
            if db is not None:
                db.rollback()
            raise StoreError("database operation failed") from exc
        except Exception:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def ingest_event(self, event, topic_device_id):
        canonical = validate_event(event, topic_device_id, self.settings)
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False)
        key = tuple(canonical[name] for name in ("device_id", "boot_id", "seq"))
        aliases = self.settings["sessions"][canonical["session_id"]]
        peer_id = aliases[canonical["peer_alias"]]
        owner = self.settings["devices"][canonical["device_id"]]["owner"]
        peer_owner = self.settings["devices"][peer_id]["owner"]
        own_alias = next(alias for alias, device in aliases.items()
                         if device == canonical["device_id"])
        with self._connection(write=True) as db:
            existing = db.execute(
                "SELECT canonical_json FROM events WHERE device_id=? AND boot_id=? AND seq=?",
                key,
            ).fetchone()
            status = "stored"
            if existing:
                if existing["canonical_json"] != payload:
                    raise EventConflictError("event key already contains different content")
                status = "duplicate"
            else:
                db.execute(
                    """INSERT INTO events (
                        device_id,boot_id,seq,owner_id,peer_device_id,peer_owner_id,
                        peer_alias,own_alias,session_id,start_utc,end_utc,time_quality,
                        duration_ms,rssi_median,sample_count,source,canonical_json,received_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (*key, owner, peer_id, peer_owner, canonical["peer_alias"], own_alias,
                     canonical["session_id"], canonical["start_utc"], canonical["end_utc"],
                     canonical["time_quality"], canonical["end_ms"] - canonical["start_ms"],
                     canonical["rssi_median"], canonical["sample_count"], canonical["source"],
                     payload, time.time()),
                )
        # Exiting the connection commits first; commit failures never return success.
        return {"schema_version": 1, "device_id": key[0], "boot_id": key[1],
                "seq": key[2], "status": status}

    def login(self, username, password):
        with self._connection(write=True) as db:
            user = db.execute("SELECT * FROM users WHERE id=?", (username,)).fetchone()
            encoded = user["password_hash"] if user else self._dummy_password
            valid = _password_matches(password, encoded)
            if not user or not valid:
                raise AuthenticationError("invalid username or password")
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute("DELETE FROM browser_sessions WHERE expires_at<=?", (time.time(),))
            db.execute("INSERT INTO browser_sessions VALUES (?, ?, ?, ?)",
                       (hashlib.sha256(token.encode()).hexdigest(), username,
                        csrf, time.time() + 86400))
        return token, {"user": {"id": username, "display_name": user["display_name"]},
                       "csrf_token": csrf}

    def get_session(self, token):
        if not token:
            raise AuthenticationError("login required")
        with self._connection() as db:
            row = db.execute(
                """SELECT s.user_id,s.csrf_token,u.display_name FROM browser_sessions s
                   JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.expires_at>?""",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        if row is None:
            raise AuthenticationError("login required")
        return {"user": {"id": row["user_id"], "display_name": row["display_name"]},
                "csrf_token": row["csrf_token"]}

    def logout(self, token):
        with self._connection(write=True) as db:
            db.execute("DELETE FROM browser_sessions WHERE token_hash=?",
                       (hashlib.sha256(token.encode()).hexdigest(),))

    @staticmethod
    def _profile(db, user_id):
        row = db.execute("SELECT display_name,bio,contact FROM users WHERE id=?",
                         (user_id,)).fetchone()
        return dict(row)

    @staticmethod
    def _relationship(db, user_id, peer_id):
        low, high = sorted((user_id, peer_id))
        return db.execute(
            """SELECT * FROM contact_requests WHERE pair_low=? AND pair_high=?
               ORDER BY CASE WHEN status IN ('pending','accepted') THEN 0 ELSE 1 END,
               id DESC LIMIT 1""",
            (low, high),
        ).fetchone()

    def list_encounters(self, user_id):
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE owner_id=? AND hidden=0 ORDER BY id DESC",
                (user_id,),
            ).fetchall()
            result = []
            for row in rows:
                mutual = False
                if row["time_quality"] == "synced":
                    mutual = db.execute(
                        """SELECT 1 FROM events WHERE session_id=? AND device_id=?
                           AND peer_device_id=? AND source=? AND time_quality='synced'
                           AND start_utc<=? AND end_utc>=? LIMIT 1""",
                        (row["session_id"], row["peer_device_id"], row["device_id"],
                         row["source"], row["end_utc"], row["start_utc"]),
                    ).fetchone() is not None
                relation = self._relationship(db, user_id, row["peer_owner_id"])
                item = {name: row[name] for name in (
                    "id", "peer_alias", "session_id", "start_utc", "end_utc",
                    "time_quality", "duration_ms", "rssi_median", "sample_count", "source")}
                item.update(mutual=mutual,
                            contact_status=relation["status"] if relation else "none",
                            request_id=relation["id"] if relation else None,
                            profile=self._profile(db, row["peer_owner_id"])
                            if relation and relation["status"] == "accepted" else None)
                result.append(item)
        return result

    @staticmethod
    def _owned_event(db, user_id, encounter_id):
        row = db.execute("SELECT * FROM events WHERE id=? AND owner_id=?",
                         (encounter_id, user_id)).fetchone()
        if row is None:
            raise NotFoundError("encounter not found")
        return row

    def hide_encounter(self, user_id, encounter_id):
        with self._connection(write=True) as db:
            self._owned_event(db, user_id, encounter_id)
            db.execute("UPDATE events SET hidden=1 WHERE id=?", (encounter_id,))

    def _request_view(self, db, row, user_id):
        outgoing = row["sender_id"] == user_id
        peer_id = row["recipient_id"] if outgoing else row["sender_id"]
        return {
            "id": row["id"], "direction": "outgoing" if outgoing else "incoming",
            "peer_alias": row["recipient_alias"] if outgoing else row["sender_alias"],
            "status": row["status"],
            "profile": self._profile(db, peer_id) if row["status"] == "accepted" else None,
        }

    def create_request(self, user_id, encounter_id):
        with self._connection(write=True) as db:
            event = self._owned_event(db, user_id, encounter_id)
            if event["hidden"]:
                raise NotFoundError("encounter not found")
            peer_id = event["peer_owner_id"]
            if peer_id == user_id:
                raise StateConflictError("cannot request a connection to yourself")
            row = self._relationship(db, user_id, peer_id)
            # Retries of the same action stay deterministic even after a decision.
            if row is None or (
                row["status"] not in ("pending", "accepted")
                and (row["sender_id"] != user_id or row["encounter_id"] != encounter_id)
            ):
                low, high = sorted((user_id, peer_id))
                cursor = db.execute(
                    """INSERT INTO contact_requests (
                        sender_id,recipient_id,encounter_id,pair_low,pair_high,
                        sender_alias,recipient_alias,status,created_at)
                        VALUES (?,?,?,?,?,?,?,'pending',?)""",
                    (user_id, peer_id, encounter_id, low, high,
                     event["own_alias"], event["peer_alias"], time.time()),
                )
                row = db.execute("SELECT * FROM contact_requests WHERE id=?",
                                 (cursor.lastrowid,)).fetchone()
            result = self._request_view(db, row, user_id)
        return result

    def list_requests(self, user_id):
        with self._connection() as db:
            rows = db.execute(
                """SELECT * FROM contact_requests WHERE sender_id=? OR recipient_id=?
                   ORDER BY id DESC""", (user_id, user_id)
            ).fetchall()
            return [self._request_view(db, row, user_id) for row in rows]

    @staticmethod
    def _participant_request(db, user_id, request_id):
        row = db.execute(
            """SELECT * FROM contact_requests WHERE id=?
               AND (sender_id=? OR recipient_id=?)""",
            (request_id, user_id, user_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("request not found")
        return row

    def decide_request(self, user_id, request_id, decision):
        if decision not in ("accepted", "rejected"):
            raise StateConflictError("invalid decision")
        with self._connection(write=True) as db:
            row = self._participant_request(db, user_id, request_id)
            if row["recipient_id"] != user_id:
                raise AccessDeniedError("only the recipient can decide")
            if row["status"] != decision:
                if row["status"] != "pending":
                    raise StateConflictError("request already has a different decision")
                db.execute("UPDATE contact_requests SET status=? WHERE id=?",
                           (decision, request_id))
                row = db.execute("SELECT * FROM contact_requests WHERE id=?",
                                 (request_id,)).fetchone()
            result = self._request_view(db, row, user_id)
        return result

    def revoke_connection(self, user_id, request_id):
        with self._connection(write=True) as db:
            row = self._participant_request(db, user_id, request_id)
            if row["status"] not in ("accepted", "revoked"):
                raise StateConflictError("only an accepted connection can be revoked")
            db.execute("UPDATE contact_requests SET status='revoked' WHERE id=?",
                       (request_id,))
