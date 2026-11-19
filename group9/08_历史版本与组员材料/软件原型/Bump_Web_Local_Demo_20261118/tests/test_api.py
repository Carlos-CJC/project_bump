import json
import time

from fastapi.testclient import TestClient
import pytest

from bump.api import COOKIE_NAME, create_app
from test_store import event, make_settings


@pytest.fixture
def app(tmp_path):
    return create_app(make_settings(tmp_path))


def logged_in(app, user):
    client = TestClient(app)
    response = client.post("/api/login", json={
        "username": user, "password": f"{user}-test-password"})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


def seed_pair(app):
    app.state.store.ingest_event(event(), "badge-a")
    app.state.store.ingest_event(event("badge-b"), "badge-b")


def no_private_peer_data(payload, peer):
    text = json.dumps(payload)
    assert f"{peer}@example.invalid" not in text
    assert f"{peer} private bio" not in text
    assert f'"{peer}"' not in text
    assert peer.title() not in text


def test_cookie_csrf_auth_logout_and_generic_health(app):
    client = TestClient(app)
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/encounters").status_code == 401
    health = client.get("/api/health")
    assert health.json()["simulation_mode"] is True
    assert "password" not in health.text and "alice" not in health.text
    assert health.headers["Cache-Control"] == "no-store"
    assert client.post("/api/login", json={
        "username": "alice", "password": "wrong"}).status_code == 401
    login = client.post("/api/login", json={
        "username": "alice", "password": "alice-test-password"})
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    csrf = login.json()["csrf_token"]
    token = client.cookies[COOKIE_NAME]
    assert client.get("/api/me").json() == login.json()
    assert client.post("/api/logout").status_code == 403
    assert client.post("/api/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/logout", headers={"X-CSRF-Token": csrf}).json() == {"ok": True}
    assert client.get("/api/me").status_code == 401
    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/api/me").status_code == 401


def test_full_authorization_flow_profiles_hidden_until_recipient_accepts(app):
    seed_pair(app)
    alice, bob, charlie = [logged_in(app, user) for user in ("alice", "bob", "charlie")]
    alice_items = alice.get("/api/encounters").json()["items"]
    alice_id = alice_items[0]["id"]
    assert alice_items[0]["mutual"] is True
    assert alice_items[0]["profile"] is None
    no_private_peer_data(alice_items, "bob")
    request = alice.post("/api/requests", json={"encounter_id": alice_id}).json()["item"]
    request_id = request["id"]
    assert request["status"] == "pending"
    assert request["direction"] == "outgoing"
    assert request["profile"] is None
    assert alice.post("/api/requests", json={"encounter_id": alice_id}).json()["item"] == request
    bob_id = bob.get("/api/encounters").json()["items"][0]["id"]
    assert bob.post("/api/requests", json={"encounter_id": bob_id}).json()["item"]["id"] == request_id
    no_private_peer_data(alice.get("/api/requests").json(), "bob")
    no_private_peer_data(bob.get("/api/requests").json(), "alice")
    assert alice.post(f"/api/requests/{request_id}/accept").status_code == 403
    assert charlie.get("/api/encounters").json() == {"items": []}
    assert charlie.get("/api/requests").json() == {"items": []}
    assert charlie.post(f"/api/requests/{request_id}/accept").status_code == 404
    assert charlie.post(f"/api/requests/{request_id}/reject").status_code == 404
    assert charlie.post("/api/requests", json={"encounter_id": alice_id}).status_code == 404
    assert charlie.delete(f"/api/encounters/{alice_id}").status_code == 404
    assert charlie.delete(f"/api/connections/{request_id}").status_code == 404
    accepted = bob.post(f"/api/requests/{request_id}/accept")
    assert accepted.status_code == 200
    assert accepted.json()["item"]["profile"]["contact"] == "alice@example.invalid"
    assert bob.post(f"/api/requests/{request_id}/accept").json() == accepted.json()
    assert bob.post(f"/api/requests/{request_id}/reject").status_code == 409
    assert alice.get("/api/encounters").json()["items"][0]["profile"]["contact"] == "bob@example.invalid"
    assert alice.delete(f"/api/connections/{request_id}").status_code == 200
    assert bob.delete(f"/api/connections/{request_id}").status_code == 200
    assert bob.post(f"/api/requests/{request_id}/accept").status_code == 409
    for client, peer in ((alice, "bob"), (bob, "alice")):
        no_private_peer_data(client.get("/api/encounters").json(), peer)
        no_private_peer_data(client.get("/api/requests").json(), peer)
        assert client.get("/api/encounters").json()["items"][0]["contact_status"] == "revoked"


def test_reject_is_idempotent_and_cannot_be_changed_to_accept(app):
    seed_pair(app)
    alice, bob = logged_in(app, "alice"), logged_in(app, "bob")
    own_id = alice.get("/api/encounters").json()["items"][0]["id"]
    request_id = alice.post("/api/requests", json={"encounter_id": own_id}).json()["item"]["id"]
    rejected = bob.post(f"/api/requests/{request_id}/reject")
    assert rejected.status_code == 200
    assert bob.post(f"/api/requests/{request_id}/reject").json() == rejected.json()
    assert bob.post(f"/api/requests/{request_id}/accept").status_code == 409
    assert alice.delete(f"/api/connections/{request_id}").status_code == 409
    assert alice.post("/api/requests", json={"encounter_id": own_id}).json()["item"]["id"] == request_id
    no_private_peer_data(alice.get("/api/encounters").json(), "bob")


def test_delete_hides_only_own_entry_and_duplicate_cannot_resurrect(app):
    seed_pair(app)
    alice, bob = logged_in(app, "alice"), logged_in(app, "bob")
    own_id = alice.get("/api/encounters").json()["items"][0]["id"]
    assert bob.delete(f"/api/encounters/{own_id}").status_code == 404
    assert alice.delete(f"/api/encounters/{own_id}").status_code == 200
    assert app.state.store.ingest_event(event(), "badge-a")["status"] == "duplicate"
    assert alice.get("/api/encounters").json() == {"items": []}
    assert len(bob.get("/api/encounters").json()["items"]) == 1
    assert alice.post("/api/requests", json={"encounter_id": own_id}).status_code == 404


def test_mutations_require_session_specific_csrf(app):
    seed_pair(app)
    alice, bob = logged_in(app, "alice"), logged_in(app, "bob")
    own_id = alice.get("/api/encounters").json()["items"][0]["id"]
    request_id = alice.post("/api/requests", json={"encounter_id": own_id}).json()["item"]["id"]
    wrong_token = {"X-CSRF-Token": alice.headers["X-CSRF-Token"]}
    for path in (f"/api/requests/{request_id}/accept", f"/api/requests/{request_id}/reject"):
        assert bob.post(path, headers=wrong_token).status_code == 403
    assert bob.delete(f"/api/connections/{request_id}", headers=wrong_token).status_code == 403
    assert bob.delete(f"/api/encounters/{own_id}", headers=wrong_token).status_code == 403
    assert bob.post("/api/requests", headers=wrong_token,
                    json={"encounter_id": own_id}).status_code == 403


def test_expired_session_fails_and_api_input_is_strict(app):
    alice = logged_in(app, "alice")
    assert alice.post("/api/requests", json={"encounter_id": True}).status_code == 422
    assert alice.post("/api/requests", json={"encounter_id": 1, "sender": "bob"}).status_code == 422
    assert alice.delete(f"/api/encounters/{2**100}").status_code == 422
    with app.state.store._connection(write=True) as db:
        db.execute("UPDATE browser_sessions SET expires_at=?", (time.time() - 1,))
    assert alice.get("/api/me").status_code == 401
    assert alice.post("/api/logout").status_code == 401


def test_non_ascii_csrf_is_rejected_without_server_error(app):
    client = logged_in(app, "alice")
    response = client.post("/api/logout", headers=[(b"X-CSRF-Token", b"\xff")])
    assert response.status_code == 403
    assert client.get("/api/me").status_code == 200


def test_no_public_device_injection_endpoint(app):
    client = logged_in(app, "alice")
    for path in ("/api/events", "/api/ingest", "/api/devices/events"):
        assert client.post(path, json=event()).status_code in (404, 405)
