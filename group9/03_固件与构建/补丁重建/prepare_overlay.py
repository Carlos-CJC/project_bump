"""Rebuild the private Nano RP2040 ArduinoBLE overlay from installed 2.1.0."""

from argparse import ArgumentParser
from hashlib import sha256
import json
from pathlib import Path
from shutil import copy2, copytree


def replace_once(path, old, new):
    value = path.read_text(encoding="utf-8")
    if value.count(old) != 1:
        raise ValueError(f"unexpected ArduinoBLE source shape in {path.name}")
    path.write_text(value.replace(old, new), encoding="utf-8", newline="\n")


def replace_methods(path, first, following, replacement):
    value = path.read_text(encoding="utf-8")
    if value.count(first) != 1 or value.count(following) != 1:
        raise ValueError(f"unexpected ArduinoBLE method layout in {path.name}")
    start, end = value.index(first), value.index(following)
    if start >= end:
        raise ValueError("unexpected ArduinoBLE method order")
    path.write_text(value[:start] + replacement + "\n\n" + value[end:],
                    encoding="utf-8", newline="\n")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path, help="installed ArduinoBLE 2.1.0")
    parser.add_argument("--output", required=True, type=Path, help="new local ArduinoBLE directory")
    parser.add_argument("--nina-stop-reset", action="store_true",
                        help="assert the onboard NINA reset line when closing Nano RP2040 BLE")
    args = parser.parse_args()
    base = args.base.resolve(strict=True)
    out = args.output.resolve()
    here = Path(__file__).resolve().parent
    if out.parent != here or out.exists():
        parser.error("--output must be a new directory inside nina_probe")
    if "version=2.1.0" not in (base / "library.properties").read_text(encoding="utf-8").splitlines():
        parser.error("ArduinoBLE 2.1.0 is required")
    out.mkdir()
    for name in ("library.properties", "LICENSE", "README.md"):
        copy2(base / name, out / name)
    copytree(base / "src", out / "src")
    utility = out / "src" / "utility"
    copy2(here / "BumpNinaRxBatch.h", utility / "BumpNinaRxBatch.h")

    header = utility / "HCINinaSpiTransport.h"
    replace_once(header, '#include "HCITransport.h"',
                 '#include "HCITransport.h"\n#include "BumpNinaRxBatch.h"')
    replace_once(header, "  virtual size_t write(const uint8_t* data, size_t length);\n};",
                 "  virtual size_t write(const uint8_t* data, size_t length);\n\n"
                 "private:\n  BumpNinaRxBatch _receiveBatch;\n};")

    transport = utility / "HCINinaSpiTransport.cpp"
    replace_methods(transport, "int BleDrv::bleAvailable()", "int BleDrv::bleRead(",
                    """int BleDrv::bleAvailable() {
    WAIT_FOR_SLAVE_SELECT();
    uint8_t result[2] = {};

    SpiDrv::sendCmd(BLE_AVAILABLE, PARAM_NUMS_0);
    SpiDrv::spiSlaveDeselect();
    SpiDrv::waitForSlaveReady();
    SpiDrv::spiSlaveSelect();

    uint8_t length = sizeof(result);
    const bool received = SpiDrv::waitResponseCmd(BLE_AVAILABLE, PARAM_NUMS_1, result, &length);
    SpiDrv::spiSlaveDeselect();
    return received && length == sizeof(result) ? BumpNinaRxBatch::decodeAvailability(result) : 0;
}""")
    replace_once(transport, "int HCINinaSpiTransportClass::begin()\n{\n  SpiDrv::begin();",
                 "extern \"C\" void bump_nina_trace(uint8_t activity) __attribute__((weak));\n"
                 "static void traceNina(uint8_t activity)\n"
                 "{\n  if (bump_nina_trace) bump_nina_trace(activity);\n}\n\n"
                 "int HCINinaSpiTransportClass::begin()\n{\n  _receiveBatch.clear();\n  SpiDrv::begin();")
    replace_once(transport, "void HCINinaSpiTransportClass::end()\n{\n  BleDrv::bleEnd();",
                 "void HCINinaSpiTransportClass::end()\n{\n  _receiveBatch.clear();\n  BleDrv::bleEnd();")
    if args.nina_stop_reset:
        replace_once(transport, "  _receiveBatch.clear();\n  BleDrv::bleEnd();\n}",
                     """  _receiveBatch.clear();
#if defined(ARDUINO_NANO_RP2040_CONNECT)
  // GPIO/SPI teardown leaves NINA in reset without waiting for a BLE_END reply.
  SpiDrv::end();
#else
  BleDrv::bleEnd();
#endif
}""")
    replace_methods(transport, "int HCINinaSpiTransportClass::available()",
                    "size_t HCINinaSpiTransportClass::write(", """int HCINinaSpiTransportClass::available()
{
  return _receiveBatch.available([] {
    traceNina(1);
    const int count = BleDrv::bleAvailable();
    traceNina(0);
    return count;
  });
}

int HCINinaSpiTransportClass::peek()
{
  int res=-1;
  BleDrv::blePeek((uint8_t*)&res, 1);
  return res;
}

int HCINinaSpiTransportClass::read()
{
  if (!available()) return -1;
  return _receiveBatch.read([](uint8_t* data, size_t request) {
    traceNina(2);
    const int received = BleDrv::bleRead(data, request);
    traceNina(0);
    return received;
  });
}""")

    hci = utility / "HCI.cpp"
    replace_once(hci, "  HCITransport.lockForRead();\n  while (HCITransport.available()) {\n"
                 "    byte b = HCITransport.read();",
                 """  HCITransport.lockForRead();
  // Yield to the badge loop even if advertising reports keep arriving.
  // _recvIndex and the remaining byte credit survive between calls.
  const unsigned long pollStarted = micros();
  for (unsigned readBytes = 0;
       readBytes < 192 && (micros() - pollStarted) < 20000 && HCITransport.available();
       ++readBytes) {
    const int value = HCITransport.read();
    if (value < 0) break;
    const byte b = byte(value);""")

    changed = ("HCI.cpp", "HCINinaSpiTransport.cpp", "HCINinaSpiTransport.h", "BumpNinaRxBatch.h")
    def digest(path):
        return sha256(path.read_bytes()).hexdigest()

    base_utility = base / "src" / "utility"
    metadata = {
        "base": "ArduinoBLE 2.1.0",
        "max_spi_read_bytes": 1,
        "cached_availability_limit": 64,
        "availability_wire_order": "big-endian",
        "nano_rp2040_nina_reset_on_ble_end": args.nina_stop_reset,
        "poll_limits": {"bytes": 192, "microseconds_between_spi_operations": 20000},
        "upstream_sha256": {f: digest(base_utility / f) for f in changed[:-1]},
        "patched_sha256": {f: digest(utility / f) for f in changed},
    }
    (out / "bump-overlay.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print("Prepared ArduinoBLE overlay in", out)


if __name__ == "__main__":
    main()
