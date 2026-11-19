"""Loopback browser API. Device events enter only through the MQTT worker."""

import hmac
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .discovery import Discovery, DiscoveryUnavailable
from .store import (AccessDeniedError, AuthenticationError, NotFoundError,
                    StateConflictError, Store, StoreError)


# Localhost cookies are not scoped to TCP ports. Keep two demo editions independent.
COOKIE_NAME = "bump_v1lab_session"
ObjectId = Annotated[int, Path(ge=1, le=2**63 - 1)]


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    encounter_id: int = Field(ge=1, le=2**63 - 1)


class DiscoveryInput(BaseModel):
    """The browser can request only the fixed synthetic pair; no event inputs."""
    model_config = ConfigDict(extra="forbid", strict=True)


def create_app(settings):
    app = FastAPI(title="Bump local prototype", version="0.1.0",
                  docs_url=None, redoc_url=None, openapi_url=None)
    store = Store(settings)
    app.state.store = store
    discovery = Discovery(settings, store)

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def error_response(status):
        async def handler(request, exc):
            message = "storage temporarily unavailable" if status == 503 else str(exc)
            return JSONResponse(status_code=status, content={"detail": message})
        return handler

    for error, status in (
        (AuthenticationError, 401), (AccessDeniedError, 403), (NotFoundError, 404),
        (StateConflictError, 409), (StoreError, 503),
    ):
        app.add_exception_handler(error, error_response(status))

    def current_session(request: Request, response: Response):
        session = store.get_session(request.cookies.get(COOKIE_NAME))
        # Tabs share a cookie. Let the client discard a finished read made under
        # a different viewer rather than painting it under a stale account.
        response.headers["X-Bump-Viewer"] = session["user"]["id"]
        return session

    def mutation_session(
        session=Depends(current_session), csrf: str | None = Header(
            default=None, alias="X-CSRF-Token")
    ):
        if not csrf or not csrf.isascii() or not hmac.compare_digest(csrf, session["csrf_token"]):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        return session

    @app.get("/api/health")
    def health():
        return {"ready": True, "version": "0.1.0", "simulation_mode": True}

    @app.post("/api/login")
    def login(body: LoginInput, response: Response):
        token, session = store.login(body.username, body.password)
        response.set_cookie(COOKIE_NAME, token, max_age=86400, httponly=True,
                            samesite="strict", secure=False, path="/")
        return session

    @app.get("/api/me")
    def me(session=Depends(current_session)):
        return session

    @app.post("/api/logout")
    def logout(request: Request, response: Response, session=Depends(mutation_session)):
        store.logout(request.cookies[COOKIE_NAME])
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="strict")
        return {"ok": True}

    @app.get("/api/demo/discovery")
    def discovery_status(session=Depends(current_session)):
        return {**discovery.status(session["user"]["id"]), "csrf_token": session["csrf_token"]}

    @app.post("/api/demo/discovery")
    def start_discovery(request: Request, body: DiscoveryInput | None = None,
                        session=Depends(mutation_session)):
        if request.query_params:
            raise HTTPException(status_code=422, detail="this fixed simulation accepts no parameters")
        try:
            return discovery.run(session["user"]["id"])
        except DiscoveryUnavailable:
            # Some observations may already have committed. Do not replay this
            # mutating action or claim an ACK for a partially completed run.
            raise HTTPException(status_code=503, detail=(
                "simulation did not confirm both application ACKs and your visible record; "
                "refresh the timeline before trying again"
            )) from None

    @app.get("/api/encounters")
    def encounters(session=Depends(current_session)):
        return {"items": store.list_encounters(session["user"]["id"])}

    @app.delete("/api/encounters/{encounter_id}")
    def hide(encounter_id: ObjectId, session=Depends(mutation_session)):
        store.hide_encounter(session["user"]["id"], encounter_id)
        return {"ok": True}

    @app.post("/api/requests")
    def request_connection(body: ConnectionInput, session=Depends(mutation_session)):
        return {"item": store.create_request(session["user"]["id"], body.encounter_id)}

    @app.get("/api/requests")
    def requests(session=Depends(current_session)):
        return {"items": store.list_requests(session["user"]["id"])}

    @app.post("/api/requests/{request_id}/accept")
    def accept(request_id: ObjectId, session=Depends(mutation_session)):
        return {"item": store.decide_request(session["user"]["id"], request_id, "accepted")}

    @app.post("/api/requests/{request_id}/reject")
    def reject(request_id: ObjectId, session=Depends(mutation_session)):
        return {"item": store.decide_request(session["user"]["id"], request_id, "rejected")}

    @app.delete("/api/connections/{request_id}")
    def revoke(request_id: ObjectId, session=Depends(mutation_session)):
        store.revoke_connection(session["user"]["id"], request_id)
        return {"ok": True}

    from .v1_routes import install_v1_routes
    install_v1_routes(app, settings, current_session, mutation_session)

    web_dir = FilePath(__file__).resolve().parent.parent / "web"
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
