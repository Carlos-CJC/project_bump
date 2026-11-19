"""Read the three local demo accounts; optionally exercise consent on a real pending request."""
import argparse
from datetime import datetime, timezone
import http.cookiejar
import json
from pathlib import Path
import urllib.request


class Session:
    def __init__(self, port, user):
        self.base = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.token = None
        result = self.call("POST", "/api/login", {"username": user["id"], "password": user["password"]})
        self.token = result["csrf_token"]

    def call(self, method, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        headers = {"Content-Type": "application/json"}
        if self.token is not None:
            headers["X-CSRF-Token"] = self.token
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        with self.opener.open(req, timeout=8) as response:
            return json.load(response)


def snapshot(sessions):
    result = {}
    for name, session in sessions.items():
        result[name] = {}
        for kind in ("encounters", "requests"):
            rows = session.call("GET", "/api/" + kind)["items"]
            result[name][kind] = [{**{k: r.get(k) for k in (
                "id", "peer_alias", "source", "simulated", "mutual", "status", "direction", "method",
                "sample_count", "duration_s", "request_id", "contact_status") if k in r},
                "profile_visible": bool(r.get("profile"))} for r in rows]
    if any(result["charlie"].values()):
        raise RuntimeError("The unrelated account has received pair data.")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("runtime", type=Path)
    p.add_argument("evidence", type=Path)
    p.add_argument("--port", type=int, default=8875)
    p.add_argument("--exercise-pending", action="store_true")
    args = p.parse_args()
    settings = json.loads((args.runtime / "live-config.json").read_text(encoding="utf-8-sig"))
    accounts = {u["id"]: Session(args.port, u) for u in settings["users"]}
    before = snapshot(accounts)
    output = dict(at=datetime.now(timezone.utc).isoformat(), before=before)
    if args.exercise_pending:
        incoming = [(name, req) for name, d in before.items() for req in d["requests"]
                    if req["direction"] == "incoming" and req["status"] == "pending"]
        if len(incoming) != 1 or incoming[0][1]["profile_visible"]:
            raise RuntimeError("Expected one pending incoming request with its profile hidden.")
        name, req = incoming[0]
        key = req["id"]
        accounts[name].call("POST", f"/api/requests/{key}/accept")
        accepted = snapshot(accounts)
        for n in ("alice", "bob"):
            if not any(r["id"] == key and r["profile_visible"] for r in accepted[n]["requests"]):
                raise RuntimeError("Accepted profile missing for a participant.")
        accounts[name].call("DELETE", f"/api/connections/{key}")
        revoked = snapshot(accounts)
        if any(r["profile_visible"] for n in ("alice","bob") for r in revoked[n]["requests"] if r["id"] == key):
            raise RuntimeError("Profile still visible after revocation.")
        output.update(accepted=accepted, revoked=revoked, request=key)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    with args.evidence.open("x", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(json.dumps({n:{k:len(v) for k,v in data.items()} for n,data in before.items()}))


if __name__ == "__main__":
    main()
