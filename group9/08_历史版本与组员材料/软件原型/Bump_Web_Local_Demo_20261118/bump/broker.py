"""TLS MQTT broker for a self-contained local demo; not an Internet service."""

import asyncio
import secrets
import threading
from dataclasses import dataclass, field

from amqtt.broker import Broker
from amqtt.contexts import Action
from amqtt.errors import AMQTTError
from amqtt.mqtt.protocol.broker_handler import BrokerProtocolHandler
from amqtt.plugins.base import BaseAuthPlugin, BaseTopicPlugin


class LocalAuthPlugin(BaseAuthPlugin):
    @dataclass
    class Config:
        accounts: dict[str, dict[str, str]] = field(default_factory=dict)

    async def authenticate(self, *, session) -> bool:
        account = self.config.accounts.get(session.username)
        # This app does not use MQTT will messages. Reject them at CONNECT so a
        # device cannot forge an application ACK through a broker will.
        return bool(
            account and session.clean_session and not session.will_flag
            and session.client_id == account["client_id"]
            and isinstance(session.password, str)
            and session.password.isascii()
            and secrets.compare_digest(session.password, account["password"])
        )


class LocalTopicPlugin(BaseTopicPlugin):
    @dataclass
    class Config:
        devices: list[str] = field(default_factory=list)

    async def topic_filtering(self, *, session=None, topic=None, action=None) -> bool:
        if session is None or not topic:
            return False
        user = session.username
        if user == "_worker":
            if action == Action.SUBSCRIBE:
                return topic == "bump/v1/device/+/events"
            suffix = "ack" if action == Action.PUBLISH else "events"
            return topic in {f"bump/v1/device/{d}/{suffix}" for d in self.config.devices}
        if user not in self.config.devices:
            return False
        suffix = "events" if action == Action.PUBLISH else "ack"
        return topic == f"bump/v1/device/{user}/{suffix}"


class AuthenticatedBroker(Broker):
    """Authenticate a fresh CONNECT before touching any existing session.

    The pinned aMQTT 0.11.4 initializer reuses/deletes cached sessions before
    its authentication hook. This local override does not enter that path.
    Bump clients use clean sessions; durable delivery is handled by our queue.
    """

    async def _initialize_client_session(self, reader, writer, remote_address, remote_port):
        try:
            handler, session = await BrokerProtocolHandler.init_from_connect(
                reader, writer, self.plugins_manager
            )
            if not session.clean_session or not await self._authenticate(
                session, self.listeners_config["default"]
            ):
                raise AMQTTError("Connection rejected before session initialization.")
        except Exception:
            await writer.close()
            raise
        existing = self._sessions.get(session.client_id)
        if existing and existing[0].transitions.is_connected():
            await existing[1].handle_connection_closed()
        await self._cleanup_session(session.client_id)
        session.parent = 0
        delay = self.config.get("timeout-disconnect-delay", 0)
        if session.keep_alive > 0 and isinstance(delay, int):
            session.keep_alive += delay
        return handler, session


def make_broker(settings: dict) -> Broker:
    mqtt = settings["mqtt"]
    return AuthenticatedBroker({
        "listeners": {"default": {
            "type": "tcp", "bind": f"127.0.0.1:{mqtt['port']}", "ssl": True,
            "cafile": mqtt["ca_file"], "certfile": mqtt["cert_file"],
            "keyfile": mqtt["key_file"], "max_connections": 16,
        }},
        "plugins": {
            "bump.broker.LocalAuthPlugin": {"accounts": mqtt["accounts"]},
            "bump.broker.LocalTopicPlugin": {"devices": list(settings["devices"])},
        },
    })


async def serve_broker(settings: dict) -> None:
    broker = make_broker(settings)
    await broker.start()
    try:
        await asyncio.Event().wait()
    finally:
        await broker.shutdown()


class BrokerThread:
    """Bounded lifecycle helper for the synchronous test suite."""

    def __init__(self, settings):
        self.settings = settings
        self.ready = threading.Event()
        self.error = None
        self.loop = None
        self.stop_event = None
        self.thread = None

    def start(self, timeout=5):
        def run():
            async def serve():
                self.loop = asyncio.get_running_loop()
                self.stop_event = asyncio.Event()
                broker = make_broker(self.settings)
                await broker.start()
                self.ready.set()
                try:
                    await self.stop_event.wait()
                finally:
                    await broker.shutdown()
            try:
                asyncio.run(serve())
            except Exception as exc:
                self.error = exc
                self.ready.set()

        self.thread = threading.Thread(target=run, name="bump-broker", daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout):
            raise TimeoutError("MQTT broker did not start in time.")
        if self.error:
            raise self.error
        return self

    def stop(self):
        if self.loop and self.stop_event and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.stop_event.set)
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise TimeoutError("MQTT broker did not shut down.")

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()
