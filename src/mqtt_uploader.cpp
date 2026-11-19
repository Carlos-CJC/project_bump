#include "mqtt_uploader.h"

#include "bump_config.h"

#if BUMP_MQTT

#include <Arduino.h>
#include <ArduinoBLE.h>
#include <ArduinoMqttClient.h>
#include <WiFiNINA.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "device_identity.h"
#include "event_queue.h"
#include "payload_writer.h"
#include "radio.h"
#include "watchdog.h"

static UploaderResumeFn s_resume;
static char s_topicEnc[64];
static char s_topicStatus[64];
static char s_batchId[40];
static char s_payload[1792];

static bool s_uploadDue;
static bool s_started;
static uint32_t s_nextUploadAt;  // 0 = 无待办
static uint8_t s_failures;
static uint32_t s_lastWindowEnd;

static void logf(const char* fmt, ...) {
  char buf[192];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  Serial.println(buf);
}

static size_t buildStatusFor(char* buf, size_t cap, const char* state) {
  StatusInfo info;
  info.deviceId = identityDeviceId();
  info.bootId = identityBootId();
  info.fw = BUMP_FW_VERSION;
  info.state = state;
  info.uptimeS = millis() / 1000;
  info.queueDepth = (uint8_t)eventQueueCount();
  info.eventsDropped = eventQueueDropped();
  info.wifiRssi = (int)WiFi.RSSI();
  char shortHex[13];
  bumpSelfShortId(shortHex, sizeof(shortHex));
  info.selfShortId = shortHex;
  return buildStatus(buf, cap, info);
}

// BLE 刚关闭时第一次 WiFi.begin 常会一直停在 WL_IDLE，整轮超时；
// WiFi.end() 后重新 begin 通常几秒内就能连上，因此失败后带一次重建重试。
static bool wifiConnect() {
  if (BUMP_WIFI_SSID[0] == '\0') {
    return false;
  }
  const uint32_t attemptMs = BUMP_WIFI_TIMEOUT_MS / 2;
  for (uint8_t attempt = 0; attempt < 2; attempt++) {
    WiFi.begin(BUMP_WIFI_SSID, BUMP_WIFI_PASS);
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED) {
      if ((int32_t)(millis() - start) > (int32_t)attemptMs) {
        break;
      }
      delay(500);
      watchdogKick();
    }
    if (WiFi.status() == WL_CONNECTED) {
      IPAddress ip = WiFi.localIP();
      logf("[mqtt] wifi ok attempt=%u ip=%u.%u.%u.%u rssi=%d",
           (unsigned)attempt + 1, ip[0], ip[1], ip[2], ip[3], (int)WiFi.RSSI());
      return true;
    }
    logf("[mqtt] wifi attempt=%u failed status=%d", (unsigned)attempt + 1,
         (int)WiFi.status());
    WiFi.end();
    delay(2000);
    watchdogKick();
  }
  return false;
}

// QoS1 的 endMessage() 会阻塞等待 PUBACK，返回 1 表示 broker 已接收。
static bool publishMsg(MqttClient& mqtt, const char* topic, const char* payload,
                       size_t len, bool retain) {
  if (!mqtt.beginMessage(topic, (unsigned long)len, retain, 1)) {
    return false;
  }
  if (mqtt.write((const uint8_t*)payload, len) != len) {
    return false;
  }
  return mqtt.endMessage() == 1;
}

static void scheduleBackoff() {
  if (s_failures < 8) {  // 只用于计算退避，封顶避免 uint8 回绕
    s_failures++;
  }
  uint8_t shift = s_failures - 1 < 5 ? s_failures - 1 : 5;
  uint32_t backoff = 10000UL << shift;
  if (backoff > 300000UL) {
    backoff = 300000UL;
  }
  backoff += (uint32_t)random(0, 5000);
  uint32_t now = millis();
  s_nextUploadAt = now + backoff;
  s_lastWindowEnd = now;
  logf("[mqtt] backoff=%lus", (unsigned long)(backoff / 1000));
}

static void failWindow(const char* stage) {
  watchdogKick();
  logf("[mqtt] session failed stage=%s queued=%u fails=%u", stage,
       (unsigned)eventQueueCount(), (unsigned)s_failures);
  WiFi.end();
  delay(500);
  watchdogKick();
  scheduleBackoff();
  s_resume();
}

static void runWindow() {
  uint32_t start = millis();
  logf("[mqtt] session start queued=%u fails=%u", (unsigned)eventQueueCount(),
       (unsigned)s_failures);

  // Wi-Fi 与 BLE 共用 NINA 射频：关 BLE 后硬复位，确保 NINA 干净进入 Wi-Fi 模式。
  // 只调 BLE.end() 时 NINA 偶尔仍停在 BLE 模式，WiFi.begin() 返回 WL_NO_MODULE。
  BLE.end();
  delay(3000);
  watchdogKick();
  bumpNinaReset();

  if (!wifiConnect()) {
    failWindow("wifi");
    return;
  }

  WiFiSSLClient ssl;
  MqttClient mqtt(ssl);
  mqtt.setId(identityDeviceId());
  mqtt.setUsernamePassword(identityDeviceId(), BUMP_MQTT_PASS);
  mqtt.setCleanSession(true);
  mqtt.setKeepAliveInterval(60000);
  mqtt.setConnectionTimeout(BUMP_MQTT_TIMEOUT_MS);

  // LWT：非正常断开时 broker 代发 retained offline。
  char status[256];
  size_t statusLen = buildStatusFor(status, sizeof(status), "offline");
  if (!statusLen) {
    failWindow("payload");
    return;
  }
  mqtt.beginWill(s_topicStatus, (unsigned short)statusLen, true, 1);
  mqtt.write((const uint8_t*)status, statusLen);
  mqtt.endWill();

  // BLE 刚释放射频时第一次 TLS 握手容易失败（NINA 内部状态残留），
  // 隔 1s 再试一次通常即可成功，比整窗退避快得多。
  bool connected = false;
  for (uint8_t attempt = 0; attempt < 2; attempt++) {
    watchdogKick();
    if (mqtt.connect(BUMP_MQTT_HOST, BUMP_MQTT_PORT)) {
      connected = true;
      break;
    }
    logf("[mqtt] connect failed attempt=%u err=%d", (unsigned)attempt + 1,
         mqtt.connectError());
    if (attempt == 0) {
      delay(1000);
    }
  }
  if (!connected) {
    failWindow("mqtt");
    return;
  }
  logf("[mqtt] mqtt connected");

  statusLen = buildStatusFor(status, sizeof(status), "online");
  if (!statusLen ||
      !publishMsg(mqtt, s_topicStatus, status, statusLen, true)) {
    failWindow("status");
    return;
  }

  while (eventQueueCount() > 0) {
    watchdogKick();
    if ((int32_t)(millis() - start) > (int32_t)BUMP_UPLOAD_BUDGET_MS) {
      logf("[mqtt] budget exceeded, deferring queued=%u",
           (unsigned)eventQueueCount());
      break;
    }
    EncEvent batch[BUMP_MQTT_BATCH_MAX];
    size_t n = eventQueuePeek(batch, BUMP_MQTT_BATCH_MAX);
    identityFormatBatchId(s_batchId, sizeof(s_batchId), batch[0].seq,
                          (uint8_t)n);
    char shortHex[13];
    bumpSelfShortId(shortHex, sizeof(shortHex));
    BatchMeta meta;
    meta.batchId = s_batchId;
    meta.deviceId = identityDeviceId();
    meta.bootId = identityBootId();
    meta.selfShortId = shortHex;
    meta.fw = BUMP_FW_VERSION;
    meta.uptimeMs = millis();
    meta.timeSource = "uptime";
    size_t len = buildEncounterBatch(s_payload, sizeof(s_payload), meta, batch, n);
    if (!len) {
      logf("[mqtt] payload overflow cap=%u", (unsigned)sizeof(s_payload));
      failWindow("payload");
      return;
    }
    if (!publishMsg(mqtt, s_topicEnc, s_payload, len, false)) {
      failWindow("publish");
      return;
    }
    eventQueueDrop(n);  // 仅在 PUBACK 之后出队
    logf("[mqtt] tx ok batch=%s n=%u bytes=%u", s_batchId, (unsigned)n,
         (unsigned)len);
  }

  statusLen = buildStatusFor(status, sizeof(status), "offline");
  if (statusLen) {
    publishMsg(mqtt, s_topicStatus, status, statusLen, true);
  }
  mqtt.stop();
  WiFi.end();
  delay(500);
  watchdogKick();

  s_failures = 0;
  s_lastWindowEnd = millis();
  // 队列未清空（预算用尽）：10 分钟内定时重试；清空则等待下一个新事件。
  s_nextUploadAt = eventQueueCount() > 0 ? s_lastWindowEnd + 600000UL : 0;
  logf("[mqtt] session done queued=%u", (unsigned)eventQueueCount());
  s_resume();
}

void uploaderBegin(UploaderResumeFn resume) {
  s_resume = resume;
  s_uploadDue = false;
  s_started = true;
  s_nextUploadAt = 0;
  s_failures = 0;
  s_lastWindowEnd = 0;
  snprintf(s_topicEnc, sizeof(s_topicEnc), "bump/v1/device/%s/encounters",
           identityDeviceId());
  snprintf(s_topicStatus, sizeof(s_topicStatus), "bump/v1/device/%s/status",
           identityDeviceId());
}

void uploaderNotifyEvent() {
  s_uploadDue = true;
}

bool uploaderMaybeUpload(uint32_t now) {
  if (!s_started) {
    return false;
  }

  bool due;
  if (s_failures > 0) {
    // 退避期内无论有无新事件都不尝试，保证失败有上界。
    due = eventQueueCount() > 0 &&
          (int32_t)(now - s_nextUploadAt) >= 0;
  } else if (s_uploadDue) {
    due = true;
  } else {
    due = eventQueueCount() > 0 && s_nextUploadAt != 0 &&
          (int32_t)(now - s_nextUploadAt) >= 0;
  }
  if (!due) {
    return false;
  }

  if (BUMP_UPLOAD_MIN_INTERVAL_MS > 0 && s_lastWindowEnd != 0 &&
      (int32_t)(now - s_lastWindowEnd) < (int32_t)BUMP_UPLOAD_MIN_INTERVAL_MS) {
    s_nextUploadAt = s_lastWindowEnd + BUMP_UPLOAD_MIN_INTERVAL_MS;
    return false;
  }

  // 凭据未配置时不进上传窗口：避免无谓地停掉 BLE 造成检测黑洞。
  if (BUMP_MQTT_HOST[0] == '\0' || BUMP_WIFI_SSID[0] == '\0') {
    if (s_failures == 0) {
      logf("[mqtt] host/ssid not configured, upload disabled");
    }
    s_uploadDue = false;
    scheduleBackoff();
    return false;
  }

  s_uploadDue = false;
  runWindow();
  return true;
}

#endif  // BUMP_MQTT
