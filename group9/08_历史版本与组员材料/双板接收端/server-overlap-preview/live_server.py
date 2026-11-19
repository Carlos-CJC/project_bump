"""Independent Bump receiver and loopback Web application for two provisioned badges."""

import argparse
import asyncio
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time

from amqtt.broker import Broker
from amqtt.contexts import Action
from amqtt.plugins.base import BaseAuthPlugin, BaseTopicPlugin
from uvicorn import Config, Server

from bump.api import create_app
from bump.broker import AuthenticatedBroker
from bump.live_store import LiveStore, code16, iso, parse_ir
from bump.store import StoreError
from bump.v1_wire import MAX_PAYLOAD_BYTES, parse_message


LOG = logging.getLogger("bump.live")
CONFIG_FILE = "live-config.json"
DB_FILE = "live.sqlite3"
TOPIC_BASE = "bump/v1/device"
UPLINK = ("status", "encounters", "ir")
DEVICE_ID = re.compile(r"bump-[0-9a-f]{12}")
PRIVATE_NETS = tuple(map(IPv4Network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


def uplink_topic(device, kind):
    return f"{TOPIC_BASE}/{device}/{kind}"


def ingest(store, topic, payload, device, stamp, transport):
    if device not in store.by_device or not topic.startswith(f"{TOPIC_BASE}/{device}/"):
        raise ValueError("unprovisioned source or inconsistent topic")
    kind = topic.rsplit("/", 1)[-1]
    if kind == "ir":
        return store.ingest_ir(device, parse_ir(device, payload), stamp, transport)
    parsed = parse_message(topic, payload, observed_at=stamp, authenticated_device_id=device)
    parsed["transport"] = transport
    return store.ingest(parsed, received_at=stamp)["receipts"]


class BadgeAuth(BaseAuthPlugin):
    @dataclass
    class Config:
        accounts: dict[str, dict[str, str]] = field(default_factory=dict)

    async def authenticate(self, *, session):
        account = self.config.accounts.get(session.username)
        if not (account and session.clean_session and session.client_id == account["client_id"]
                and isinstance(session.password, str) and session.password.isascii()
                and secrets.compare_digest(session.password, account["password"])):
            return False
        if not session.will_flag:
            return True
        device = session.username
        if (session.will_topic != uplink_topic(device, "status")
                or session.will_qos != 1 or not session.will_retain):
            return False
        try:
            will = parse_message(session.will_topic, bytes(session.will_message or b""),
                                 observed_at=time.time(), retained=True,
                                 authenticated_device_id=device)
        except ValueError:
            return False
        return will["body"]["state"] == "offline"


class BadgeTopics(BaseTopicPlugin):
    @dataclass
    class Config:
        devices: list[str] = field(default_factory=list)

    async def topic_filtering(self, *, session=None, topic=None, action=None):
        if session is None or session.username not in self.config.devices:
            return False
        if action == Action.PUBLISH:
            return any(topic == uplink_topic(session.username, kind) for kind in UPLINK)
        return (action in (Action.SUBSCRIBE, Action.RECEIVE)
                and topic == uplink_topic(session.username, "ack"))


class BoardBroker(AuthenticatedBroker):
    """Store verified uplinks directly before queueing any application ACK."""

    def __init__(self, store, settings, host):
        self.store = store
        self.last_message_at = None
        self.active = False
        mqtt_config = settings["mqtt"]
        # aMQTT imports plugins by module path. This module is aliased if run as a script.
        super().__init__({
            "listeners": {"default": {
                "type": "tcp", "bind": f"{host}:{mqtt_config['port']}", "ssl": False,
                "max_connections": 8,
            }},
            "plugins": {
                "live_server.BadgeAuth": {"accounts": mqtt_config["accounts"]},
                "live_server.BadgeTopics": {"devices": list(store.by_device)},
            },
        })

    async def _handle_message_delivery(self, session, handler, wait_deliver):
        message = wait_deliver.result()
        if message is None:
            return True
        topic = message.topic
        if not await self._topic_filtering(session, topic, Action.PUBLISH):
            return True
        kind = topic.rsplit("/", 1)[-1]
        retained = message.publish_packet and message.publish_packet.retain_flag
        if (message.qos != 1 or not message.data or len(message.data) > MAX_PAYLOAD_BYTES
                or (retained and kind != "status")):
            return True
        stamp = time.time()
        try:
            receipts = await asyncio.to_thread(
                ingest, self.store, topic, bytes(message.data), session.username, stamp, "mqtt")
        except ValueError as exc:
            LOG.warning("rejected packet kind=%s: %s", kind, exc)
            return True
        except (StoreError, sqlite3.Error):
            LOG.exception("storage failed, no application receipt sent")
            return True
        self.last_message_at = stamp
        LOG.info("committed kind=%s device=%s events=%d", kind, session.username, len(receipts))
        for event_id, result in receipts.items():
            payload = json.dumps({"event_id": event_id, "status": result},
                                 separators=(",", ":")).encode("ascii")
            await self._broadcast_message(None, uplink_topic(session.username, "ack"), payload, 1)
            LOG.info("queued app ACK event_id=%s status=%s", event_id, result)
        # Preserve valid retained status behavior. Devices can subscribe only to their ACK.
        return await Broker._handle_message_delivery(self, session, handler, wait_deliver)

    async def _handle_disconnect(self, session, handler, disconnect_waiter):
        from amqtt.mqtt.disconnect import DisconnectPacket

        result = disconnect_waiter.result()
        if session.will_flag and not isinstance(result, DisconnectPacket):
            try:
                stamp = time.time()
                await asyncio.to_thread(ingest, self.store, session.will_topic,
                                        bytes(session.will_message), session.username, stamp, "mqtt")
                self.last_message_at = stamp
            except (ValueError, StoreError, sqlite3.Error):
                LOG.exception("validated offline will could not be recorded")
        await super()._handle_disconnect(session, handler, disconnect_waiter)

    def health(self):
        return {"ready": self.active, "source": "hardware", "mqtt": {
            "listening": self.active, "last_message_at": iso(self.last_message_at),
        }}


def validate_port(value):
    port = int(value)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def validate_host(value):
    try:
        addr = IPv4Address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("enter the exact IPv4 address of the test interface") from exc
    if not (str(addr) == "127.0.0.1" or any(addr in net for net in PRIVATE_NETS)):
        raise argparse.ArgumentTypeError("broker must bind to one private interface or loopback")
    return str(addr)


def init(args):
    runtime = args.runtime.resolve()
    settings_file = runtime / CONFIG_FILE
    if args.device_a == args.device_b or args.code_a == args.code_b:
        raise ValueError("the two badges must have distinct device IDs and IR codes")
    if settings_file.exists():
        existing = load(runtime)
        expected = [(args.device_a, code16(args.code_a)), (args.device_b, code16(args.code_b))]
        actual = [(existing["bindings"][u]["device_id"], existing["bindings"][u]["code16"])
                  for u in ("alice", "bob")]
        if expected != actual or (args.web_port, args.mqtt_port) != (
            existing["web_port"], existing["mqtt"]["port"]
        ):
            raise ValueError("runtime already provisioned with different badges or ports")
        return runtime
    if args.web_port == args.mqtt_port or (runtime / DB_FILE).exists():
        raise ValueError("choose distinct ports and a fresh runtime directory")
    bindings = {
        "alice": {"device_id": args.device_a, "code16": code16(args.code_a), "alias": "挂件 A"},
        "bob": {"device_id": args.device_b, "code16": code16(args.code_b), "alias": "挂件 B"},
    }
    users = [{"id": user, "display_name": name, "password": secrets.token_urlsafe(18),
              "bio": "EIE568 Group 9 本机联调账号", "contact": f"{user}@example.invalid"}
             for user, name in (("alice", "Alice"), ("bob", "Bob"), ("charlie", "Charlie"))]
    config = {"config_version": 1, "bindings": bindings, "users": users,
              "mqtt": {"port": args.mqtt_port, "accounts": {
                  v["device_id"]: {"client_id": v["device_id"], "password": secrets.token_urlsafe(24)}
                  for v in bindings.values()
              }}, "web_port": args.web_port}
    runtime.mkdir(parents=True, exist_ok=True)
    with settings_file.open("x", encoding="utf-8") as out:
        json.dump(config, out, indent=2, ensure_ascii=False)
        out.write("\n")
    with (runtime / "credentials.txt").open("x", encoding="utf-8") as out:
        out.write("Bump 真板联调网页测试账号，请留在本机。\n")
        out.writelines(f"{u['id']}: {u['password']}\n" for u in users)
    return runtime


def load(runtime):
    path = runtime.resolve()
    settings = json.loads((path / CONFIG_FILE).read_text(encoding="utf-8"))
    if settings.get("config_version") != 1:
        raise ValueError("unrecognized hardware test runtime")
    settings["db_path"] = str(path / DB_FILE)
    return settings


async def serve(runtime, bind_host):
    settings = load(runtime)
    broker = BoardBroker(LiveStore(settings), settings, bind_host)
    await broker.start()
    broker.active = True
    try:
        LOG.info("MQTT 3.1.1 TCP listening at %s:%d; HTTP loopback 127.0.0.1:%d",
                 bind_host, settings["mqtt"]["port"], settings["web_port"])
        server = Server(Config(create_app(broker.store, broker.health), host="127.0.0.1",
                               port=settings["web_port"], access_log=False))
        await server.serve()
    finally:
        broker.active = False
        await broker.shutdown()


def inspect(runtime):
    path = Path(load(runtime)["db_path"])
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        queries = {
            "encounters": """SELECT event_id,device,boot,peer_short,peer_device,
               received_at,start_at,seen_at,discovery_start_at,discovery_end_at,
               rssi_avg,samples,transport FROM resolved
               ORDER BY received_at DESC LIMIT 100""",
            "status": "SELECT * FROM live_status ORDER BY device",
            "bindings": "SELECT * FROM bindings ORDER BY last_seen DESC LIMIT 100",
            "ir": """SELECT event_id,device,boot,peer_device,uptime_ms,received_at,transport,
                      matched,request_id FROM ir_events ORDER BY received_at DESC LIMIT 100""",
            "requests": """SELECT id,sender_id,recipient_id,status,created_at
                           FROM contact_requests ORDER BY id DESC LIMIT 100""",
        }
        return {key: [dict(row) for row in db.execute(sql)] for key, sql in queries.items()}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "serve", "inspect", "ingest"):
        sub.add_parser(name).add_argument("--runtime", type=Path, required=True)
    provision = sub.choices["init"]
    provision.add_argument("--device-a", required=True)
    provision.add_argument("--device-b", required=True)
    provision.add_argument("--code-a", default="A30F", type=code16)
    provision.add_argument("--code-b", default="C371", type=code16)
    provision.add_argument("--mqtt-port", default=18883, type=validate_port)
    provision.add_argument("--web-port", default=8875, type=validate_port)
    sub.choices["serve"].add_argument("--mqtt-host", required=True, type=validate_host)
    sub.choices["ingest"].add_argument("--device", required=True)
    sub.choices["ingest"].add_argument("--kind", choices=UPLINK, required=True)
    args = parser.parse_args()
    supplied = {"init": ("device_a", "device_b"), "ingest": ("device",)}.get(args.command, ())
    if any(not DEVICE_ID.fullmatch(getattr(args, key)) for key in supplied):
        parser.error("device ID must be bump- followed by twelve lowercase hex digits")
    return args


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        if args.command == "init":
            root = init(args)
            print(f"Provisioned: {root / CONFIG_FILE}")
        elif args.command == "serve":
            asyncio.run(serve(args.runtime, args.mqtt_host))
        elif args.command == "inspect":
            print(json.dumps(inspect(args.runtime), ensure_ascii=True, indent=2))
        elif args.command == "ingest":
            store = LiveStore(load(args.runtime))
            receipts = ingest(store, uplink_topic(args.device, args.kind),
                              sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1), args.device,
                              time.time(), "usb")
            print(json.dumps({"receipts": receipts}, separators=(",", ":")))
    except (ValueError, OSError, StoreError, sqlite3.Error) as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.modules.setdefault("live_server", sys.modules[__name__])
    raise SystemExit(main())

