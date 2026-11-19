"""HTTP scope for the independent v1 rehearsal. No raw device data API."""
import re

import pytest
from fastapi.testclient import TestClient

from bump.api import create_app
from test_api import logged_in
from test_store import make_settings


STATE = "/api/compat/v1/state"
DEMO = "/api/compat/v1/demo"


@pytest.fixture
def app(tmp_path):
    return create_app(make_settings(tmp_path))


class MockTransport:
    def __init__(self, result=None):
        self.calls = []
        self.result = {"persisted": True, "private_diagnostic": "hidden"} if result is None else result

    def health(self):
        return {"connected": True, "last_message_at": 1700000000}

    def run_demo(self, user):
        self.calls.append(user)
        return self.result


def test_health_is_generic_not_a_fake_live_claim(app):
    client = TestClient(app)
    before = client.get("/api/v1/health")
    assert before.status_code == 503 and before.json()["db"] == "ok"
    assert before.json()["mqtt"] == {"connected": False, "last_message_at": None}
    app.state.v1_transport = MockTransport()
    after = client.get("/api/v1/health")
    assert after.status_code == 200 and after.json()["status"] == "ok"
    assert after.json()["mqtt"]["last_message_at"] == "2023-11-14T22:13:20Z"
    assert not re.search(r"password|csrf|runtime_dir|bump-[a-f0-9]{12}", after.text)


def test_cannot_read_or_act_without_session_and_own_csrf(app):
    assert TestClient(app).get(STATE).status_code == 401
    assert TestClient(app).post(DEMO).status_code == 401
    alice, bob, charlie = [logged_in(app, x) for x in ("alice", "bob", "charlie")]
    app.state.v1_transport = MockTransport()
    state = alice.get(STATE)
    assert state.headers["x-bump-viewer"] == "alice"
    assert state.json()["available"] and state.json()["simulated"]
    assert state.json()["csrf_token"] == alice.headers["X-CSRF-Token"]
    assert alice.post(DEMO, headers={"X-CSRF-Token": ""}).status_code == 403
    assert alice.post(DEMO, headers={"X-CSRF-Token": bob.headers["X-CSRF-Token"]}).status_code == 403
    assert charlie.get(STATE).json()["device"] is None
    assert charlie.get(STATE).json()["items"] == [] and not charlie.get(STATE).json()["available"]
    assert charlie.post(DEMO).status_code == 403
    assert not app.state.v1_transport.calls


def test_response_minimizes_data_and_cannot_choose_device_or_raw_event(app):
    a = logged_in(app, "alice")
    app.state.v1_transport = MockTransport()
    assert a.post(DEMO + "?device=bob").status_code == 422
    assert a.post(DEMO, json={"source": "hardware"}).status_code == 422
    assert a.post(DEMO, json={"device_id": "bump-b20000000002", "events": []}).status_code == 422
    assert not app.state.v1_transport.calls
    response = a.post(DEMO, json={})
    assert response.status_code == 200
    assert response.headers["X-Bump-Viewer"] == "alice"
    assert set(response.json()) == {"mode","simulated","source","persisted","application_ack"}
    assert response.json()["persisted"] is True and response.json()["application_ack"] is False
    assert "private_diagnostic" not in response.text
    assert app.state.v1_transport.calls == ["alice"]
    assert a.get("/api/encounters").json() == {"items": []}
    assert a.get("/api/requests").json() == {"items": []}


def test_unavailable_or_incomplete_is_not_success(app):
    a = logged_in(app, "alice")
    assert not a.get(STATE).json()["available"]
    assert a.post(DEMO).status_code == 503
    app.state.v1_transport = MockTransport(result={"persisted": False})
    assert a.post(DEMO).status_code == 503


def test_hidden_card_uses_owner_and_token_and_does_not_report_other_data(app):
    a, b = logged_in(app, "alice"), logged_in(app, "bob")
    assert a.delete("/api/compat/v1/cards/invalid").status_code == 404
    assert a.delete("/api/compat/v1/cards/" + "a"*32).status_code == 404
    assert b.delete("/api/compat/v1/cards/" + "a"*32, headers={"X-CSRF-Token": a.headers["X-CSRF-Token"]}).status_code == 403
