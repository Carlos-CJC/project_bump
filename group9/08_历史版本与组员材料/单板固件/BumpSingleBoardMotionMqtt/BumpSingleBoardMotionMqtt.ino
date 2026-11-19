// Independent single-board motion and LAN MQTT experiment; no fabricated peers.
#include <Arduino_LSM6DSOX.h>
#include <ArduinoBLE.h>
#include <Arduino_SpiNINA.h>
#include <WiFiNINA.h>
#include <ArduinoMqttClient.h>
#include <ctype.h>
#include "BumpCore.h"
#include "MotionAck.h"
extern "C" {
#include "hardware/watchdog.h"
#include "pico/bootrom.h"
}

enum AppState { IDLE, DISCOVERY, SYNC };
enum RadioMode { RADIO_NONE, RADIO_BLE, RADIO_WIFI };
AppState appState = IDLE;
RadioMode radioMode = RADIO_NONE;
bump::ShakeDetector realDetector, injectedDetector;
bump::UptimeClock uptimeClock;
WiFiClient network;
MqttClient mqtt(network);
const char* eventTopic = "bump/v1/device/badge-a/events";
const char* ackTopic = "bump/v1/device/badge-a/ack";
const uint64_t windowMs = 60000;
char ssid[33] = {}, wifiPassword[65] = {}, brokerHost[65] = {};
char mqttUser[65] = "badge-a", mqttPassword[129] = {}, bootId[37] = {};
char transport[14] = "wifi_mqtt";
uint16_t brokerPort = 1883;
struct QueuedEvent { uint32_t seq; char payload[512]; };
QueuedEvent queue[16];
size_t queueCount = 0;
uint32_t nextSeq = 1, overflowCount = 0, awaitingSeq = 0, bridgeExportedThrough = 0;
bool imuReady = false, streamImu = false, advertising = false, ackReceived = false;
bool injectionActive = false, lineOverflow = false;
float latestAccel[3] = {};
uint64_t samples = 0, firstSampleAt = 0, lastSampleAt = 0, maxGapMs = 0;
uint64_t windowStartedAt = 0, injectionStartedAt = 0, lastInjectionAt = 0;
uint32_t hardwareTriggers = 0, acceptedHardwareTriggers = 0, injectedTriggers = 0;
unsigned long lastTelemetry = 0;
char commandLine[256];
size_t commandLength = 0;
uint8_t manufacturerFrame[11] = {0xff, 0xff, 1, 0, 0, 0, 0, 0, 0, 1, 0xfc};

uint64_t nowMs() { return uptimeClock.update(millis()); }
const char* stateName() {
  return appState == DISCOVERY ? "DISCOVERY" : appState == SYNC ? "SYNC" : "IDLE";
}
const char* radioName() {
  return radioMode == RADIO_BLE ? "BLE" : radioMode == RADIO_WIFI ? "WIFI" : "NONE";
}
void reportError(const char* reason) {
  Serial.print("{\"type\":\"error\",\"reason\":\""); Serial.print(reason); Serial.println("\"}");
}
void radioStage(const char* operation, const char* phase) {
  Serial.print("{\"type\":\"radio_stage\",\"operation\":\""); Serial.print(operation);
  Serial.print("\",\"phase\":\""); Serial.print(phase);
  Serial.print("\",\"uptime_ms\":"); Serial.print(millis()); Serial.println("}");
  Serial.flush();
}
void resetNinaDriver() {
  radioStage("nina_driver_reset", "begin");
  SpiDrv::end();  // Holds only the onboard NINA in reset; next API call reinitializes it.
  radioStage("nina_driver_reset", "end");
}
void stopWifi() {
  if (radioMode != RADIO_WIFI) return;
  radioStage("wifi_disconnect", "begin"); WiFi.disconnect(); radioStage("wifi_disconnect", "end");
  radioStage("wifi_end", "begin"); WiFi.end(); radioStage("wifi_end", "end");
  resetNinaDriver();  // WiFi.end() in the installed library is an empty deinit.
  radioMode = RADIO_NONE;
}
void printStatus() {
  uint64_t now = nowMs(), remaining = 0;
  if (appState == DISCOVERY && now - windowStartedAt < windowMs)
    remaining = windowMs - (now - windowStartedAt);
  char line[900];
  snprintf(line, sizeof(line),
    "{\"type\":\"status\",\"firmware\":\"BumpSingleBoardMotionMqtt\",\"firmware_version\":2,"
    "\"uptime_ms\":%llu,\"state\":\"%s\",\"nina_mode\":\"%s\",\"ble_advertising\":%s,"
    "\"window_started_ms\":%llu,\"window_deadline_ms\":%llu,\"window_remaining_ms\":%llu,"
    "\"queued\":%u,\"overflow\":%lu,\"source\":\"hardware\",\"imu_ready\":%s,"
    "\"samples\":%llu,\"span_ms\":%llu,\"max_gap_ms\":%llu,\"trigger_count\":%lu,"
    "\"accepted_hardware_triggers\":%lu,\"injected_trigger_count\":%lu,"
    "\"last_sample_uptime_ms\":%llu,\"boot_id_set\":%s,\"transport\":\"%s\",\"mqtt_tls\":false",
    now, stateName(), radioName(), advertising ? "true" : "false",
    windowStartedAt, appState == DISCOVERY ? windowStartedAt + windowMs : 0, remaining,
    unsigned(queueCount), (unsigned long)overflowCount, imuReady ? "true" : "false",
    samples, samples ? lastSampleAt - firstSampleAt : 0, maxGapMs,
    (unsigned long)hardwareTriggers, (unsigned long)acceptedHardwareTriggers,
    (unsigned long)injectedTriggers, lastSampleAt, *bootId ? "true" : "false", transport);
  Serial.print(line);
  if (samples) {
    Serial.print(",\"accel_g\":["); Serial.print(latestAccel[0], 5); Serial.print(',');
    Serial.print(latestAccel[1], 5); Serial.print(','); Serial.print(latestAccel[2], 5);
    Serial.println("]}");
  } else Serial.println(",\"accel_g\":null}");
}
bool enqueueEvent(const char* event, const char* source, const float* accel, uint64_t at) {
  if (!*bootId) { reportError("set_boot_id_before_events"); return false; }
  if (queueCount == 16) {
    ++overflowCount;
    Serial.print("{\"type\":\"queue_rejected\",\"reason\":\"full\",\"overflow\":");
    Serial.print(overflowCount); Serial.println(",\"queued\":16}"); return false;
  }
  QueuedEvent& item = queue[queueCount];
  item.seq = nextSeq;
  char accelText[3][24];
  for (unsigned i = 0; i < 3; ++i) {
    long fixed = lroundf(accel[i] * 100000.0f);
    unsigned long magnitude = fixed < 0 ? -fixed : fixed;
    snprintf(accelText[i], sizeof(accelText[i]), "%s%lu.%05lu",
             fixed < 0 ? "-" : "", magnitude / 100000, magnitude % 100000);
  }
  int length = snprintf(item.payload, sizeof(item.payload),
    "{\"v\":1,\"device_id\":\"badge-a\",\"boot_id\":\"%s\",\"seq\":%lu,"
    "\"event\":\"%s\",\"source\":\"%s\",\"transport\":\"%s\",\"uptime_ms\":%llu,"
    "\"accel_g\":[%s,%s,%s],\"state\":\"%s\",\"window_ms\":60000}",
    bootId, (unsigned long)item.seq, event, source, transport, at,
    accelText[0], accelText[1], accelText[2], stateName());
  if (length < 0 || size_t(length) >= sizeof(item.payload)) {
    reportError("payload_buffer_full"); return false;
  }
  ++nextSeq; ++queueCount;
  Serial.print("{\"type\":\"event_queued\",\"queued\":"); Serial.print(queueCount);
  Serial.print(",\"payload\":"); Serial.print(item.payload); Serial.println("}");
  return true;
}
void removeAcknowledged(const char* origin) {
  uint32_t seq = queue[0].seq;
  --queueCount;
  memmove(queue, queue + 1, queueCount * sizeof(queue[0]));
  if (!queueCount) bridgeExportedThrough = 0;
  Serial.print("{\"type\":\"queue_removed_after_application_ack\",\"ack_origin\":\"");
  Serial.print(origin); Serial.print("\",\"seq\":"); Serial.print(seq);
  Serial.print(",\"queued\":"); Serial.print(queueCount); Serial.println("}");
}
void stopBle() {
  if (radioMode == RADIO_BLE) {
    radioStage("ble_stop_advertise", "begin"); BLE.stopAdvertise(); radioStage("ble_stop_advertise", "end");
    radioStage("ble_end", "begin"); BLE.end(); radioStage("ble_end", "end");
    resetNinaDriver();
  }
  advertising = false; radioMode = RADIO_NONE;
}
void startBle() {
  stopWifi();
  radioStage("ble_begin", "begin");
  int initialized = BLE.begin(), configured = 0, started = 0;
  radioStage("ble_begin", "end");
  if (initialized) {
    radioMode = RADIO_BLE;
    String address = BLE.address(); address.replace(":", "");
    if (address.length() == 12) {
      for (unsigned i = 0; i < 6; ++i)
        manufacturerFrame[3 + i] = strtoul(address.substring(i * 2, i * 2 + 2).c_str(), nullptr, 16);
      BLEAdvertisingData fresh;
      BLE.setAdvertisingData(fresh);  // BLE.end() clears the previous flags/data.
      BLE.setConnectable(false);
      configured = BLE.setLocalName("BumpMotion") && BLE.setAdvertisedServiceUuid("A5B0") &&
                   BLE.setManufacturerData(manufacturerFrame, sizeof(manufacturerFrame));
      if (configured) {
        radioStage("ble_advertise", "begin"); started = BLE.advertise(); radioStage("ble_advertise", "end");
      }
    }
  }
  advertising = started != 0;
  Serial.print("{\"type\":\"ble_advertise\",\"initialized\":"); Serial.print(initialized);
  Serial.print(",\"configured\":"); Serial.print(configured);
  Serial.print(",\"started\":"); Serial.print(started);
  Serial.print(",\"service_uuid\":\"A5B0\",\"manufacturer_hex\":\"");
  for (unsigned i = 0; i < sizeof(manufacturerFrame); ++i) {
    if (manufacturerFrame[i] < 16) Serial.print('0');
    Serial.print(manufacturerFrame[i], HEX);
  }
  Serial.println("\",\"over_air_verified\":false}");
  if (!started) stopBle();
}
void scanWifi() {
  if (appState != IDLE) { reportError("wifi_scan_requires_idle"); return; }
  stopBle(); radioMode = RADIO_WIFI;
  radioStage("wifi_scan", "begin");
  int count = WiFi.scanNetworks(), matches = 0;
  for (int i = 0; i < count; ++i)
    if (*ssid && !strcmp(WiFi.SSID(i), ssid)) ++matches;
  radioStage("wifi_scan", "end");
  Serial.print("{\"type\":\"wifi_scan\",\"network_count\":"); Serial.print(count);
  Serial.print(",\"configured_ssid_set\":"); Serial.print(*ssid ? "true" : "false");
  Serial.print(",\"configured_matches\":"); Serial.print(matches); Serial.print(",\"matches\":[");
  bool first = true;
  for (int i = 0; i < count; ++i) {
    if (!*ssid || strcmp(WiFi.SSID(i), ssid)) continue;
    if (!first) Serial.print(',');
    first = false;
    Serial.print("{\"rssi_dbm\":"); Serial.print(WiFi.RSSI(i));
    Serial.print(",\"encryption_type\":"); Serial.print(WiFi.encryptionType(i)); Serial.print('}');
  }
  Serial.println("],\"identifiers_logged\":false,\"connected\":false}");
  stopWifi();
}
void motionTriggered(const char* source, const float* accel, uint64_t at) {
  if (appState != IDLE) {
    Serial.print("{\"type\":\"motion_ignored\",\"source\":\""); Serial.print(source);
    Serial.print("\",\"state\":\""); Serial.print(stateName()); Serial.println("\"}"); return;
  }
  appState = DISCOVERY; windowStartedAt = at;
  if (!strcmp(source, "hardware")) ++acceptedHardwareTriggers;
  enqueueEvent("motion_trigger", source, accel, at);
  startBle();
}
void sampleHardware() {
  if (!imuReady || !IMU.accelerationAvailable()) return;
  float x, y, z;
  if (!IMU.readAcceleration(x, y, z) || !isfinite(x) || !isfinite(y) || !isfinite(z)) return;
  uint64_t now = nowMs();
  if (!samples) firstSampleAt = now;
  else if (now - lastSampleAt > maxGapMs) maxGapMs = now - lastSampleAt;
  lastSampleAt = now; ++samples;
  latestAccel[0] = x; latestAccel[1] = y; latestAccel[2] = z;
  if (realDetector.sample(x, y, z, now)) {
    ++hardwareTriggers; motionTriggered("hardware", latestAccel, now);
  }
}
void serviceInjection() {
  if (!injectionActive) return;
  uint64_t now = nowMs(), age = now - injectionStartedAt;
  if (now - lastInjectionAt < 5) return;
  lastInjectionAt = now;
  if (age > 700) {
    injectionActive = false; Serial.println("{\"type\":\"injection_complete\",\"source\":\"injected_test\"}"); return;
  }
  float accel[3] = {0, 0, ((age >= 100 && age < 150) || (age >= 400 && age < 450)) ? 2.2f : 1.0f};
  if (injectedDetector.sample(accel[0], accel[1], accel[2], now)) {
    ++injectedTriggers; motionTriggered("injected_test", accel, now);
  }
}
void receiveAck(int messageSize) {
  bool topicMatches = mqtt.messageTopic() == ackTopic && !mqtt.messageRetain();
  char payload[224];
  if (messageSize <= 0 || messageSize >= int(sizeof(payload))) {
    reportError("ack_size_rejected"); mqtt.stop(); return;
  }
  int count = 0;
  unsigned long started = millis();
  while (count < messageSize && millis() - started < 750) {
    if (network.available()) {
      int value = mqtt.read(); if (value < 0) break;
      payload[count++] = char(value);
    } else delay(1);
  }
  bool embeddedNull = memchr(payload, '\0', count) != nullptr;
  payload[count] = '\0';
  bool matched = count == messageSize && !embeddedNull && topicMatches && awaitingSeq &&
                 motion_ack::matches(payload, bootId, awaitingSeq);
  if (matched) ackReceived = true;
  Serial.print("{\"type\":\"application_ack\",\"ack_origin\":\"wifi_mqtt\",\"matched\":");
  Serial.print(matched ? "true" : "false"); Serial.print(",\"expected_seq\":");
  Serial.print(awaitingSeq); Serial.println("}");
  if (count != messageSize) { reportError("ack_read_timeout"); mqtt.stop(); }
}
void syncQueue() {
  injectionActive = false;
  stopBle(); appState = SYNC;
  uint64_t syncStarted = nowMs();
  unsigned delivered = 0;
  Serial.print("{\"type\":\"sync_started\",\"transport\":\"");
  Serial.print(transport); Serial.println("\",\"tls\":false}");
  do {
    if (!strcmp(transport, "serial_bridge")) {
      Serial.println("{\"type\":\"serial_bridge_waiting\",\"instructions\":\"dump_then_matching_ACK\",\"wifi_attempted\":false}"); break;
    }
    if (!queueCount) break;
    if (!*ssid || !*brokerHost || !*bootId) { reportError("sync_configuration_incomplete"); break; }
    radioMode = RADIO_WIFI;
    WiFi.setTimeout(12000);
    radioStage("wifi_begin", "begin");
    int result = *wifiPassword ? WiFi.begin(ssid, wifiPassword) : WiFi.begin(ssid);
    radioStage("wifi_begin", "end");
    sampleHardware();  // Records the actual gap caused by blocking association.
    Serial.print("{\"type\":\"wifi_connect\",\"status\":"); Serial.print(result);
    Serial.print(",\"connected\":"); Serial.print(result == WL_CONNECTED ? "true" : "false");
    if (result == WL_CONNECTED) {
      Serial.print(",\"local_ip\":\""); Serial.print(WiFi.localIP());
      Serial.print("\",\"gateway_ip\":\""); Serial.print(WiFi.gatewayIP()); Serial.print('"');
    }
    Serial.println("}");
    if (result != WL_CONNECTED) break;
    network.setConnectionTimeout(5000);
    mqtt.setId("badge-a"); mqtt.setCleanSession(true);
    mqtt.setUsernamePassword(mqttUser, mqttPassword);
    mqtt.setConnectionTimeout(4000); mqtt.setKeepAliveInterval(15000);
    radioStage("mqtt_connect", "begin");
    int connected = mqtt.connect(brokerHost, brokerPort);
    radioStage("mqtt_connect", "end");
    Serial.print("{\"type\":\"mqtt_connect\",\"connected\":"); Serial.print(connected);
    Serial.print(",\"error\":"); Serial.print(mqtt.connectError()); Serial.println(",\"tls\":false}");
    sampleHardware();
    if (!connected) break;
    int subscribed = mqtt.subscribe(ackTopic, 1);
    Serial.print("{\"type\":\"mqtt_subscribe\",\"subscribed\":"); Serial.print(subscribed);
    Serial.print(",\"granted_qos\":"); Serial.print(mqtt.subscribeQoS()); Serial.println("}");
    if (!subscribed) break;
    while (queueCount && mqtt.connected() && nowMs() - syncStarted < 45000) {
      awaitingSeq = queue[0].seq; ackReceived = false;
      size_t length = strlen(queue[0].payload);
      bool begun = mqtt.beginMessage(eventTopic, (unsigned long)length, false, 1);
      bool written = begun && mqtt.write((const uint8_t*)queue[0].payload, length) == length;
      int puback = written ? mqtt.endMessage() : 0;
      Serial.print("{\"type\":\"mqtt_publish\",\"seq\":"); Serial.print(awaitingSeq);
      Serial.print(",\"qos\":1,\"retain\":false,\"puback_ok\":");
      Serial.print(puback ? "true" : "false"); Serial.println("}");
      uint64_t waitStarted = nowMs();
      while (written && !ackReceived && mqtt.connected() &&
             nowMs() - waitStarted < 4000 && nowMs() - syncStarted < 45000) {
        mqtt.poll(); sampleHardware(); delay(1);
      }
      if (!ackReceived) { reportError("application_ack_missing_queue_retained"); break; }
      removeAcknowledged("wifi_mqtt"); ++delivered; awaitingSeq = 0;
    }
  } while (false);
  awaitingSeq = 0;
  radioStage("mqtt_stop", "begin"); mqtt.stop(); radioStage("mqtt_stop", "end");
  stopWifi();
  radioMode = RADIO_NONE;
  sampleHardware();
  appState = IDLE;
  Serial.print("{\"type\":\"sync_complete\",\"delivered\":"); Serial.print(delivered);
  Serial.print(",\"queued\":"); Serial.print(queueCount);
  Serial.print(",\"elapsed_ms\":"); Serial.print((unsigned long)(nowMs() - syncStarted));
  Serial.println(",\"queue_storage\":\"ram\",\"tls\":false}");
}
bool validUuid(const char* value) {
  if (strlen(value) != 36) return false;
  for (unsigned i = 0; i < 36; ++i) {
    if (i == 8 || i == 13 || i == 18 || i == 23) { if (value[i] != '-') return false; }
    else if (!isxdigit((unsigned char)value[i])) return false;
  }
  return true;
}
void setConfig(char* setting) {
  char* separator = strchr(setting, ' ');
  if (!separator) { reportError("set_requires_key_and_value"); return; }
  *separator = '\0'; const char* value = separator + 1;
  char* destination = nullptr; size_t capacity = 0;
  if (!strcmp(setting, "ssid")) { destination = ssid; capacity = sizeof(ssid); }
  else if (!strcmp(setting, "wifi_password")) { destination = wifiPassword; capacity = sizeof(wifiPassword); }
  else if (!strcmp(setting, "broker_host")) { destination = brokerHost; capacity = sizeof(brokerHost); }
  else if (!strcmp(setting, "mqtt_user")) { destination = mqttUser; capacity = sizeof(mqttUser); }
  else if (!strcmp(setting, "mqtt_password")) { destination = mqttPassword; capacity = sizeof(mqttPassword); }
  else if (!strcmp(setting, "transport")) {
    if (queueCount || nextSeq != 1 ||
        (strcmp(value, "wifi_mqtt") && strcmp(value, "serial_bridge"))) {
      reportError("transport_requires_valid_value_before_first_event"); return;
    }
    destination = transport; capacity = sizeof(transport);
  } else if (!strcmp(setting, "boot_id")) {
    if (!validUuid(value) || (*bootId && strcmp(bootId, value))) {
      reportError("boot_id_requires_uuid_and_is_fixed_until_reset"); return;
    }
    destination = bootId; capacity = sizeof(bootId);
  } else if (!strcmp(setting, "broker_port")) {
    const char* p = value; uint32_t port = 0;
    if (!motion_ack::number(p, port) || *p || !port || port > 65535) {
      reportError("broker_port_invalid"); return;
    }
    brokerPort = uint16_t(port);
  } else { reportError("unknown_configuration_key"); return; }
  if (destination) {
    if (strlen(value) >= capacity) { reportError("configuration_value_too_long"); return; }
    strcpy(destination, value);
  }
  Serial.print("{\"type\":\"configuration_set\",\"key\":\""); Serial.print(setting);
  Serial.println("\",\"ok\":true}");
}
void handleCommand(char* command) {
  if (!strncmp(command, "SET ", 4)) setConfig(command + 4);
  else if (!strcmp(command, "status")) printStatus();
  else if (!strcmp(command, "wifi_scan")) scanWifi();
  else if (!strcmp(command, "sample")) {
    sampleHardware();
    if (samples) enqueueEvent("imu_sample", "hardware", latestAccel, lastSampleAt);
    else reportError("no_hardware_sample_available");
  } else if (!strcmp(command, "inject_shake")) {
    if (injectionActive) { reportError("injection_already_active"); return; }
    injectedDetector = bump::ShakeDetector();
    injectionStartedAt = nowMs(); lastInjectionAt = injectionStartedAt;
    injectedDetector.sample(0, 0, 1, injectionStartedAt);
    injectionActive = true;
    Serial.println("{\"type\":\"injection_started\",\"source\":\"injected_test\",\"sim\":true}");
  } else if (!strcmp(command, "sync")) syncQueue();
  else if (!strcmp(command, "dump")) {
    for (size_t i = 0; i < queueCount; ++i) {
      Serial.print("{\"type\":\"queued_event\",\"payload\":"); Serial.print(queue[i].payload); Serial.println("}");
    }
    bridgeExportedThrough = queueCount ? queue[queueCount - 1].seq : 0;
    Serial.print("{\"type\":\"queue_dump_complete\",\"queued\":"); Serial.print(queueCount); Serial.println("}");
  } else if (!strncmp(command, "ACK ", 4)) {
    bool matched = !strcmp(transport, "serial_bridge") && queueCount &&
                   queue[0].seq <= bridgeExportedThrough &&
                   motion_ack::matches(command + 4, bootId, queue[0].seq);
    Serial.print("{\"type\":\"application_ack\",\"ack_origin\":\"serial_bridge\",\"matched\":");
    Serial.print(matched ? "true" : "false"); Serial.println("}");
    if (matched) removeAcknowledged("serial_bridge");
  } else if (!strcmp(command, "i") || !strcmp(command, "q")) {
    streamImu = !strcmp(command, "i");
    Serial.print("{\"type\":\"telemetry\",\"enabled\":"); Serial.print(streamImu ? "true" : "false"); Serial.println("}");
  } else if (!strcmp(command, "r")) {
    Serial.println("{\"type\":\"restart\",\"mechanism\":\"rp2040_watchdog\",\"ram_queue_will_be_lost\":true}");
    Serial.flush(); delay(100); watchdog_reboot(0, 0, 100);
    while (true) delay(1);
  } else if (!strcmp(command, "t")) {
    Serial.println("{\"type\":\"bootloader\",\"mechanism\":\"rp2040_rom\",\"ram_queue_will_be_lost\":true}");
    Serial.flush(); delay(100); reset_usb_boot(1u << digitalPinToPinName(LED_BUILTIN), 0);
  } else if (*command) reportError("unknown_command");
}
void setup() {
  Serial.begin(115200);
  unsigned long started = millis();
  while (!Serial && millis() - started < 8000) delay(10);
  imuReady = IMU.begin(); mqtt.onMessage(receiveAck);
  Serial.print("{\"type\":\"boot\",\"firmware\":\"BumpSingleBoardMotionMqtt\",\"firmware_version\":2,\"imu_ready\":");
  Serial.print(imuReady ? "true" : "false");
  Serial.println(",\"configured_imu_hz\":104,\"queue_capacity\":16,\"mqtt_tls\":false,\"configuration_storage\":\"ram\",\"commands\":\"SET status wifi_scan sample inject_shake sync dump ACK i q r t\"}");
}
void loop() {
  uint64_t now = nowMs();
  if (appState == DISCOVERY && now - windowStartedAt >= windowMs) {
    radioStage("discovery_deadline", "reached");
    stopBle(); appState = IDLE;
    Serial.println("{\"type\":\"discovery_complete\",\"state\":\"IDLE\"}");
    if (queueCount) syncQueue();
  }
  sampleHardware();
  // Non-connectable advertising/scan responses run autonomously in the controller.
  // There are no connections, scans or GATT events needing continuous host polling.
  serviceInjection();
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (lineOverflow) reportError("command_line_too_long");
      else { commandLine[commandLength] = '\0'; handleCommand(commandLine); }
      commandLength = 0; lineOverflow = false;
    } else if (commandLength + 1 < sizeof(commandLine) && !lineOverflow)
      commandLine[commandLength++] = c;
    else lineOverflow = true;
  }
  if (streamImu && millis() - lastTelemetry >= 1000) {
    lastTelemetry = millis(); printStatus();
  }
  delay(1);
}
