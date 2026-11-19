"""Verify metadata logs, bounded first-packet wait, and ordinary MQTT reconnection."""

import argparse
import asyncio
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from bump.broker import CONNECTION_LOG
from bump.live_store import LiveStore
from live_server import BoardBroker, init, load, open_connection_log
from smoke_test import A, B, BOOT_A, SHORT_A, SHORT_B, Client, encounters, select_ports


HERE = Path(__file__).resolve().parent
LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z "
                      r"stage=[a-z_]+ peer=127\.0\.0\.1:\d+(?: [a-z_]+=[A-Za-z0-9_,.]+)*$")
STAGES = ["tcp_accepted", "connect_decoded", "connack_processed",
          "subscribe_processing", "suback_processed", "tcp_closed"]


async def wait_for_slot(server, count):
    async with asyncio.timeout(4):
        while server.conn_count != count:
            await asyncio.sleep(0.02)


def use_badge(settings, expected):
    event = encounters(A, BOOT_A, SHORT_A, SHORT_B)
    eid = event["events"][0]["event_id"]
    with Client(settings, A) as sender:
        sender.publish("encounters", event)
        sender.ack(eid, expected)


async def exercise(runtime):
    settings = load(runtime)
    store = LiveStore(settings)
    handler = open_connection_log(runtime)
    broker = BoardBroker(store, settings, "127.0.0.1")
    # Keep the production default; shorten only this isolated test listener.
    assert broker.CONNECT_TIMEOUT_S == 10.0
    broker.CONNECT_TIMEOUT_S = 0.45
    broker.listeners_config["default"].max_connections = 1
    try:
        await broker.start()
        server = broker._servers["default"]
        reader, writer = await asyncio.open_connection("127.0.0.1", settings["mqtt"]["port"])
        idle_port = writer.get_extra_info("sockname")[1]
        try:
            await wait_for_slot(server, 1)
            assert await asyncio.wait_for(reader.read(1), timeout=2) == b""
            await wait_for_slot(server, 0)
        finally:
            writer.close()
            await writer.wait_closed()

        # One provisioned client disconnects normally and reconnects using the
        # same identity. Retrying the same committed event returns duplicate.
        await asyncio.to_thread(use_badge, settings, "stored")
        await wait_for_slot(server, 0)
        await asyncio.to_thread(use_badge, settings, "duplicate")
        await wait_for_slot(server, 0)

        # Read the open file before shutdown to verify records were flushed live.
        records = (runtime / "mqtt-connection-stages.log").read_text(encoding="utf-8").splitlines()
        assert records and all(LOG_LINE.fullmatch(line) for line in records), records
        assert not any(secret in line for line in records for secret in (
            A, B, settings["mqtt"]["accounts"][A]["password"], settings["mqtt"]["accounts"][B]["password"],
            "bump/v1/device",
        ))
        grouped = {}
        for line in records:
            port = int(line.split(" peer=127.0.0.1:",1)[1].split()[0])
            grouped.setdefault(port, []).append(line)
        assert len(grouped) == 3
        assert any("stage=timeout" in x and "phase=connect" in x for x in grouped[idle_port])
        assert any("stage=tcp_closed" in x and "released_slot=1" in x for x in grouped[idle_port])
        for port, lines in grouped.items():
            if port != idle_port:
                assert [x.split("stage=",1)[1].split()[0] for x in lines] == STAGES, lines
        with store._transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 1
        return len(records)
    finally:
        CONNECTION_LOG.removeHandler(handler)
        handler.close()
        await broker.shutdown()


def main():
    port, web_port = select_ports()
    with TemporaryDirectory(prefix="stage-test-", dir=HERE) as path:
        runtime = Path(path)
        init(argparse.Namespace(runtime=runtime, device_a=A, device_b=B, code_a="A30F",
                                code_b="C371", web_port=web_port, mqtt_port=port))
        records = asyncio.run(exercise(runtime))
    print(f"PASS: idle first-packet wait expired and released the only slot; ordinary reconnect "
          f"delivered once; {records} live-flushed metadata records contain no credentials.")


if __name__ == "__main__":
    main()

