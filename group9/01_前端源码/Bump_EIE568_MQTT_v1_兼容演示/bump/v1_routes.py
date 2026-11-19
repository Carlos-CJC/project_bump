"""Authenticated, least-privilege browser facade for the v1 compatibility lab.

The demo does not implement the attachment's production bearer REST API. It
does exercise the MQTT wire format and durable data handling independently of
the original timeline and contact consent database.
"""
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from .v1_store import DEMO_IDS, V1Store


class RehearsalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def install_v1_routes(app, settings, current_session, mutation_session):
    db_path = Path(settings["db_path"]).with_name("bump-v1.sqlite3")
    store = V1Store(db_path)
    app.state.v1_store = store
    app.state.v1_transport = None

    @app.get("/api/v1/health")
    def public_v1_health(response: Response):
        # Test the separate durable lab database, not merely the HTTP process.
        # Never return configured paths, account identifiers or raw DB errors.
        try:
            with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro",
                                         uri=True, timeout=0.25)) as db:
                db_ok = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone() is not None
        except sqlite3.Error:
            db_ok = False
        transport = app.state.v1_transport
        health = transport.health() if transport is not None else {}
        mqtt_ok = health.get("connected") is True
        stamp = health.get("last_message_at")
        def iso(value):
            return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        if not (db_ok and mqtt_ok):
            response.status_code = 503
        return {"status": "ok" if db_ok and mqtt_ok else "degraded", "time": iso(time.time()),
                "db": "ok" if db_ok else "unavailable",
                "mqtt": {"connected": mqtt_ok, "last_message_at": iso(stamp) if stamp is not None else None},
                "version": "0.1.0"}

    @app.delete("/api/compat/v1/cards/{card_id}")
    def hide_card(card_id: str, session=Depends(mutation_session)):
        user = session["user"]["id"]
        if (user not in DEMO_IDS or not re.fullmatch("[0-9a-f]{32}", card_id)
                or not store.hide(user, card_id)):
            raise HTTPException(404, "protocol card not found")
        return {"ok": True}

    @app.get("/api/compat/v1/state")
    def state(session=Depends(current_session)):
        user = session["user"]["id"]
        panel = store.snapshot(user, now=time.time()) if user in DEMO_IDS else {"items": [], "device": None}
        return {"mode": "protocol_lab", "simulated": True, "source": "simulated",
                "available": user in DEMO_IDS and app.state.v1_transport is not None,
                "csrf_token": session["csrf_token"],
                "items": panel["items"], "device": panel["device"]}

    @app.post("/api/compat/v1/demo")
    def run(request: Request, body: RehearsalInput | None = None, session=Depends(mutation_session)):
        from .v1_transport import DemoBusy, TransportUnavailable
        if request.query_params:
            raise HTTPException(422, "the fixed rehearsal accepts no request parameters")
        user = session["user"]["id"]
        if user not in DEMO_IDS:
            raise HTTPException(403, "not a participant in the fixed v1 simulation")
        transport = app.state.v1_transport
        if transport is None:
            raise HTTPException(503, "local v1 receiver is not ready")
        try:
            result = transport.run_demo(user)
        except DemoBusy:
            raise HTTPException(409, "a v1 rehearsal is already running") from None
        except (TransportUnavailable, OSError, RuntimeError):
            # A partial run may have committed. Never auto-replay a POST.
            raise HTTPException(503, "durable receipt was not confirmed; refresh before retrying") from None
        if not isinstance(result, dict) or result.get("persisted") is not True:
            raise HTTPException(503, "durable receipt was not confirmed; refresh before retrying")
        # Even though run_demo is server-owned, never forward device/MAC IDs,
        # packet keys, MQTT/PUBACK diagnostics, or credentials to the browser.
        return {"mode": "protocol_lab", "simulated": True, "source": "simulated",
                "persisted": True, "application_ack": False}
