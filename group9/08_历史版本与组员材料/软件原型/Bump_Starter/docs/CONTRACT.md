# Bump v0.1 shared implementation contract

This is a local, hardware-free prototype. All supplied device scenarios use
`source: "simulated"`. No BLE or IMU performance claims follow from these tests.

## Module ownership

- `bump/protocol.py`, `bump/store.py`, `bump/api.py`,
  `tests/test_store.py`, `tests/test_api.py`: backend agent.
- `bump/device.py`, `bump/worker.py`, `bump/simulator.py`,
  `tests/test_device.py`: simulator agent.
- `firmware/**`, `tests/test_firmware*`: firmware agent.
- `bump/config.py`, `bump/broker.py`, `bump/__main__.py`, `web/**`,
  `tests/test_integration.py`, launch scripts, README, packaging and handoff:
  coordinator.

## Runtime settings

`bump.config.init_runtime(path, web_port=8765, mqtt_port=18883)` creates
local runtime files, random demo passwords and a private development CA.
It returns the same dict as `load_settings(path)`. Paths below are absolute
after loading; on-disk paths are reconstructed when runtime is loaded.

```
{
  "runtime_dir": "...",
  "db_path": ".../bump.sqlite3",
  "web_port": 8765,
  "mqtt": {
    "host": "localhost", "port": 18883,
    "ca_file": ".../ca.pem", "cert_file": ".../server.pem",
    "key_file": ".../server.key",
    "accounts": {
      "_worker": {"password": "...", "client_id": "bump-worker"},
      "badge-a": {"password": "...", "client_id": "badge-a"},
      "badge-b": {"password": "...", "client_id": "badge-b"}
    }
  },
  "users": [
    {"id": "alice", "password": "...", "display_name": "演示用户 A",
     "bio": "...", "contact": "alice@example.invalid"},
    {"id": "bob", ...}, {"id": "charlie", ...}
  ],
  "devices": {"badge-a": {"owner": "alice", "alias": "A00001"},
              "badge-b": {"owner": "bob", "alias": "B00002"}},
  "sessions": {"demo-001": {"A00001": "badge-a", "B00002": "badge-b"}}
}
```

Only localhost listeners are supported in v0.1. No external hosting is needed.
MQTT uses TLS, per-device credentials and directional topic ACL.
HTTP is loopback only; browser sessions use HttpOnly/SameSite=Strict cookies
and a server-issued CSRF token for mutations.

## Event

JSON object; reject unknown fields. `schema_version` is integer 1.
`device_id` must equal the ID encoded in the authenticated topic.

```
{
  "schema_version": 1,
  "device_id": "badge-a",
  "boot_id": "boot-unique-per-start",
  "seq": 1,
  "session_id": "demo-001",
  "peer_alias": "B00002",
  "start_ms": 1000,
  "end_ms": 7000,
  "start_utc": "2026-10-23T08:00:00Z",
  "end_utc": "2026-10-23T08:00:06Z",
  "time_quality": "synced",
  "rssi_median": -55,
  "sample_count": 12,
  "source": "simulated"
}
```

- Unique key `(device_id, boot_id, seq)`, seq >= 1 and never reset per window.
- `source` is `simulated` or `hardware`; never match across sources.
- `time_quality` is `synced` or `unknown`. UTC values must both be null if
  unknown; otherwise both timezone-aware, ordered and duration consistent with
  end_ms - start_ms (allow <= 1000 ms difference).
- Milliseconds >= 0, end >= start; sample_count > 0; RSSI -127..20.
- session and both devices must exist; peer must differ from observer.
- Same key/same canonical content -> duplicate; different content -> conflict.
  Canonical timestamps normalize equivalent UTC representations.
- Store raw events durably before success ACK. A failed commit must not ACK.
- Same session + reversed device pair + same source + trusted overlapping UTC
  intervals gives mutual encounter. Keep both raw observations.
- Deleting one's timeline entry is a soft hide; deduplication evidence remains
  and a retransmission must not resurrect it.

## MQTT

- Publish `bump/v1/device/{device_id}/events`
- Subscribe `bump/v1/device/{device_id}/ack`
- Both retain=false, QoS 1; subscribe and wait for SUBACK before upload.
- The development broker only supports clean_session=true; authenticate a
  fresh CONNECT before reading, replacing or modifying any existing session.
- Worker may subscribe events wildcard and publish ACK; devices cannot publish
  ACK or subscribe each other's data. Credentials and client IDs are bound.
- ACK:
  `{"schema_version":1,"device_id":"badge-a","boot_id":"...","seq":1,
    "status":"stored"}`. `duplicate` also permits deleting that queue item.
  `rejected` with reason does not. Full key must match.
- Simulator queue is RAM only and finite. It persists through network/worker
  outages within the same process, not process restart or power loss.
- Local states: IDLE, DISCOVERY, SYNC, PAUSED. Discovery ends at 60 seconds.
  Pause stops discovery immediately and still permits backlog retry.

## Python module interfaces

- `Store(settings)`: initialize SQLite + demo identities/mapping. Connections
  are short-lived and safe for worker and API threads.
- `Store.ingest_event(event: dict, topic_device_id: str) -> dict`: validate,
  commit and return full ACK; throw on invalid/conflict/store failure so the
  worker can reject or leave unacknowledged. Do not expose a public ingest API.
- `create_app(settings) -> FastAPI`: serve API and `web/` assets.
- `Worker(settings, store=None, drop_ack_count=0, fail_store_count=0)`:
  start(), stop(), health boolean/event. Fault parameters are Python/test hooks,
  never public API controls.
- `DeviceSimulator(settings, device_id, capacity=128, drop_ack_count=0)`:
  connect(), disconnect(), close(), enqueue(dict)->bool, sync(timeout=...)->bool.
  Queue contents/size and overflow counter inspectable by tests.
- Broker coordinator exposes `async serve_broker(settings)` for CLI, and a
  start/stop helper if needed by integration tests.

## Web API response contract

Mutation requests are JSON with `X-CSRF-Token` obtained at login/me.
Protected failures use HTTP 401, object authorization 403/404, conflicts 409.

- `POST /api/login {"username":"alice","password":"..."}` ->
  `{"user":{"id":"alice","display_name":"演示用户 A"},"csrf_token":"..."}` and cookie.
- `GET /api/me` -> same response, 401 if logged out.
- `POST /api/logout` -> `{"ok":true}`, revoke session and expire cookie.
- `GET /api/encounters` -> `{"items":[...]}`
  Each item: `id, peer_alias, session_id, start_utc, end_utc, time_quality,
  duration_ms, rssi_median, sample_count, source, mutual, contact_status,
  request_id, profile`. `profile` is null before accepted; otherwise
  `{display_name,bio,contact}`. Do not include peer username or private profile
  anywhere in unauthorized JSON.
- `DELETE /api/encounters/{id}` -> own observation only; `{"ok":true}`.
- `POST /api/requests {"encounter_id":id}` -> `{"item":request}`; require owned
  observation; one active relationship per user pair, deterministic repeated clicks.
- `GET /api/requests` -> `{"items":[request,...]}`; participant only.
  Request fields: `id, direction:incoming|outgoing, peer_alias, status,
  profile` (same privacy rule).
- `POST /api/requests/{id}/accept` and `/reject` -> `{"item":request}`.
  Only recipient can decide, repeated identical action idempotent.
- `DELETE /api/connections/{request_id}` -> participant may revoke an accepted
  connection; status becomes `revoked`, profile hidden again.
- `GET /api/health` -> generic readiness with version and simulation_mode,
  no credentials or account information.

Record design changes and test boundaries in the shared handoff.

## Firmware advertising packet

Project UUID: `965b0120-8aaa-4e72-9a37-6e208731ba01`.
29 bytes in total, with flags followed by 128-bit Service Data:
`02 01 06 19 21 <16-byte UUID little endian> 01 01 <6 ASCII alias bytes>`.
The two `01` bytes mean protocol version 1 and DISCOVERY state.
Aliases contain six uppercase ASCII letters/digits. Scan raw Service Data;
UUID-filtered scans do not necessarily include this AD type (`0x21`).
Construction/parsing and Nano compilation can be checked without hardware.
Radio coexistence, actual reception rate, range, battery use and IMU triggering
still require two real devices.
