"""Independent bounds for a physical badge's uptime in the host UTC clock."""

from math import isfinite

U32 = (1 << 32) - 1
HALF = 1 << 31
MAX_PROJECTION_AGE_MS = 900_000
MAX_RECEIVE_DELAY_S = 600
# Conservative guard for projection away from a measurement, not a measured
# oscillator specification. Wide or conflicting intervals are not corroboration.
DRIFT_S_PER_S = 0.001
MAX_PROJECTED_WIDTH_S = 2.0

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS clock_samples (
        device TEXT NOT NULL, boot TEXT NOT NULL, raw_uptime_ms INTEGER NOT NULL,
        utc_low REAL NOT NULL, utc_high REAL NOT NULL, source TEXT NOT NULL,
        PRIMARY KEY(device,boot,raw_uptime_ms,utc_low,utc_high,source),
        FOREIGN KEY(device,boot) REFERENCES bindings(device,boot)
    )""",
    """CREATE TABLE IF NOT EXISTS clock_encounters (
        event_id TEXT PRIMARY KEY REFERENCES observations(event_id),
        occurred_uptime_ms INTEGER NOT NULL, sent_uptime_ms INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS clock_projection (
        event_id TEXT PRIMARY KEY REFERENCES observations(event_id),
        shift_s REAL NOT NULL, uncertainty_s REAL NOT NULL
    )""",
    """CREATE VIEW IF NOT EXISTS clock_resolved AS
        SELECT o.observation_id,o.event_id,o.event_hash,o.card_key,o.device,o.boot,o.peer_short,
        o.start_at+COALESCE(c.shift_s,0) AS start_at,
        o.seen_at+COALESCE(c.shift_s,0) AS seen_at,
        o.duration_s,o.samples,o.rssi_avg,o.received_at,o.transport,
        CASE WHEN c.event_id IS NOT NULL THEN 'anchored' ELSE o.time_quality END AS time_quality,
        o.hidden,
        o.discovery_start_at+COALESCE(c.shift_s,0) AS discovery_start_at,
        o.discovery_end_at+COALESCE(c.shift_s,0) AS discovery_end_at,
        o.peer_device,c.uncertainty_s AS clock_uncertainty_s
        FROM resolved o LEFT JOIN clock_projection c ON c.event_id=o.event_id""",
)


def _uptime_ms(n):
    return type(n) is int and 0 <= n <= U32


def elapsed_ms(later, earlier):
    return ((later - earlier + HALF) & U32) - HALF


def status_bound(body, sent_connack, received_at):
    """Use a first live online status bracketed by a server-sent CONNACK."""
    uptime = body["uptime_s"] * 1000
    if not _uptime_ms(uptime) or not 0 <= received_at - sent_connack <= 1.0:
        return None
    # uptime_s truncates milliseconds. The timestamp for the start of that
    # uptime second is up to one second earlier than creation of the status.
    fractional_s = min(0.999, (U32 - uptime) / 1000)
    return uptime, sent_connack - fractional_s, received_at


def add_sample(db, device, boot, short, uptime, lower, upper, source):
    if (source not in ("mqtt_connack", "serial_status") or not _uptime_ms(uptime)
            or not all(type(v) in (int, float) and isfinite(v) for v in (lower, upper))
            or not 0 < lower <= upper <= lower + MAX_PROJECTED_WIDTH_S):
        return False
    identity = db.execute("SELECT short FROM bindings WHERE device=? AND boot=?", (device, boot)).fetchone()
    if identity is None or identity["short"] != short:
        return False
    return bool(db.execute("INSERT OR IGNORE INTO clock_samples VALUES(?,?,?,?,?,?)",
                           (device, boot, uptime, lower, upper, source)).rowcount)


def add_event(db, event_id, occurred, sent):
    if not _uptime_ms(occurred) or not _uptime_ms(sent) or elapsed_ms(sent, occurred) < 0:
        return False
    return bool(db.execute("INSERT OR IGNORE INTO clock_encounters VALUES(?,?,?)",
                           (event_id, occurred, sent)).rowcount)


def _projection(event, samples):
    age = elapsed_ms(event["sent_uptime_ms"], event["occurred_uptime_ms"]) / 1000
    proposals = []
    for sample in samples:
        distance = elapsed_ms(event["occurred_uptime_ms"], sample["raw_uptime_ms"])
        if abs(distance) > MAX_PROJECTION_AGE_MS:
            continue
        delta = distance / 1000
        pad = abs(delta) * DRIFT_S_PER_S
        earliest = sample["utc_low"] + delta - pad
        latest = sample["utc_high"] + delta + pad
        # This also rejects a modulo match with a different 49.7-day cycle.
        if (earliest + age > event["received_at"] + 1.0
                or latest + age < event["received_at"] - MAX_RECEIVE_DELAY_S):
            continue
        proposals.append((earliest, latest))
    if not proposals:
        return None
    lower = max(x[0] for x in proposals)
    upper = min(x[1] for x in proposals)
    if not 0 <= upper - lower <= MAX_PROJECTED_WIDTH_S:
        return None
    return (lower + upper) / 2 - event["seen_at"], (upper - lower) / 2


def refresh(db, device, boot):
    samples = db.execute("SELECT * FROM clock_samples WHERE device=? AND boot=?", (device, boot)).fetchall()
    rows = db.execute("""SELECT o.*, w.occurred_uptime_ms, w.sent_uptime_ms
                        FROM observations o JOIN clock_encounters w ON w.event_id=o.event_id
                        WHERE o.device=? AND o.boot=? AND o.time_quality='estimated'
                          AND o.transport='mqtt'""", (device, boot))
    changed = 0
    for event in rows:
        found = _projection(event, samples)
        if found is None:
            db.execute("DELETE FROM clock_projection WHERE event_id=?", (event["event_id"],))
            continue
        db.execute("""INSERT INTO clock_projection VALUES(?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET shift_s=excluded.shift_s,
                    uncertainty_s=excluded.uncertainty_s""", (event["event_id"], *found))
        changed += 1
    return changed

