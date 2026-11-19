#!/usr/bin/env python3
"""把公开 HiveMQ 的 v1 上报桥接进本地接收端。

固件（platformio env mqtt）只把消息发到 broker.hivemq.com:8883；
group9 网页不订阅 HiveMQ，只读本地 live_server 的 SQLite。
本进程订阅公开主题，并复用 live_server.ingest 写入同一个 runtime，
这样真板数据才会出现在网页里。
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import re
import secrets
import sys
import time

BACKEND = Path(__file__).resolve().parent / "02_后端源码"
sys.path.insert(0, str(BACKEND))

import paho.mqtt.client as mqtt  # noqa: E402

import live_server  # noqa: E402

LOG = logging.getLogger("bump.bridge")
UPLINK_TOPIC = re.compile(
    r"^bump/v1/device/(bump-[0-9a-f]{12})/(status|encounters|ir)$"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True,
                        help="live_server 的 runtime 目录（含 live-config.json）")
    parser.add_argument("--host", default="broker.hivemq.com")
    parser.add_argument("--port", type=int, default=8883)
    parser.add_argument("--no-tls", action="store_true",
                        help="明文 MQTT，本地调试用")
    parser.add_argument("--client-id", default=None,
                        help="默认随机，避免两个桥接互相踢下线")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    settings = live_server.load(args.runtime)
    store = live_server.LiveStore(settings)
    expected = ", ".join(sorted(store.by_device))
    LOG.info("已登记设备：%s", expected)
    LOG.info("桥接 %s:%d -> %s", args.host, args.port, settings["db_path"])

    client_id = args.client_id or f"bump-bridge-{secrets.token_hex(4)}"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    if not args.no_tls:
        client.tls_set()
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    unknown_seen: set[str] = set()

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("连接失败：%s", reason_code)
            return
        LOG.info("已连接 %s:%d", args.host, args.port)
        for kind in live_server.UPLINK:
            topic = f"{live_server.TOPIC_BASE}/+/{kind}"
            client.subscribe(topic, qos=1)
            LOG.info("订阅 %s", topic)

    def on_disconnect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.warning("连接断开：%s，稍后重连", reason_code)

    def on_message(client, userdata, message):
        matched = UPLINK_TOPIC.match(message.topic)
        if matched is None:
            return
        device, kind = matched.groups()
        if device not in store.by_device:
            if device not in unknown_seen:
                unknown_seen.add(device)
                LOG.warning(
                    "收到未登记设备 %s 的消息；用该 ID 重建 runtime 后才会显示", device
                )
            return
        stamp = time.time()
        # 公网中转看不到设备与 HiveMQ 之间的 CONNACK。online status 紧跟在
        # CONNECT 之后发出，这里用接收时刻近似 CONNACK，让服务端仍能为该次
        # 启动建立时钟锚点；误差只剩单程网络时延。
        connack_at = stamp if kind == "status" and not message.retain else None
        try:
            receipts = live_server.ingest(
                store, message.topic, message.payload, device, stamp,
                "mqtt", clock_connack_at=connack_at,
                retained=bool(message.retain),
            )
        except Exception as exc:  # 单条坏消息不应中断桥接
            LOG.warning("丢弃 %s/%s：%s", device, kind, exc)
            return
        for event_id, status in receipts.items():
            LOG.info("%s %s %s -> %s", device, kind, event_id, status)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    try:
        client.connect(args.host, args.port, keepalive=60)
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        pass
    finally:
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
