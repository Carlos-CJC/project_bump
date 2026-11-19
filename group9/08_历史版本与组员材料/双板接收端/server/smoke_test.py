"""Exercise the actual CLI, MQTT listener, database and browser HTTP routes with synthetic packets."""

from contextlib import ExitStack
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPCookieProcessor

import paho.mqtt.client as mqtt


HERE = Path(__file__).resolve().parent
A, B = "bump-aa1100000001", "bump-bb2200000002"
BOOT_A, BOOT_B = "a0a0a0a0", "b0b0b0b0"
SHORT_A, SHORT_B = "0aa110000001", "0bb220000002"


def select_ports():
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return first.getsockname()[1], second.getsockname()[1]


def command(*args, **kwargs):
    return subprocess.run([sys.executable, "-X", "utf8", str(HERE / "live_server.py"), *map(str, args)],
                          check=True, cwd=HERE, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, **kwargs)


def http(opener, url, path, *, method="GET", data=None, csrf=None, status=200):
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if csrf:
        headers["X-CSRF-Token"] = csrf
    request = Request(url + path, method=method, headers=headers,
                      data=json.dumps(data).encode() if data is not None else None)
    try:
        response = opener.open(request, timeout=3)
    except HTTPError as err:
        response = err
    with response:
        assert response.code == status, (method, path, status, response.code, response.read())
        return json.load(response)


def opener():
    return build_opener(HTTPCookieProcessor(CookieJar()))


def wait_ready(url, process):
    end = time.monotonic() + 15
    while time.monotonic() <= end:
        if process.poll() is not None:
            raise AssertionError(f"CLI server exited with {process.returncode}")
        try:
            assert http(opener(), url, "/api/health")["ready"]
            return
        except URLError:
            time.sleep(0.15)
    raise AssertionError("HTTP service not started")


def wait_state(get, accepts):
    end = time.monotonic() + 3
    while time.monotonic() <= end:
        item = get()
        if accepts(item):
            return item
        time.sleep(0.05)
    raise AssertionError("expected state did not appear")


def status_packet(dev, boot, short, state="online"):
    return {"schema": 1, "device_id": dev, "boot_id": boot, "fw": "smoke",
            "state": state, "uptime_s": 10, "queue_depth": 0, "events_dropped": 0,
            "self_short_id": short}


def encounters(dev, boot, short, peer):
    seq = "00000001"
    return {"schema": 1, "device_id": dev, "boot_id": boot, "self_short_id": short,
            "fw": "smoke", "batch_id": f"bt_{dev[5:]}_{boot}_{seq}_0001",
            "time_source": "uptime", "uptime_ms": 10000, "events": [{
                "event_id": f"ev_{dev[5:]}_{boot}_{seq}", "peer_short_id": peer,
                "rssi_avg": -62, "rssi_last": -63, "duration_s": 4, "samples": 3,
                "flags": 1, "occurred_uptime_ms": 9000,
            }]}


def ir(dev, boot, own, peer):
    return {"device_id": dev, "boot_id": boot, "self_code16": own, "peer_code16": peer,
            "uptime_ms": 12000, "event_id": f"ir_{dev[5:]}_{boot}_00000002"}


class Client:
    def __init__(self, conf, dev):
        self.dev = dev
        self.port = conf["mqtt"]["port"]
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=dev,
                                  protocol=mqtt.MQTTv311, reconnect_on_failure=False)
        self.client.username_pw_set(dev, conf["mqtt"]["accounts"][dev]["password"])
        self.messages = queue.Queue()
        self.connected = threading.Event()
        self.subscribed = threading.Event()
        self.grants = []
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = lambda c, u, msg: self.messages.put((msg.topic, json.loads(msg.payload)))

    def _on_connect(self, client, _data, _flags, reason, _props):
        if reason == 0:
            self.connected.set()
            # Test that only the exact own ACK subscription is accepted.
            other = A if self.dev == B else B
            client.subscribe([(f"bump/v1/device/{self.dev}/ack", 1),
                              (f"bump/v1/device/{other}/ack", 1)])

    def _on_subscribe(self, _client, _data, _mid, reasons, _props):
        self.grants = [r.value for r in reasons]
        self.subscribed.set()

    def __enter__(self):
        self.client.connect("127.0.0.1", self.port, 10)
        self.client.loop_start()
        if not self.subscribed.wait(5) or self.grants != [1, 128]:
            self.__exit__()
            raise AssertionError(f"authenticated ACK subscription failed or ACL leaked: {self.grants}")
        return self

    def __exit__(self, *_):
        self.client.disconnect()
        self.client.loop_stop()

    def publish(self, kind, body, *, retained=False, target=None):
        d = self.dev if target is None else target
        sent = self.client.publish(f"bump/v1/device/{d}/{kind}", json.dumps(body),
                                   qos=1, retain=retained)
        sent.wait_for_publish(3)
        assert sent.is_published()

    def ack(self, event, status):
        topic, body = self.messages.get(timeout=5)
        assert topic == f"bump/v1/device/{self.dev}/ack"
        assert body == {"event_id": event, "status": status}

    def no_ack(self):
        try:
            msg = self.messages.get(timeout=0.3)
        except queue.Empty:
            return
        raise AssertionError(f"unauthorized ACK received: {msg}")


def run_tests(runtime, url, conf):
    sessions = {}
    http(opener(), url, "/api/encounters", status=401)
    for user in conf["users"]:
        who = user["id"]
        session = opener()
        auth = http(session, url, "/api/login", method="POST",
                    data={"username": who, "password": user["password"]})
        sessions[who] = (session, auth["csrf_token"])
    alice, ac = sessions["alice"]
    bob, bc = sessions["bob"]
    charlie, cc = sessions["charlie"]
    timeline = lambda who: http(who, url, "/api/encounters")["items"]
    requests = lambda who: http(who, url, "/api/requests")["items"]
    station = lambda who: http(who, url, "/api/device")["device"]
    assert station(charlie) is None and timeline(charlie) == requests(charlie) == []
    with ExitStack() as stack:
        client_a = stack.enter_context(Client(conf, A))
        client_b = stack.enter_context(Client(conf, B))
        packet_a = encounters(A, BOOT_A, SHORT_A, SHORT_B)
        packet_b = encounters(B, BOOT_B, SHORT_B, SHORT_A)
        aid, bid = [p["events"][0]["event_id"] for p in (packet_a, packet_b)]
        client_a.publish("encounters", packet_b, target=B)
        client_a.publish("ack", {"event_id": bid, "status": "stored"}, target=B)
        client_b.no_ack()
        assert timeline(alice) == timeline(bob) == []
        client_a.publish("status", status_packet(A, BOOT_A, SHORT_A), retained=True)
        client_a.publish("encounters", packet_a)
        client_a.ack(aid, "stored")
        row = timeline(alice)[0]
        assert row["peer_alias"] == "身份待匹配" and not row["mutual"]
        assert row["source"] == "hardware" and row["transport"] == "mqtt"
        assert row["time_quality"] == "estimated" and row["end_utc"] < row["received_at"]
        assert row["profile"] is None and row["can_invite"] is False
        client_a.publish("encounters", packet_a)
        client_a.ack(aid, "duplicate")
        assert timeline(alice)[0]["event_count"] == 1
        packet_a["events"][0]["rssi_avg"] = -74
        client_a.publish("encounters", packet_a)
        client_a.no_ack()
        assert timeline(alice)[0]["rssi_median"] == -62
        client_b.publish("status", status_packet(B, BOOT_B, SHORT_B), retained=True)
        resolved = wait_state(lambda: timeline(alice)[0], lambda item: item["peer_alias"] == "挂件 B")
        assert not resolved["mutual"] and not resolved["can_invite"] and not resolved["profile"]
        http(alice, url, "/api/requests", method="POST", csrf=ac,
             data={"encounter_id": row["id"]}, status=403)
        client_b.publish("encounters", packet_b)
        client_b.ack(bid, "stored")
        assert timeline(alice)[0]["mutual"] and timeline(bob)[0]["mutual"]
        assert timeline(charlie) == []
        http(charlie, url, f'/api/encounters/{row["id"]}', method="DELETE", csrf=cc, status=404)
        send_a = ir(A, BOOT_A, 0xA30F, 0xC371)
        send_b = ir(B, BOOT_B, "c371", "a30f")
        client_a.publish("ir", send_a)
        client_a.ack(send_a["event_id"], "stored")
        assert requests(alice) == requests(bob) == []
        client_b.publish("ir", send_b)
        client_b.ack(send_b["event_id"], "stored")
        client_b.publish("ir", send_b)
        client_b.ack(send_b["event_id"], "duplicate")
        outgoing, incoming = requests(alice), requests(bob)
        assert len(outgoing) == len(incoming) == 1
        invite = incoming[0]
        assert invite["status"] == "pending" and invite["method"] == "ir"
        assert invite["profile"] is None and timeline(bob)[0]["profile"] is None
        assert outgoing[0]["direction"] == "outgoing" and invite["direction"] == "incoming"
        assert requests(charlie) == []
        accept = f'/api/requests/{invite["id"]}/accept'
        http(charlie, url, accept, method="POST", csrf=cc, status=404)
        http(alice, url, accept, method="POST", csrf=ac, status=403)
        http(bob, url, accept, method="POST", status=403)
        assert http(bob, url, accept, method="POST", csrf=bc)["item"]["status"] == "accepted"
        assert timeline(alice)[0]["profile"]["display_name"] == "Bob"
        assert timeline(bob)[0]["profile"]["display_name"] == "Alice"
        http(alice, url, f'/api/connections/{invite["id"]}', method="DELETE", csrf=ac)
        assert requests(bob)[0]["status"] == "revoked" and not timeline(alice)[0]["profile"]
        assert http(alice, url, "/api/requests", method="POST", csrf=ac,
                    data={"encounter_id": row["id"]})["item"]["method"] == "ble"
        client_a.publish("status", status_packet(A, BOOT_A, SHORT_A, "offline"), retained=True)
        wait_state(lambda: station(alice), lambda item: item["state"] == "offline")
        assert station(alice)["last_transport"] == "mqtt"
        audit = json.loads(command("inspect", "--runtime", runtime, capture_output=True).stdout)
        assert len(audit["encounters"]) == 2 and len(audit["ir"]) == 2
        assert all(x["matched"] for x in audit["ir"])
    assert timeline(charlie) == requests(charlie) == []
    with opener().open(url + "/", timeout=3) as root:
        assert "双板联调".encode() in root.read()


def with_server(log_path, runtime, url, action):
    with log_path.open("w+b") as log:
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(HERE / "live_server.py"), "serve",
             "--runtime", str(runtime), "--mqtt-host", "127.0.0.1"],
            stdout=log, stderr=log, cwd=HERE, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            wait_ready(url, process)
            action()
        except BaseException:
            log.seek(0)
            print(log.read().decode("utf-8", "replace"))
            raise
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def verify_reopened(url, config):
    who = next(user for user in config["users"] if user["id"] == "alice")
    alice = opener()
    http(alice, url, "/api/login", method="POST",
         data={"username": who["id"], "password": who["password"]})
    retained = http(alice, url, "/api/encounters")["items"]
    assert len(retained) == 1 and retained[0]["mutual"] and retained[0]["transport"] == "mqtt"
    statuses = {x["status"] for x in http(alice, url, "/api/requests")["items"]}
    assert statuses == {"revoked", "pending"}


def main():
    mqtt_port, web_port = select_ports()
    url = f"http://127.0.0.1:{web_port}"
    with TemporaryDirectory(prefix="test-runtime-", dir=HERE) as temp:
        runtime = Path(temp)
        command("init", "--runtime", runtime, "--device-a", A, "--device-b", B,
                "--mqtt-port", mqtt_port, "--web-port", web_port, capture_output=True)
        config = json.loads((runtime / "live-config.json").read_text(encoding="utf-8"))
        with_server(runtime / "service.log", runtime, url, lambda: run_tests(runtime, url, config))
        with_server(runtime / "reopened.log", runtime, url, lambda: verify_reopened(url, config))
    print("PASS: loopback MQTT/HTTP synthetic wire test, commit receipts, BLE ID backfill and mutuality, "
          "IR matching, consent, third-account isolation, and reopening the same runtime.")


if __name__ == "__main__":
    main()


