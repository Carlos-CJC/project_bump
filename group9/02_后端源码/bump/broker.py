"""TLS MQTT broker for a self-contained local demo; not an Internet service."""

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

from amqtt.broker import Broker
from amqtt.contexts import Action
from amqtt.errors import AMQTTError
from amqtt.mqtt.protocol.broker_handler import BrokerProtocolHandler
from amqtt.plugins.base import BaseAuthPlugin, BaseTopicPlugin

from .config import V1_MQTT_DEVICES, V1_TOPIC_FILTERS, V1_WORKER_ACCOUNT
from .v1_wire import MAX_PAYLOAD_BYTES, parse_message


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
        if user == V1_WORKER_ACCOUNT:
            if action == Action.SUBSCRIBE:
                return topic in V1_TOPIC_FILTERS
            if action == Action.RECEIVE:
                return any(topic == f"bump/v1/device/{device}/{kind}"
                           for device in V1_MQTT_DEVICES for kind in ("encounters", "status"))
            return False  # No application ACK/downlink or legacy publishing privileges.
        if user in V1_MQTT_DEVICES:
            return action == Action.PUBLISH and topic in (
                f"bump/v1/device/{user}/encounters", f"bump/v1/device/{user}/status"
            )
        if user == "_worker":
            if action == Action.SUBSCRIBE:
                return topic == "bump/v1/device/+/events"
            suffix = "ack" if action == Action.PUBLISH else "events"
            return topic in {f"bump/v1/device/{d}/{suffix}" for d in self.config.devices}
        if user not in self.config.devices:
            return False
        suffix = "events" if action == Action.PUBLISH else "ack"
        return topic == f"bump/v1/device/{user}/{suffix}"


CONNECTION_LOG = logging.getLogger("bump.mqtt.stages")


def connection_stage(peer, stage, **metadata):
    fields = " ".join(f"{key}={value}" for key, value in metadata.items())
    CONNECTION_LOG.info("stage=%s peer=%s:%s%s", stage, peer[0], peer[1],
                        " " + fields if fields else "")


class ObservedProtocolHandler(BrokerProtocolHandler):
    async def mqtt_connack_authorize(self, authorize):
        issued_utc, issued_monotonic = time.time(), time.monotonic()
        await super().mqtt_connack_authorize(authorize)
        if authorize:
            self.sync_session_at = issued_utc
            self.clock_connack_bound = (issued_utc, issued_monotonic)
        session = self.session
        connection_stage((session.remote_address, session.remote_port),
                         "connack_processed", accepted=int(authorize))

    async def mqtt_acknowledge_subscription(self, packet_id, return_codes):
        await super().mqtt_acknowledge_subscription(packet_id, return_codes)
        session = self.session
        connection_stage((session.remote_address, session.remote_port),
                         "suback_processed", packet_id=int(packet_id),
                         codes=",".join(str(int(code)) for code in return_codes))


class AuthenticatedBroker(Broker):
    """Authenticate a fresh CONNECT before touching any existing session.

    The pinned aMQTT 0.11.4 initializer reuses/deletes cached sessions before
    its authentication hook. This local override does not enter that path.
    Bump clients use clean sessions; durable delivery is handled by our queue.
    """

    # The deadline starts at TCP acceptance and includes admission wait.
    CONNECT_TIMEOUT_S = 10.0
    CLOSE_TIMEOUT_S = 1.0

    async def _client_connected(self, listener_name, reader, writer):
        peer = writer.get_peer_info() or ("unknown", 0)
        loop = asyncio.get_running_loop()
        accepted_at = loop.time()
        deadline = accepted_at + self.CONNECT_TIMEOUT_S
        server = self._servers.get(listener_name)
        acquired = False
        phase = "admission"
        connection_stage(peer, "tcp_accepted")
        try:
            if server is None:
                raise AMQTTError("Unknown MQTT listener")
            async with asyncio.timeout_at(deadline):
                await server.acquire_connection()
            acquired = True
            phase = "connect"
            handler, session = await self._initialize_client_session(
                reader, writer, peer[0], peer[1], deadline=deadline)
            phase = "session"
            await self._handle_client_session(reader, writer, session, handler, server, listener_name)
        except TimeoutError:
            connection_stage(peer, "timeout", phase=phase,
                             elapsed_ms=round((loop.time() - accepted_at) * 1000))
        except Exception as exc:
            # Exception text from a decoder may interpolate untrusted fields.
            connection_stage(peer, "connection_error", phase=phase, error_type=type(exc).__name__)
        finally:
            if acquired:
                server.release_connection()
            try:
                await asyncio.wait_for(writer.close(), timeout=self.CLOSE_TIMEOUT_S)
            except Exception as exc:
                connection_stage(peer, "close_error", error_type=type(exc).__name__)
            connection_stage(peer, "tcp_closed", released_slot=int(acquired),
                             elapsed_ms=round((loop.time() - accepted_at) * 1000))

    async def _initialize_client_session(self, reader, writer, remote_address, remote_port,
                                         *, deadline=None):
        peer = (remote_address, remote_port)
        first_packet_started = asyncio.get_running_loop().time()
        if deadline is None:
            deadline = first_packet_started + self.CONNECT_TIMEOUT_S
        async with asyncio.timeout_at(deadline):
            handler, session = await ObservedProtocolHandler.init_from_connect(
                reader, writer, self.plugins_manager
            )
            connection_stage(peer, "connect_decoded",
                             read_ms=round((asyncio.get_running_loop().time() - first_packet_started) * 1000))
            if not session.clean_session or not await self._authenticate(
                session, self.listeners_config["default"]
            ):
                raise AMQTTError("Connection rejected before session initialization.")
            existing = self._sessions.get(session.client_id)
            if existing and existing[0].transitions.is_connected():
                await existing[1].handle_connection_closed()
            await self._cleanup_session(session.client_id)
            session.parent = 0
            delay = self.config.get("timeout-disconnect-delay", 0)
            if session.keep_alive > 0 and isinstance(delay, int):
                session.keep_alive += delay
        return handler, session

    async def _handle_subscription(self, client_session, handler, subscribe_waiter):
        subscribe = subscribe_waiter.result()
        connection_stage((client_session.remote_address, client_session.remote_port),
                         "subscribe_processing", packet_id=int(subscribe.packet_id),
                         topic_count=len(subscribe.topics))
        await super()._handle_subscription(client_session, handler, subscribe_waiter)

    async def _handle_message_delivery(self, client_session, handler, wait_deliver):
        # A live MQTT subscriber does not receive the publisher's RETAIN bit.
        # Enforce it on the incoming packet, before *either* broadcast or caching.
        msg = wait_deliver.result()
        user = client_session.username
        if msg is not None and user in V1_MQTT_DEVICES:
            if msg.qos != 1 or not msg.data or len(msg.data) > MAX_PAYLOAD_BYTES:
                return True
            if msg.publish_packet is not None and msg.publish_packet.retain_flag:
                if msg.topic != f"bump/v1/device/{user}/status":
                    return True
                # Malformed status must not replace the last good retained value.
                try:
                    parse_message(msg.topic, bytes(msg.data), observed_at=time.time(),
                                  retained=True, authenticated_device_id=user)
                except ValueError:
                    return True
        return await super()._handle_message_delivery(client_session, handler, wait_deliver)

    async def _publish_retained_messages_for_subscription(self, subscription, session):
        # The pinned broker's connect path walks all filters. Replay only where
        # this actual session registered the filter AND may receive the topic.
        topic_filter, qos = subscription
        if not any(owner is session for owner, _ in self._subscriptions.get(topic_filter, ())):
            return
        if not await self._topic_filtering(session, topic_filter, Action.SUBSCRIBE):
            return
        for topic in tuple(self._retained_messages):
            if self._matches(topic, topic_filter) and await self._topic_filtering(
                session, topic, Action.RECEIVE
            ):
                await super()._publish_retained_messages_for_subscription((topic, qos), session)

    async def _shutdown_broadcast_loop(self):
        # amqtt 0.11.4 gathers in-flight QoS deliveries after closing sessions.
        # A peer can disconnect before its final PUBACK, cancelling that child.
        # Let shutdown release the listener in that case; genuine cancellation
        # of the shutdown caller itself must still propagate.
        try:
            await super()._shutdown_broadcast_loop()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if (task is None or task.cancelling() or self._broadcast_task is None
                    or not self._broadcast_task.cancelled()):
                raise
            self._broadcast_shutdown_waiter = asyncio.get_running_loop().create_future()


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
