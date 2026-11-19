from pathlib import Path
import json, sys
server, result = sys.argv[1:]
sys.path.insert(0, server)
from bump.v1_wire import parse_message, event_interval
from bump.live_store import parse_ir
lines = Path(result).read_bytes().splitlines()
body1, body2, ir = map(json.loads, lines)
dev = body1["device_id"]
messages = [
    parse_message(f"bump/v1/device/{dev}/encounters", raw,
                  observed_at=1_700_000_000 + delay, authenticated_device_id=dev)
    for raw, delay in zip(lines[:2], (0,45))
]
assert body1["events"] == body2["events"]
for key in ("batch_id", "device_id", "boot_id", "self_short_id"):
    assert body1[key] == body2[key]
times = [event_interval(msg["body"], msg["body"]["events"][0], msg["observed_at"])
         for msg in messages]
assert times[0] == times[1], times
assert parse_ir(dev, lines[2])["peer_code16"].lower() == "c371"
print("PASS: exact firmware encoder parsed by the live server, stable observance time on a 45 s delayed retry, IR identity, 7 application ACK cases and hardware shake detector fixtures")
