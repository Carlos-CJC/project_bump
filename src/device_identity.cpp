#include <Arduino.h>
#include <WiFiNINA.h>

#include "device_identity.h"

static char s_deviceId[18];   // "bump-" + 12 hex + '\0'
static char s_bootId[9];      // 8 hex + '\0'
static uint32_t s_eventSeq;

void identityInit() {
  uint8_t mac[6] = {0};
  WiFi.macAddress(mac);
  snprintf(s_deviceId, sizeof(s_deviceId),
           "bump-%02x%02x%02x%02x%02x%02x",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

  // 与 makeShortId() 相同的熵源；boot_id 只用于区分重启世代。
  randomSeed(micros() ^ ((uint32_t)analogRead(A0) << 16) ^ analogRead(A1));
  uint32_t boot = ((uint32_t)random(0x10000) << 16) | (uint32_t)random(0x10000);
  snprintf(s_bootId, sizeof(s_bootId), "%08lx", (unsigned long)boot);

  s_eventSeq = 0;
}

const char* identityDeviceId() {
  return s_deviceId;
}

const char* identityBootId() {
  return s_bootId;
}

const char* identityShortIdDev() {
  return &s_deviceId[5];  // 跳过 "bump-"
}

uint32_t identityNextEventSeq() {
  return ++s_eventSeq;
}

void identityFormatEventId(char* out, size_t cap, uint32_t seq) {
  snprintf(out, cap, "ev_%s_%s_%08lu",
           identityShortIdDev(), s_bootId, (unsigned long)seq);
}

void identityFormatBatchId(char* out, size_t cap, uint32_t firstSeq, uint8_t count) {
  snprintf(out, cap, "bt_%s_%s_%08lu_%02u",
           identityShortIdDev(), s_bootId, (unsigned long)firstSeq, (unsigned)count);
}
