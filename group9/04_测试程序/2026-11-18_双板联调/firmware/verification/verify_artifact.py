from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import struct
import sys

root = Path(sys.argv[1])
build = root / "build"
firmware = build / "BumpDualBadge.ino.uf2"
flat = (build / "BumpDualBadge.ino.bin").read_bytes()
uf2 = firmware.read_bytes()
expected = "CF10CB135538EFCD809C15C746456B82CC9D6DE23E12D34E4A9C22C11A06F740".lower()
assert sha256(uf2).hexdigest() == expected
assert len(uf2) % 512 == 0
blocks = len(uf2) // 512
data = bytearray()
address = None
for index in range(blocks):
    block = uf2[index * 512: (index + 1) * 512]
    m1, m2, flags, target, length, ordinal, block_count, family = struct.unpack_from("<8I", block)
    assert (m1, m2, family) == (0x0A324655, 0x9E5D5157, 0xE48BFF56)
    assert flags & 0x2000 and 0 < length <= 476
    assert struct.unpack_from("<I", block, 508)[0] == 0x0AB16F30
    assert (ordinal, block_count) == (index, blocks)
    if address is None:
        address = target
    assert target == address + len(data)
    data.extend(block[32:32 + length])
assert data[:len(flat)] == flat and not any(data[len(flat):])
elf = build / "BumpDualBadge.ino.elf"
header = elf.read_bytes()[:20]
assert header[:6] == b"\x7fELF\x01\x01" and int.from_bytes(header[18:20],"little") == 40

names = ("BadgeApp.cpp", "BadgeApp.h", "BumpDualBadge.ino",
         "MotionDetector.h", "Nearfield.cpp", "Nearfield.h", "WireAck.h")
assets = [root / "BumpDualBadge" / name for name in names]
assets += [firmware, build / "BumpDualBadge.ino.bin", elf,
           root / "vendor" / "IRremote-4.7.1.zip",
           root / "vendor" / "firmware" / "nearfield_pairing" / "nearfield_pairing.ino",
           root / "baseline-build" / "nearfield_pairing.ino.uf2"]
manifest = {
    "firmware": "BumpDualBadge 0.5.0-dual",
    "compiled_utc": datetime.fromtimestamp(firmware.stat().st_mtime, timezone.utc).isoformat(),
    "fqbn": "arduino:mbed_nano:nanorp2040connect",
    "core_version": "4.6.0",
    "libraries": {
        "ArduinoBLE": "2.1.0",
        "WiFiNINA": "2.1.1",
        "Arduino_SpiNINA": "0.0.2",
        "ArduinoMqttClient": "0.1.8",
        "Arduino_LSM6DSOX": "1.1.2",
        "IRremote": "4.7.1",
    },
    "flash_bytes": 163345,
    "static_ram_bytes": 50152,
    "uf2_blocks": blocks,
    "uf2_target_address": hex(address),
    "ble_gate": {"rssi_min_dbm": -70, "samples_min": 2, "span_min_ms": 3000,
                 "window_ms": 60000},
    "offline_validation": (root / "verification" / "wire_probe_result.txt").read_text().strip(),
    "files": {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest() for p in assets},
}
(root / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(f"PASS: {blocks} valid RP2040 UF2 blocks, continuous flash image matching the compiled BIN and ARM ELF")
