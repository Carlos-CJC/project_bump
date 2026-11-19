"""Verify failure on the second insert and replay after undelivered ACKs on loopback."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading

from smoke_test import (
    A, B, BOOT_A, SHORT_A, SHORT_B, Client, command, encounters, select_ports,
    wait_state, with_server,
)


def verify(runtime, config):
    packet = encounters(A, BOOT_A, SHORT_A, SHORT_B)
    first = packet["events"][0]
    first_id = first["event_id"]
    second_id = first_id[:-8] + "00000002"
    packet["events"].append({**first, "event_id": second_id, "occurred_uptime_ms": 42000})
    packet["batch_id"] = packet["batch_id"][:-4] + "0002"
    expected = {first_id: 1, second_id: 1}
    ack_topic = f"bump/v1/device/{A}/ack"

    def service_log():
        return (runtime / "service.log").read_text(encoding="utf-8", errors="replace")

    with closing(sqlite3.connect(runtime / "live.sqlite3", isolation_level=None)) as db:
        def row_counts():
            return dict(db.execute("SELECT event_id, count(*) FROM observations GROUP BY event_id"))

        assert row_counts() == {}
        db.execute("CREATE TABLE test_batch_ids (first_id TEXT, second_id TEXT)")
        db.execute("INSERT INTO test_batch_ids VALUES (?, ?)", (first_id, second_id))
        # Fail only after the first row exists inside the same transaction.
        db.execute("""
            CREATE TRIGGER test_reject_second BEFORE INSERT ON observations
            WHEN NEW.event_id = (SELECT second_id FROM test_batch_ids)
              AND EXISTS (SELECT 1 FROM observations
                          WHERE event_id = (SELECT first_id FROM test_batch_ids))
            BEGIN SELECT RAISE(ABORT, 'fixture_rejected_second_insert'); END
        """)
        with Client(config, A) as device:
            device.publish("encounters", packet)
            log = wait_state(service_log, lambda s: "sqlite3.IntegrityError: fixture_rejected_second_insert" in s)
            assert "storage failed, no application receipt sent" in log
            assert "queued app ACK" not in log
            assert row_counts() == {}
            device.no_ack()

            db.execute("DROP TRIGGER test_reject_second")
            db.execute("DROP TABLE test_batch_ids")
            # This is the only MQTT client. Wait for the broker to remove its ACK subscription.
            unsubscribed = threading.Event()
            device.client.on_unsubscribe = lambda *_: unsubscribed.set()
            rc, _ = device.client.unsubscribe(ack_topic)
            assert rc == 0 and unsubscribed.wait(5)
            device.publish("encounters", packet)
            stored_log = wait_state(service_log, lambda s: all(
                f"queued app ACK event_id={eid} status=stored" in s for eid in expected))
            assert stored_log.count("queued app ACK") == 2
            assert row_counts() == expected
            device.no_ack()

            device.subscribed.clear()
            rc, _ = device.client.subscribe(ack_topic, qos=1)
            assert rc == 0 and device.subscribed.wait(5) and device.grants == [1]
            device.no_ack()
            device.publish("encounters", packet)
            for eid in expected:
                device.ack(eid, "duplicate")
            device.no_ack()
            final_counts = row_counts()
            assert final_counts == expected

    return {"mqtt_pubacks": 3, "after_second_insert_failed": {"rows": 0, "app_acks_received": 0},
            "after_unsubscribed_retry": {"rows": 2, "stored_acks_queued": 2, "app_acks_received": 0},
            "after_subscribed_retry": {"rows_per_event": list(final_counts.values()), "duplicate_acks_received": 2}}


def main():
    mqtt_port, web_port = select_ports()
    url = f"http://127.0.0.1:{web_port}"
    with TemporaryDirectory(prefix="bump-recovery-") as tmp:
        runtime = Path(tmp)
        command("init", "--runtime", runtime, "--device-a", A, "--device-b", B,
                "--mqtt-port", mqtt_port, "--web-port", web_port, capture_output=True)
        config = json.loads((runtime / "live-config.json").read_text(encoding="utf-8"))
        with_server(runtime / "service.log", runtime, url,
                    lambda: print(json.dumps(verify(runtime, config), indent=2)))


if __name__ == "__main__":
    main()
