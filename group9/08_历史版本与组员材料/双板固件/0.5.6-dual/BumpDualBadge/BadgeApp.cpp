#include <Arduino.h>
#include <ArduinoBLE.h>
#include <Arduino_LSM6DSOX.h>
#include <ArduinoMqttClient.h>
#include <Arduino_SpiNINA.h>
#include <WiFiNINA.h>

#include "BadgeApp.h"
#include "BootEntropy.h"
#include "MotionDetector.h"
#include "Nearfield.h"
#include "WireAck.h"

extern "C" {
#include "hardware/structs/rosc.h"
#include "hardware/structs/watchdog.h"
#include "hardware/watchdog.h"
#include "pico/bootrom.h"
}

namespace {
constexpr char kFirmware[] = "0.5.6-dual";
constexpr uint32_t kWindowMs = 60000;
constexpr uint32_t kFirstPhaseMs = 28000;
constexpr uint32_t kMinPhaseRemainingMs = 3500;
constexpr uint32_t kScanPollIntervalMs = 5;
constexpr uint32_t kWatchdogMs = 8000;
constexpr uint32_t kGuardMagic = 0x42570000;
constexpr uint32_t kRetryMs = 15000;
constexpr uint32_t kNetworkTimeoutMs = 12000;
constexpr int kNearRssi = -70;
constexpr size_t kMaxEvents = 16;
constexpr size_t kMaxPeers = 8;
constexpr size_t kPayloadSize = 512;

enum class Phase { Idle, Advertise, Scan };
enum class Kind { Encounter, Infrared };
enum class GuardStage : uint8_t {
  None, DiscoveryStart, Serial, Infrared, Imu, BleStart, BleStop, BlePoll, RestoreLed, NinaVersion,
  SpiAvailable, SpiRead
};

struct Peer {
  uint8_t shortId[6];
  uint32_t firstAt;
  uint32_t lastAt;
  int32_t rssiSum;
  int8_t lastRssi;
  int8_t txPower;
  uint8_t flags;
  uint16_t samples;
};

struct Pending {
  Kind kind;
  uint32_t sequence;
  uint32_t occurredAt;
  char id[40];
  const char* source;
  bool exported;
  Peer peer;
  uint32_t discoveryStartedAt;
  uint32_t discoveryEndedAt;
  uint16_t peerCode;
};

Pending events[kMaxEvents] = {};
Peer peers[kMaxPeers] = {};
size_t eventCount = 0;
size_t peerCount = 0;
uint32_t nextSequence = 1;
uint32_t dropped = 0;

uint8_t radioShort[6] = {};
uint8_t frame[11] = {0xff, 0xff, 1, 0, 0, 0, 0, 0, 0, 1, 0xfc};
char shortId[13] = {};
char deviceId[18] = {};
char bootId[9] = {};
char encounterTopic[64] = {}, irTopic[64] = {}, statusTopic[64] = {}, ackTopic[64] = {};

Phase phase = Phase::Idle;
bool discovery = false;
bool scanningFirst = false;
bool plannedScan = false;
bool bleOpen = false;
bool paused = false;
uint8_t phaseCount = 0;
uint32_t windowStart = 0, phaseDue = 0, nextScanPoll = 0;
const char* discoverySource = "hardware";
uint16_t lastIrPeer = 0;
bool guardArmed = false, ninaVersionRead = false;
bool tracingBlePoll = false;
GuardStage resumedStage = GuardStage::None;
uint8_t resumedQueueDepth = 0;
char ninaVersion[20] = {};

bump::ShakeDetector shakeDetector;
bump::UptimeClock clockMs;
bool imuReady = false, streamImu = false;
float acceleration[3] = {};
uint32_t samples = 0, triggerCount = 0, lastImuStatus = 0;

class TimedWiFiClient : public WiFiClient {
 public:
  int connect(IPAddress address, uint16_t port) override {
    const uint32_t started = millis();
    result = WiFiClient::connect(address, port);
    elapsedMs = millis() - started;
    return result;
  }

  uint32_t elapsedMs = 0;
  int result = -1;
};

TimedWiFiClient network;
MqttClient mqtt(network);
char ssid[33] = {}, wifiPassword[65] = {}, mqttPassword[129] = {};
IPAddress brokerAddress;
bool brokerSet = false, useBridge = false;
uint16_t brokerPort = 18883;
bool uploadPending = false;
uint32_t nextUploadAt = 0;
const char* awaitingId = nullptr;
bool ackMatched = false;

char command[256] = {};
size_t commandLength = 0;
bool commandOverflow = false;

bool due(uint32_t now, uint32_t at) { return int32_t(now - at) >= 0; }

void signal(const char* type, const char* detail) {
  Serial.print("{\"type\":\""); Serial.print(type);
  Serial.print("\",\"detail\":\""); Serial.print(detail);
  Serial.print("\",\"uptime_ms\":"); Serial.print(millis()); Serial.println("}");
}

const char* guardName(GuardStage stage) {
  switch (stage) {
    case GuardStage::DiscoveryStart: return "discovery_start";
    case GuardStage::Serial: return "serial";
    case GuardStage::Infrared: return "infrared";
    case GuardStage::Imu: return "imu";
    case GuardStage::BleStart: return "ble_start";
    case GuardStage::BleStop: return "ble_stop";
    case GuardStage::BlePoll: return "ble_poll";
    case GuardStage::RestoreLed: return "restore_led";
    case GuardStage::NinaVersion: return "nina_version";
    case GuardStage::SpiAvailable: return "spi_available";
    case GuardStage::SpiRead: return "spi_read";
    default: return "none";
  }
}

void markGuard(GuardStage stage) {
  if (guardArmed)
    watchdog_hw->scratch[0] = kGuardMagic | (uint32_t(stage) << 8) | uint32_t(eventCount);
}

void armGuard(GuardStage stage) {
  guardArmed = true;
  markGuard(stage);
  watchdog_enable(kWatchdogMs, true);
}

void stopGuard() {
  hw_clear_bits(&watchdog_hw->ctrl, WATCHDOG_CTRL_ENABLE_BITS);
  watchdog_hw->scratch[0] = 0;
  guardArmed = false;
}

void readGuardRecovery() {
  const uint32_t marker = watchdog_hw->scratch[0];
  if (watchdog_enable_caused_reboot() && (marker & 0xffff0000) == kGuardMagic) {
    resumedStage = GuardStage((marker >> 8) & 0xff);
    resumedQueueDepth = uint8_t(marker);
    // A failed firmware-version probe must not cause a boot loop.
    ninaVersionRead = resumedStage == GuardStage::NinaVersion;
  }
  stopGuard();
}

void hexShort(const uint8_t value[6], char out[13]) {
  snprintf(out, 13, "%02x%02x%02x%02x%02x%02x",
           value[0], value[1], value[2], value[3], value[4], value[5]);
}

const char* topicFor(Kind kind) { return kind == Kind::Infrared ? irTopic : encounterTopic; }

void wireLog(const char* action, const char* source, const char* topic, const char* payload) {
  Serial.print("{\"type\":\"wire\",\"phase\":\""); Serial.print(action);
  Serial.print("\",\"source\":\""); Serial.print(source);
  Serial.print("\",\"topic\":\""); Serial.print(topic);
  Serial.print("\",\"payload\":"); Serial.print(payload); Serial.println("}");
}

void scheduleUpload(uint32_t delayMs) {
  if (!uploadPending || delayMs == 0) nextUploadAt = millis() + delayMs;
  uploadPending = true;
}

Pending* reserveEvent(Kind kind, const char* source) {
  if (!*deviceId || !*bootId) {
    signal("error", "device_identity_unavailable");
    return nullptr;
  }
  if (eventCount == kMaxEvents || nextSequence == 0) {
    ++dropped;
    signal("queue_rejected", "ram_queue_full_or_sequence_exhausted");
    return nullptr;
  }
  Pending& item = events[eventCount];
  item = Pending{};
  item.kind = kind;
  item.sequence = nextSequence;
  item.source = source;
  snprintf(item.id, sizeof(item.id), "%s_%s_%s_%08lx",
           kind == Kind::Infrared ? "ir" : "ev", deviceId + 5, bootId,
           (unsigned long)item.sequence);
  return &item;
}

bool makeEventPayload(const Pending& item, char (&out)[kPayloadSize]) {
  if (item.kind == Kind::Infrared) {
    const int length = snprintf(out, sizeof(out),
        "{\"device_id\":\"%s\",\"boot_id\":\"%s\",\"event_id\":\"%s\","
        "\"self_code16\":\"%04x\",\"peer_code16\":\"%04x\",\"uptime_ms\":%lu}",
        deviceId, bootId, item.id, nearfield::selfCode(), item.peerCode,
        (unsigned long)item.occurredAt);
    return length > 0 && size_t(length) < sizeof(out);
  }
  const Peer& peer = item.peer;
  char peerShort[13];
  hexShort(peer.shortId, peerShort);
  // Publish-time uptime lets the receiver recover the actual observation time on retry.
  const int length = snprintf(out, sizeof(out),
      "{\"schema\":1,\"device_id\":\"%s\",\"boot_id\":\"%s\",\"fw\":\"%s\","
      "\"batch_id\":\"bt_%s_%s_%08lx_0001\",\"self_short_id\":\"%s\","
      "\"uptime_ms\":%lu,\"time_source\":\"uptime\",\"events\":["
      "{\"event_id\":\"%s\",\"peer_short_id\":\"%s\",\"rssi_avg\":%ld,"
      "\"rssi_last\":%d,\"duration_s\":%lu,\"samples\":%u,\"flags\":%u,"
      "\"peer_tx_power\":%d,\"occurred_uptime_ms\":%lu,"
      "\"discovery_started_uptime_ms\":%lu,\"discovery_ended_uptime_ms\":%lu}]}",
      deviceId, bootId, kFirmware, deviceId + 5, bootId,
      (unsigned long)item.sequence, shortId, (unsigned long)millis(),
      item.id, peerShort, (long)(peer.rssiSum / peer.samples), peer.lastRssi,
      (unsigned long)((peer.lastAt - peer.firstAt) / 1000), peer.samples, peer.flags,
      peer.txPower, (unsigned long)peer.lastAt,
      (unsigned long)item.discoveryStartedAt, (unsigned long)item.discoveryEndedAt);
  return length > 0 && size_t(length) < sizeof(out);
}

void commitEvent(const Pending& item) {
  char payload[kPayloadSize];
  if (!makeEventPayload(item, payload)) {
    signal("error", "event_payload_too_long");
    return;
  }
  ++nextSequence;
  ++eventCount;
  markGuard(GuardStage::Serial);
  wireLog("queued", item.source, topicFor(item.kind), payload);
  scheduleUpload(0);
}

void queueInfrared(uint16_t peer) {
  Pending* item = reserveEvent(Kind::Infrared, "infrared");
  if (!item) return;
  item->peerCode = peer;
  item->occurredAt = millis();
  commitEvent(*item);
}

void queueEncounter(const Peer& peer) {
  Pending* item = reserveEvent(Kind::Encounter, discoverySource);
  if (!item) return;
  item->peer = peer;
  // The receiver needs the actual effective window even after an offline upload.
  item->discoveryStartedAt = windowStart;
  item->discoveryEndedAt = windowStart + kWindowMs;
  commitEvent(*item);
}

void removeHead(const char* origin) {
  Serial.print("{\"type\":\"queue_removed_after_application_ack\",\"origin\":\"");
  Serial.print(origin); Serial.print("\",\"event_id\":\""); Serial.print(events[0].id);
  Serial.print("\",\"queued\":"); Serial.print(eventCount - 1); Serial.println("}");
  --eventCount;
  memmove(events, events + 1, eventCount * sizeof(events[0]));
}

void processAdvertisement(BLEDevice device) {
  const uint32_t now = millis();
  if (!discovery || phase != Phase::Scan || now - windowStart >= kWindowMs) return;
  uint8_t received[sizeof(frame)];
  if (device.manufacturerData(received, sizeof(received)) != sizeof(received) ||
      received[0] != frame[0] || received[1] != frame[1] || received[2] != frame[2] ||
      !memcmp(received + 3, radioShort, sizeof(radioShort))) return;
  const int rssi = device.rssi();
  if (rssi < kNearRssi || rssi > 0) return;
  size_t i = 0;
  while (i < peerCount && memcmp(peers[i].shortId, received + 3, sizeof(radioShort))) ++i;
  if (i == peerCount) {
    if (peerCount == kMaxPeers) return;
    ++peerCount;
    peers[i] = Peer{};
    memcpy(peers[i].shortId, received + 3, sizeof(radioShort));
    peers[i].firstAt = now;
  }
  Peer& peer = peers[i];
  if (peer.samples && now - peer.lastAt < 400) return;
  peer.lastAt = now;
  peer.rssiSum += rssi;
  peer.lastRssi = rssi;
  peer.flags = received[9];
  peer.txPower = int8_t(received[10]);
  ++peer.samples;
  if (peer.samples <= 2) {
    char heard[13];
    hexShort(peer.shortId, heard);
    Serial.print("{\"type\":\"ble_observation\",\"peer_short_id\":\""); Serial.print(heard);
    Serial.print("\",\"rssi\":"); Serial.print(rssi);
    Serial.print(",\"samples\":"); Serial.print(peer.samples);
    Serial.print(",\"uptime_ms\":"); Serial.print(now); Serial.println("}");
  }
}

void stopBle() {
  if (!bleOpen) return;
  markGuard(GuardStage::BleStop);
  signal("radio", "ble_end_begin");
  // Host-side BLE cleanup calls the local transport's physical NINA reset.
  BLE.end();
  bleOpen = false;
  phase = Phase::Idle;
  signal("radio", "ble_end_done");
}

void startPhase(bool scan) {
  stopBle();
  markGuard(GuardStage::BleStart);
  delay(350);
  if (millis() - windowStart >= kWindowMs) return;
  signal("radio", scan ? "ble_scan_begin" : "ble_advertise_begin");
  if (!BLE.begin()) {
    // ArduinoBLE's failed begin path has already called end().
    signal("error", "ble_begin_failed");
    phaseDue = millis() + 700;
    return;
  }
  bleOpen = true;
  phase = scan ? Phase::Scan : Phase::Advertise;
  bool started = false;
  if (scan) {
    BLE.setEventHandler(BLEDiscovered, processAdvertisement);
    started = BLE.scan(true);
  } else {
    // Keep every AD field in the primary packet; no scan-response exchange is needed.
    BLEAdvertisingData data, noScanResponse;
    const bool named = data.setLocalName("Bump");
    BLE.setAdvertisingData(data);
    BLE.setScanResponseData(noScanResponse);
    BLE.setConnectable(false);
    BLE.setAdvertisingInterval(100);
    started = named && BLE.setAdvertisedServiceUuid("A5B0") &&
              BLE.setManufacturerData(frame, sizeof(frame)) && BLE.advertise();
  }
  if (!started) {
    signal("error", scan ? "ble_scan_failed" : "ble_advertise_failed");
    stopBle();
    phaseDue = millis() + 700;
    return;
  }
  plannedScan = !scan;
  ++phaseCount;
  const uint32_t ready = millis();
  phaseDue = phaseCount == 1 ? ready + kFirstPhaseMs : windowStart + kWindowMs;
  nextScanPoll = ready;
  signal("radio", scan ? "ble_scan_ready" : "ble_advertise_ready");
}

void finishDiscovery(bool complete) {
  if (!discovery) return;
  discovery = false;
  stopBle();
  if (complete) {
    for (size_t i = 0; i < peerCount; ++i) {
      // Two time-separated RSSI observations, then preserve the measured span.
      if (peers[i].samples >= 2 && peers[i].lastAt - peers[i].firstAt >= 3000)
        queueEncounter(peers[i]);
    }
  }
  markGuard(GuardStage::RestoreLed);
  nearfield::restoreLed();
  markGuard(GuardStage::Serial);
  Serial.print("{\"type\":\"discovery_end\",\"source\":\""); Serial.print(discoverySource);
  Serial.print("\",\"complete\":"); Serial.print(complete ? "true" : "false");
  Serial.print(",\"observed_peers\":"); Serial.print(peerCount);
  Serial.print(",\"uptime_ms\":"); Serial.print(millis()); Serial.println("}");
  peerCount = 0;
  stopGuard();
}

void startDiscovery(const char* source) {
  if (!*bootId) {
    signal("discovery_ignored", "boot_entropy_unavailable");
    return;
  }
  if (discovery || paused || nearfield::handshakeBusy()) {
    signal("discovery_ignored", paused ? "paused" : "discovery_or_ir_busy");
    return;
  }
  discovery = true;
  discoverySource = source;
  windowStart = phaseDue = millis();
  peerCount = 0;
  phaseCount = 0;
  plannedScan = scanningFirst;
  armGuard(GuardStage::DiscoveryStart);
  nearfield::suspendLed();
  SpiDrv::end();
  markGuard(GuardStage::Serial);
  Serial.print("{\"type\":\"discovery_start\",\"source\":\""); Serial.print(source);
  Serial.print("\",\"self_short_id\":\""); Serial.print(shortId);
  Serial.print("\",\"first_phase\":\""); Serial.print(plannedScan ? "scan" : "advertise");
  Serial.print("\",\"window_ms\":"); Serial.print(kWindowMs);
  Serial.print(",\"uptime_ms\":"); Serial.print(windowStart); Serial.println("}");
}

void pollDiscovery() {
  if (!discovery) return;
  uint32_t now = millis();
  if (now - windowStart >= kWindowMs) {
    finishDiscovery(true);
    return;
  }
  if (phaseCount < 2 && due(now, phaseDue) &&
      now - windowStart < kWindowMs - kMinPhaseRemainingMs) {
    startPhase(plannedScan);
    now = millis();
  }
  if (phase == Phase::Scan && due(now, nextScanPoll)) {
    nextScanPoll = now + kScanPollIntervalMs;
    markGuard(GuardStage::BlePoll);
    // ArduinoBLE 2.1.0 BLE.available() already polls HCI and dispatches BLEDiscovered.
    tracingBlePoll = true;
    BLEDevice device = BLE.available();
    tracingBlePoll = false;
    if (device) processAdvertisement(device);
  }
}

void sampleMotion() {
  if (!imuReady || !IMU.accelerationAvailable()) return;
  if (!IMU.readAcceleration(acceleration[0], acceleration[1], acceleration[2])) return;
  ++samples;
  if (shakeDetector.sample(acceleration[0], acceleration[1], acceleration[2], clockMs.update(millis()))) {
    ++triggerCount;
    signal("motion_trigger", "hardware");
    startDiscovery("hardware");
  }
}

bool networkConfigured() {
  return *deviceId && *bootId && *ssid && *wifiPassword && *mqttPassword && brokerSet && !useBridge;
}

void readNinaVersionOnce() {
  if (ninaVersionRead || discovery || bleOpen || nearfield::active()) return;
  ninaVersionRead = true;
  armGuard(GuardStage::NinaVersion);
  const String version = WiFi.firmwareVersion();
  stopGuard();
  if (version.length() > 0 && version.length() < sizeof(ninaVersion) &&
      strspn(version.c_str(), "0123456789.-") == version.length())
    strcpy(ninaVersion, version.c_str());
}

bool publish(const char* topic, const char* payload) {
  const size_t length = strlen(payload);
  return mqtt.beginMessage(topic, (unsigned long)length, false, 1) &&
         mqtt.write((const uint8_t*)payload, length) == length && mqtt.endMessage() == 1;
}

void makeStatus(char out[256], const char* state) {
  snprintf(out, 256,
      "{\"schema\":1,\"device_id\":\"%s\",\"boot_id\":\"%s\",\"fw\":\"%s\","
      "\"self_short_id\":\"%s\",\"state\":\"%s\",\"uptime_s\":%lu,"
      "\"queue_depth\":%u,\"events_dropped\":%lu}",
      deviceId, bootId, kFirmware, shortId, state, (unsigned long)(millis() / 1000),
      (unsigned)eventCount, (unsigned long)dropped);
}

bool sendStatus(const char* state) {
  char payload[256];
  makeStatus(payload, state);
  wireLog("transmit", "presence", statusTopic, payload);
  return publish(statusTopic, payload);
}

void receiveAck(int size) {
  char payload[128];
  if (size <= 0 || size >= int(sizeof(payload))) {
    signal("error", "application_ack_size_invalid");
    mqtt.stop();
    return;
  }
  const bool topicMatches = mqtt.messageTopic() == ackTopic && !mqtt.messageRetain();
  int n = 0;
  uint32_t started = millis();
  while (n < size && millis() - started < 600) {
    if (network.available()) {
      const int byte = mqtt.read();
      if (byte >= 0) payload[n++] = char(byte);
    } else {
      delay(1);
    }
  }
  payload[n] = '\0';
  const bool matches = n == size && !memchr(payload, '\0', n) && topicMatches &&
                       bump_ack::matches(payload, awaitingId);
  if (matches) ackMatched = true;
  signal("application_ack", matches ? "matched" : "ignored");
  if (n != size) mqtt.stop();
}

void logSyncStage(const char* stage, uint32_t elapsed, int result) {
  char message[145];
  snprintf(message, sizeof(message),
           "{\"type\":\"sync_stage\",\"stage\":\"%s\",\"elapsed_ms\":%lu,"
           "\"result\":%d,\"uptime_ms\":%lu}",
           stage, (unsigned long)elapsed, result, (unsigned long)millis());
  Serial.println(message);
}

void logWifiLink() {
  const uint32_t started = millis();
  const IPAddress address = WiFi.localIP();
  const long rssi = WiFi.RSSI();
  char message[165];
  snprintf(message, sizeof(message),
           "{\"type\":\"wifi_link\",\"local_ip\":\"%u.%u.%u.%u\","
           "\"rssi_dbm\":%ld,\"elapsed_ms\":%lu,\"uptime_ms\":%lu}",
           unsigned(address[0]), unsigned(address[1]), unsigned(address[2]),
           unsigned(address[3]), rssi, (unsigned long)(millis() - started),
           (unsigned long)millis());
  Serial.println(message);
}

void syncWindow() {
  if (!networkConfigured() || discovery || nearfield::handshakeBusy()) return;
  const uint32_t syncStarted = millis();
  nearfield::suspendLed();
  SpiDrv::end();
  signal("radio", "wifi_begin");
  WiFi.setTimeout(9000);
  const uint32_t wifiStarted = millis();
  const int wifiResult = WiFi.begin(ssid, wifiPassword);
  const bool wifiOk = wifiResult == WL_CONNECTED;
  logSyncStage("wifi_associate", millis() - wifiStarted, wifiResult);
  signal("radio", wifiOk ? "wifi_connected" : "wifi_failed");
  bool presenceOk = false;
  if (wifiOk) {
    mqtt.setId(deviceId);
    mqtt.setUsernamePassword(deviceId, mqttPassword);
    mqtt.setCleanSession(true);
    mqtt.setConnectionTimeout(kNetworkTimeoutMs);
    network.setConnectionTimeout(kNetworkTimeoutMs);
    network.elapsedMs = 0;
    network.result = -1;
    const uint32_t connectStarted = millis();
    const bool connected = mqtt.connect(brokerAddress, brokerPort);
    const uint32_t connectElapsed = millis() - connectStarted;
    logSyncStage("tcp_connect", network.elapsedMs, network.result);
    logSyncStage("mqtt_connect", connectElapsed - network.elapsedMs, mqtt.connectError());
    if (connected) {
      signal("sync", "broker_connected");
      const uint32_t subscribeStarted = millis();
      const bool subscribed = mqtt.subscribe(ackTopic, 1);
      logSyncStage("mqtt_subscribe", millis() - subscribeStarted, mqtt.subscribeQoS());
      signal("sync", subscribed ? "ack_subscribed" : "ack_subscription_failed");
      const uint32_t presenceStarted = millis();
      presenceOk = sendStatus("online");
      logSyncStage("presence_online", millis() - presenceStarted, presenceOk);
      const uint32_t syncAt = millis();
      while (subscribed && eventCount && mqtt.connected() && millis() - syncAt < 20000) {
        Pending& item = events[0];
        char payload[kPayloadSize];
        if (!makeEventPayload(item, payload)) {
          signal("error", "event_payload_too_long");
          break;
        }
        wireLog("transmit", item.source, topicFor(item.kind), payload);
        ackMatched = false;
        const size_t length = strlen(payload);
        const uint32_t publishStarted = millis();
        const bool written = mqtt.beginMessage(topicFor(item.kind), (unsigned long)length, false, 1) &&
                             mqtt.write((const uint8_t*)payload, length) == length;
        awaitingId = written ? item.id : nullptr;
        const bool brokerAck = written && mqtt.endMessage() == 1;
        logSyncStage("mqtt_puback", millis() - publishStarted, brokerAck);
        signal("mqtt_puback", brokerAck ? "received" : "not_received");
        if (!written) {
          signal("sync", "event_write_failed_queue_retained");
          break;
        }
        const uint32_t waited = millis();
        while (!ackMatched && mqtt.connected() && millis() - waited < kNetworkTimeoutMs) {
          mqtt.poll();
          delay(1);
        }
        logSyncStage("application_ack_wait", millis() - waited, ackMatched);
        awaitingId = nullptr;
        if (!ackMatched) {
          signal("sync", "application_ack_missing_queue_retained");
          break;
        }
        removeHead("wifi_mqtt");
      }
      if (mqtt.connected()) {
        const uint32_t offlineStarted = millis();
        const bool offlineOk = sendStatus("offline");
        logSyncStage("presence_offline", millis() - offlineStarted, offlineOk);
      }
    } else {
      Serial.print("{\"type\":\"sync\",\"detail\":\"broker_connect_failed\",\"code\":");
      Serial.print(mqtt.connectError()); Serial.println("}");
    }
    const uint32_t stopStarted = millis();
    mqtt.stop();
    logSyncStage("mqtt_stop", millis() - stopStarted, 1);
    logWifiLink();
    WiFi.disconnect();
  }
  WiFi.end();
  SpiDrv::end();
  delay(350);
  nearfield::restoreLed();
  uploadPending = eventCount || !presenceOk;
  nextUploadAt = millis() + kRetryMs;
  logSyncStage("sync_total", millis() - syncStarted, int(eventCount));
  Serial.print("{\"type\":\"sync_complete\",\"transport\":\"wifi_mqtt_tcp\",\"queued\":");
  Serial.print(eventCount); Serial.println("}");
}

void printStatus() {
  char state[850];
  const uint32_t now = millis();
  const uint32_t age = now - windowStart;
  snprintf(state, sizeof(state),
      "{\"type\":\"status\",\"firmware\":\"BumpDualBadge\",\"fw\":\"%s\","
      "\"device_id\":\"%s\",\"boot_id\":\"%s\",\"self_short_id\":\"%s\","
      "\"self_code16\":\"%04x\",\"peer_code16\":\"%04x\",\"ir_paired\":%s,"
      "\"uptime_ms\":%lu,\"paused\":%s,\"discovery\":%s,\"ble_phase\":\"%s\","
      "\"window_remaining_ms\":%lu,\"queued\":%u,\"dropped\":%lu,"
      "\"imu_ready\":%s,\"imu_samples\":%lu,\"motion_triggers\":%lu,"
      "\"network_configured\":%s,\"transport\":\"%s\","
      "\"nina_version\":\"%s\",\"last_watchdog_stage\":\"%s\",\"pre_reset_ram_queued\":%u",
      kFirmware, deviceId, bootId, shortId, nearfield::selfCode(), nearfield::peerCode(),
      nearfield::paired() ? "true" : "false", (unsigned long)now,
      paused ? "true" : "false", discovery ? "true" : "false",
      phase == Phase::Scan ? "scan" : phase == Phase::Advertise ? "advertise" : "idle",
      (unsigned long)(discovery && age < kWindowMs ? kWindowMs - age : 0),
      (unsigned)eventCount, (unsigned long)dropped, imuReady ? "true" : "false",
      (unsigned long)samples, (unsigned long)triggerCount, networkConfigured() ? "true" : "false",
      useBridge ? "serial_bridge" : "wifi_mqtt_tcp", ninaVersion, guardName(resumedStage),
      unsigned(resumedQueueDepth));
  Serial.print(state);
  if (samples && isfinite(acceleration[0]) && isfinite(acceleration[1]) && isfinite(acceleration[2])) {
    Serial.print(",\"accel_g\":["); Serial.print(acceleration[0], 5);
    Serial.print(','); Serial.print(acceleration[1], 5);
    Serial.print(','); Serial.print(acceleration[2], 5); Serial.print(']');
  }
  Serial.println("}");
}

void pauseDiscovery() {
  paused = !paused;
  if (paused) finishDiscovery(false);
  signal("privacy", paused ? "discovery_paused" : "discovery_resumed");
}

void pressBlue() {
  finishDiscovery(false);
  nearfield::blue();
}

void pressWhite() {
  const bool endingPairing = nearfield::active();
  finishDiscovery(false);
  nearfield::white();
  if (!endingPairing) pauseDiscovery();
}

bool privateBroker(const char* value) {
  IPAddress address;
  if (!address.fromString(value)) return false;
  const bool allowed = address[0] == 10 ||
      (address[0] == 172 && address[1] >= 16 && address[1] <= 31) ||
      (address[0] == 192 && address[1] == 168);
  if (!allowed) return false;
  brokerAddress = address;
  return true;
}

bool copyConfig(char* target, size_t capacity, const char* value) {
  if (!*value || strlen(value) >= capacity) return false;
  strcpy(target, value);
  return true;
}

void configure(char* field) {
  char* value = strchr(field, ' ');
  if (!value) {
    signal("error", "set_requires_key_and_value");
    return;
  }
  *value++ = '\0';
  bool ok = false;
  if (!strcmp(field, "ssid")) {
    ok = copyConfig(ssid, sizeof(ssid), value);
  } else if (!strcmp(field, "wifi_password")) {
    ok = copyConfig(wifiPassword, sizeof(wifiPassword), value);
  } else if (!strcmp(field, "mqtt_password")) {
    ok = copyConfig(mqttPassword, sizeof(mqttPassword), value);
  } else if (!strcmp(field, "broker_host")) {
    ok = privateBroker(value);
    if (ok) brokerSet = true;
  } else if (!strcmp(field, "broker_port")) {
    char* tail = nullptr;
    unsigned long port = strtoul(value, &tail, 10);
    ok = *value >= '0' && *value <= '9' && !*tail && port >= 1 && port <= 65535;
    if (ok) brokerPort = uint16_t(port);
  } else if (!strcmp(field, "ble_role")) {
    ok = !strcmp(value, "a") || !strcmp(value, "b");
    if (ok) scanningFirst = (*value == 'a');
  } else if (!strcmp(field, "transport")) {
    ok = !strcmp(value, "serial_bridge") || !strcmp(value, "wifi_mqtt");
    if (ok) useBridge = !strcmp(value, "serial_bridge");
  }
  signal(ok ? "configuration_set" : "configuration_rejected",
         ok ? field : "unknown_or_invalid_field");
  if (ok && networkConfigured()) scheduleUpload(0);
}

void dumpQueue() {
  if (!*deviceId) return;
  char payload[256];
  makeStatus(payload, "online");
  wireLog("dump", "presence", statusTopic, payload);
  for (size_t i = 0; i < eventCount; ++i) {
    Pending& item = events[i];
    char itemPayload[kPayloadSize];
    if (makeEventPayload(item, itemPayload)) {
      wireLog("dump", item.source, topicFor(item.kind), itemPayload);
      if (useBridge) item.exported = true;
    }
  }
  signal("queue_dump_complete", "all_pending_events_exported");
}

void handleCommand(char* line) {
  if (!strncmp(line, "SET ", 4)) configure(line + 4);
  else if (!strcmp(line, "status") || !strcmp(line, "s")) { printStatus(); nearfield::printStatus(); }
  else if (!strcmp(line, "discover")) startDiscovery("serial_diagnostic");
  else if (!strcmp(line, "pause")) pauseDiscovery();
  else if (!strcmp(line, "b")) pressBlue();
  else if (!strcmp(line, "w")) pressWhite();
  else if (!strcmp(line, "sync")) {
    if (networkConfigured()) scheduleUpload(0);
    else signal("error", "live_network_configuration_missing");
  } else if (!strcmp(line, "dump")) dumpQueue();
  else if (!strncmp(line, "ACK ", 4)) {
    if (useBridge && eventCount && events[0].exported && bump_ack::matches(line + 4, events[0].id))
      removeHead("serial_bridge");
    else signal("application_ack", "ignored");
  } else if (!strcmp(line, "i") || !strcmp(line, "q")) {
    streamImu = *line == 'i';
    printStatus();
  } else if (!strcmp(line, "t")) {
    signal("bootloader", "ram_queue_and_credentials_will_be_lost");
    if (guardArmed) stopGuard();
    Serial.flush();
    delay(100);
    reset_usb_boot(1u << digitalPinToPinName(LED_BUILTIN), 0);
  } else if (!strcmp(line, "r")) {
    signal("restart", "ram_queue_and_credentials_will_be_lost");
    if (guardArmed) stopGuard();
    Serial.flush();
    delay(100);
    watchdog_reboot(0, 0, 100);
    while (true) delay(1);
  } else if (*line) signal("error", "unknown_command");
}

void pollSerial() {
  for (unsigned i = 0; i < 256 && Serial.available(); ++i) {
    const char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      command[commandLength] = '\0';
      if (commandOverflow) signal("error", "command_line_too_long");
      else handleCommand(command);
      memset(command, 0, sizeof(command));
      commandLength = 0;
      commandOverflow = false;
    } else if (commandLength + 1 < sizeof(command) && !commandOverflow) {
      command[commandLength++] = c;
    } else {
      commandOverflow = true;
    }
  }
}

void makeIdentity() {
  uint8_t mac[6] = {};
  WiFi.macAddress(mac);
  const uint8_t zeros[6] = {};
  const uint8_t ones[6] = {255, 255, 255, 255, 255, 255};
  if (memcmp(mac, zeros, sizeof(mac)) && memcmp(mac, ones, sizeof(mac))) {
    char macHex[13];
    hexShort(mac, macHex);
    snprintf(deviceId, sizeof(deviceId), "bump-%s", macHex);
  }
  uint8_t entropy[boot_identity::kBytes];
  if (!boot_identity::collect([](unsigned delayUs) {
        delayMicroseconds(delayUs);
        return (rosc_hw->randombit & 1) != 0;
      }, entropy)) {
    signal("error", "rp2040_rosc_boot_entropy_unavailable");
    return;
  }
  memcpy(radioShort, entropy, sizeof(radioShort));
  hexShort(radioShort, shortId);
  memcpy(frame + 3, radioShort, sizeof(radioShort));
  uint32_t boot = 0;
  for (unsigned i = sizeof(radioShort); i < sizeof(entropy); ++i) boot = (boot << 8) | entropy[i];
  snprintf(bootId, sizeof(bootId), "%08lx", (unsigned long)boot);
  snprintf(encounterTopic, sizeof(encounterTopic), "bump/v1/device/%s/encounters", deviceId);
  snprintf(irTopic, sizeof(irTopic), "bump/v1/device/%s/ir", deviceId);
  snprintf(statusTopic, sizeof(statusTopic), "bump/v1/device/%s/status", deviceId);
  snprintf(ackTopic, sizeof(ackTopic), "bump/v1/device/%s/ack", deviceId);
}
}  // namespace

extern "C" void bump_nina_trace(uint8_t activity) {
  if (!tracingBlePoll) return;
  markGuard(activity == 1 ? GuardStage::SpiAvailable :
            activity == 2 ? GuardStage::SpiRead : GuardStage::BlePoll);
}

void badgeSetup() {
  readGuardRecovery();
  Serial.begin(115200);
  const uint32_t waiting = millis();
  while (!Serial && millis() - waiting < 1500) delay(10);
  nearfield::begin();   // Seeds random with time, the physical IR device code and A0.
  makeIdentity();
  scanningFirst = (nearfield::selfCode() & 0x0020) == 0;
  imuReady = IMU.begin();
  mqtt.onMessage(receiveAck);
  signal("boot", "BumpDualBadge");
  if (resumedStage != GuardStage::None) {
    Serial.print("{\"type\":\"watchdog_recovery\",\"stage\":\""); Serial.print(guardName(resumedStage));
    Serial.print("\",\"pre_reset_ram_queued\":"); Serial.print(resumedQueueDepth);
    Serial.println(",\"ram_queue_lost\":true,\"network_configuration_lost\":true}");
  }
  printStatus();
}

void badgeLoop() {
  markGuard(GuardStage::Serial);
  pollSerial();
  markGuard(GuardStage::Infrared);
  const uint8_t button = nearfield::buttons();
  if (button & nearfield::kWhite) pressWhite();
  else if (button & nearfield::kBlue) pressBlue();
  nearfield::tick();
  const uint16_t peer = nearfield::paired() ? nearfield::peerCode() : 0;
  if (peer && peer != lastIrPeer) queueInfrared(peer);
  lastIrPeer = peer;
  markGuard(GuardStage::Imu);
  sampleMotion();
  pollDiscovery();
  readNinaVersionOnce();
  const uint32_t now = millis();
  if (uploadPending && networkConfigured() && due(now, nextUploadAt)) syncWindow();
  if (streamImu && now - lastImuStatus >= 1000) {
    lastImuStatus = now;
    markGuard(GuardStage::Serial);
    printStatus();
  }
  if (guardArmed) watchdog_update();
  delay(1);
}
