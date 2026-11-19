"""The local button is a fixed exercise, not a public device-ingestion API."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import threading

from fastapi.testclient import TestClient
import pytest

from bump import simulator
from bump.api import create_app
from bump.broker import BrokerThread
from bump.config import init_runtime
from bump.worker import Worker
from test_api import logged_in
from test_integration import free_port, login
from test_store import event, make_settings


PATH = "/api/demo/discovery"


@pytest.fixture
def app(tmp_path):
    return create_app(make_settings(tmp_path))


def test_status_is_private_and_measurements_are_not_invented(app):
    anon, alice = TestClient(app), logged_in(app, "alice")
    assert anon.get(PATH).status_code == 401
    assert anon.post(PATH).status_code == 401
    status = alice.get(PATH)
    assert status.headers["cache-control"] == "no-store"
    assert status.headers["X-Bump-Viewer"] == "alice"
    data = status.json()
    assert data["csrf_token"] == alice.headers["X-CSRF-Token"]
    assert data["available"] and data["mode"] == "simulation"
    assert data["hardware_telemetry"] == "not_integrated"
    assert data["device"] == {"alias": "A00001", "last_received_at": None, "last_source": None}

    # A peer's receipt, even for a real-tagged event, is never our receipt.
    app.state.store.ingest_event(event("badge-b", source="hardware"), "badge-b")
    assert alice.get(PATH).json()["device"] == data["device"]
    before = datetime.now(timezone.utc).timestamp() - 1
    app.state.store.ingest_event(event(), "badge-a")
    after = datetime.now(timezone.utc).timestamp() + 1
    receipt = alice.get(PATH).json()["device"]
    assert set(receipt) == {"alias", "last_received_at", "last_source"}
    assert receipt["last_source"] == "simulated"
    assert before <= datetime.fromisoformat(receipt["last_received_at"]).timestamp() <= after
    first = alice.get("/api/encounters").json()["items"][0]
    assert first["simulated"] and first["profile"] is None
    assert alice.delete(f"/api/encounters/{first['id']}").status_code == 200
    assert alice.get("/api/encounters").json() == {"items": []}
    assert alice.get(PATH).json()["device"] == receipt


def test_roles_tokens_and_fixed_inputs(app, monkeypatch):
    def unexpected(*args):
        pytest.fail("unapproved call reached the simulator")
    monkeypatch.setattr(simulator, "_run_demo", unexpected)
    alice, bob, charlie = [logged_in(app, n) for n in ("alice", "bob", "charlie")]
    missing = {"X-CSRF-Token": ""}
    wrong = {"X-CSRF-Token": bob.headers["X-CSRF-Token"]}
    for headers in (missing, wrong):
        assert alice.post(PATH, headers=headers).status_code == 403
    assert not charlie.get(PATH).json()["available"]
    assert charlie.get(PATH).json()["device"] is None
    assert charlie.post(PATH, json={}).status_code == 403
    for payload in ({"source": "hardware"}, {"owner": "bob"}, {"scenario": "offline"}, event()):
        assert alice.post(PATH, json=payload).status_code == 422
    assert alice.post(PATH + "?device=badge-b").status_code == 422
    for client, user in ((alice, "alice"), (bob, "bob"), (charlie, "charlie")):
        for path in ("/api/me", "/api/requests", "/api/encounters", PATH):
            assert client.get(path).headers["X-Bump-Viewer"] == user


def test_cross_client_single_flight_and_no_success_on_failure(app, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def wait_and_fail(settings, scenario):
        assert scenario == "normal"
        entered.set()
        assert release.wait(5)
        return {"ok": False}

    monkeypatch.setattr(simulator, "_run_demo", wait_and_fail)
    alice, bob = logged_in(app, "alice"), logged_in(app, "bob")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(alice.post, PATH)
        try:
            assert entered.wait(5)
            assert bob.post(PATH).status_code == 409
            with pytest.raises(simulator.DemoBusy):
                simulator.run_demo(app.state.store.settings)
        finally:
            release.set()
        assert pending.result().status_code == 503
    # A failed run releases the process-safe lease. No fake history is added.
    assert bob.post(PATH).status_code == 503
    assert alice.get("/api/encounters").json() == {"items": []}


def test_cannot_report_a_missing_row_or_unmatched_ack(app, monkeypatch):
    devices = []
    for device_id in ("badge-a", "badge-b"):
        data = event(device_id)
        key = [data[k] for k in ("device_id", "boot_id", "seq")]
        ack = dict(zip(("device_id", "boot_id", "seq"), key))
        ack["status"] = "stored"
        devices.append({"device_id": device_id, "event_key": key,
                        "queue_size": 0, "acks": [ack]})
    result = {"ok": True, "simulation_mode": True, "source": "simulated",
              "hardware_verified": False, "devices": devices}
    monkeypatch.setattr(simulator, "_run_demo", lambda *_: result)
    client = logged_in(app, "alice")
    assert client.post(PATH).status_code == 503  # Fixture has ACKs but no local row.
    for device_id in ("badge-a", "badge-b"):
        app.state.store.ingest_event(event(device_id), device_id)
    devices[1]["acks"][0]["seq"] = 2
    assert client.post(PATH).status_code == 503  # No forged success after a partial run.


def test_button_really_drives_loopback_tls_mqtt_sqlite_and_ack(tmp_path):
    settings = init_runtime(tmp_path / "private-runtime", mqtt_port=free_port())
    app = create_app(settings)
    worker = Worker(settings, store=app.state.store)
    with BrokerThread(settings), ExitStack() as sessions:
        try:
            assert worker.start()
            a, b, c = [login(sessions, app, settings, user) for user in ("alice", "bob", "charlie")]
            for actor, alias in ((a, "B00002"), (b, "A00001")):
                reply = actor.post(PATH, json={})
                assert reply.status_code == 200, reply.text
                data = reply.json()
                assert set(data) == {"mode", "simulated", "source", "created", "ack_status",
                                     "encounter_id", "peer_alias"}
                assert data["source"] == "simulated" and data["simulated"] and data["created"]
                assert data["ack_status"] == "stored" and data["peer_alias"] == alias
                own = actor.get("/api/encounters").json()["items"][0]
                assert own["id"] == data["encounter_id"] and own["peer_alias"] == alias
                assert own["mutual"] and own["simulated"] and own["profile"] is None
                assert actor.get(PATH).json()["device"]["last_source"] == "simulated"
            assert worker.stats["stored"] == 4
            assert len(b.get("/api/encounters").json()["items"]) == 2
            assert len(a.get("/api/encounters").json()["items"]) == 2
            assert c.get("/api/encounters").json() == {"items": []}
            assert c.get(PATH).json()["device"] is None
        finally:
            worker.stop()
