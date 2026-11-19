#include <Arduino.h>
#include <ArduinoBLE.h>
#include <Arduino_SpiNINA.h>

extern "C" {
#include "hardware/structs/watchdog.h"
#include "hardware/watchdog.h"
#include "pico/bootrom.h"
}

namespace {
#ifdef BUMP_PROBE_BATCHED
constexpr char kVariant[] = "BumpNinaProbe/batch";
#else
constexpr char kVariant[] = "BumpNinaProbe/stock";
#endif
constexpr uint32_t kWindowMs = 60000, kWatchdogMs = 8000, kMagic = 0x42504e00;
enum class Stage : uint8_t { Idle, Start, Poll, SpiAvailable, SpiRead, Stop, Serial };
enum class Role { Idle, Scan, Advertise };
Role role = Role::Idle;
Stage lastReset = Stage::Idle;
uint32_t started = 0, nextPoll = 0, lastReport = 0, maxPollUs = 0;
uint32_t reports = 0, samples = 0, firstSample = 0, lastSample = 0;
uint8_t peerShort[6] = {};
int lastRssi = 0;
bool tracing = false;

const char* name(Stage stage) {
  switch (stage) {
    case Stage::Start: return "ble_start";
    case Stage::Poll: return "ble_poll";
    case Stage::SpiAvailable: return "spi_available";
    case Stage::SpiRead: return "spi_read";
    case Stage::Stop: return "ble_stop";
    case Stage::Serial: return "serial";
    default: return "none";
  }
}

void stage(Stage s) {
  if (role != Role::Idle) watchdog_hw->scratch[0] = kMagic | uint8_t(s);
}

void stopWatchdog() {
  hw_clear_bits(&watchdog_hw->ctrl, WATCHDOG_CTRL_ENABLE_BITS);
  watchdog_hw->scratch[0] = 0;
}

void status(const char* type) {
  const char* mode = role == Role::Scan ? "scan" : role == Role::Advertise ? "advertise" : "idle";
  const uint32_t span = samples ? lastSample - firstSample : 0;
  char record[420];
  snprintf(record, sizeof(record),
      "{\"type\":\"%s\",\"firmware\":\"%s\",\"role\":\"%s\",\"uptime_ms\":%lu,"
      "\"elapsed_ms\":%lu,\"reports\":%lu,\"samples\":%lu,\"span_ms\":%lu,"
      "\"last_rssi\":%d,\"qualified\":%s,\"max_poll_us\":%lu,\"last_watchdog_stage\":\"%s\"}",
      type,kVariant,mode,(unsigned long)millis(),(unsigned long)(started ? millis()-started : 0),
      (unsigned long)reports,(unsigned long)samples,(unsigned long)span,lastRssi,
      samples >= 2 && span >= 3000 ? "true" : "false",(unsigned long)maxPollUs,name(lastReset));
  Serial.println(record);
}

void receive(BLEDevice device) {
  const uint32_t observed = millis();
  if (role != Role::Scan || observed - started >= kWindowMs) return;
  ++reports;
  uint8_t data[11];
  const int rssi = device.rssi();
  if (rssi < -70 || rssi > 0 || device.manufacturerData(data, sizeof(data)) != sizeof(data) ||
      data[0] != 0xff || data[1] != 0xff || data[2] != 1) return;
  if (samples && (observed - lastSample < 400 || memcmp(peerShort, data + 3, sizeof(peerShort))))
    return;
  if (!samples) {
    firstSample = observed;
    memcpy(peerShort, data + 3, sizeof(peerShort));
  }
  ++samples;
  lastSample = observed;
  lastRssi = rssi;
}

void stop(const char* result) {
  if (role == Role::Idle) return;
  stage(Stage::Stop);
  BLE.end();
  SpiDrv::end();
  role = Role::Idle;
  stopWatchdog();
  status(result);
}

void start(Role requested) {
  if (role != Role::Idle) {
    status("probe_busy");
    return;
  }
  started = lastReport = nextPoll = millis();
  reports = samples = maxPollUs = 0;
  lastRssi = 0;
  role = requested;
  stage(Stage::Start);
  watchdog_enable(kWatchdogMs, true);
  if (!BLE.begin()) {
    SpiDrv::end();
    role = Role::Idle;
    stopWatchdog();
    status("probe_begin_failed");
    return;
  }
  bool ready;
  if (role == Role::Scan) {
    BLE.setEventHandler(BLEDiscovered, receive);
    ready = BLE.scan(true);
  } else {
    static const uint8_t frame[] = {0xff, 0xff, 1, 0x42, 0x75, 0x6d, 0x70, 0x50, 0x72, 1, 0xfc};
    BLEAdvertisingData data, noResponse;
    const bool named = data.setLocalName("Bump");
    BLE.setAdvertisingData(data);
    BLE.setScanResponseData(noResponse);
    BLE.setConnectable(false);
    BLE.setAdvertisingInterval(100);
    ready = named && BLE.setAdvertisedServiceUuid("A5B0") &&
            BLE.setManufacturerData(frame, sizeof(frame)) && BLE.advertise();
  }
  if (ready) status("probe_start");
  else stop("probe_start_failed");
}

void command(const char* line) {
  if (!strcmp(line, "scan")) start(Role::Scan);
  else if (!strcmp(line, "advertise")) start(Role::Advertise);
  else if (!strcmp(line, "status")) status("status");
  else if (!strcmp(line, "stop")) stop("probe_cancelled");
  else if (!strcmp(line, "t")) {
    stop("probe_cancelled");
    reset_usb_boot(1u << digitalPinToPinName(LED_BUILTIN), 0);
  }
}
}  // namespace

extern "C" void bump_nina_trace(uint8_t activity) {
  if (!tracing) return;
  stage(activity == 1 ? Stage::SpiAvailable : activity == 2 ? Stage::SpiRead : Stage::Poll);
}

void probeSetup() {
  const uint32_t word = watchdog_hw->scratch[0];
  if (watchdog_enable_caused_reboot() && (word & 0xffffff00) == kMagic)
    lastReset = Stage(word & 0xff);
  stopWatchdog();
  Serial.begin(115200);
  uint32_t start = millis();
  while (!Serial && millis() - start < 1500) delay(10);
  status("probe_boot");
}

void probeLoop() {
  static char line[20];
  static unsigned count = 0;
  stage(Stage::Serial);
  while (Serial.available()) {
    const char ch = Serial.read();
    if (ch == '\r') continue;
    if (ch == '\n') {
      line[count] = '\0';
      command(line);
      count = 0;
    } else if (count + 1 < sizeof(line)) line[count++] = ch;
  }
  const uint32_t now = millis();
  if (role != Role::Idle && now - started >= kWindowMs) stop("probe_complete");
  if (role == Role::Scan && int32_t(now - nextPoll) >= 0) {
    nextPoll = now + 5;
    stage(Stage::Poll);
    tracing = true;
    const uint32_t before = micros();
    BLEDevice device = BLE.available();
    const uint32_t elapsed = micros() - before;
    tracing = false;
    if (elapsed > maxPollUs) maxPollUs = elapsed;
    if (device) receive(device);
  }
  if (role != Role::Idle) {
    if (millis() - lastReport >= 1000) {
      lastReport = millis();
      stage(Stage::Serial);
      status("probe_progress");
    }
    watchdog_update();
  }
  delay(1);
}
