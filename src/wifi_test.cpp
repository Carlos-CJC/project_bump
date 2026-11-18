#include <Arduino.h>
#include <WiFiNINA.h>

#include "wifi_test.h"

#ifndef WIFI_SSID
#define WIFI_SSID "iPhone"
#endif
#ifndef WIFI_PASS
#define WIFI_PASS "22222222"
#endif

static void printMac() {
  uint8_t mac[6];
  WiFi.macAddress(mac);
  char buf[18];
  snprintf(buf, sizeof(buf), "%02x:%02x:%02x:%02x:%02x:%02x",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  Serial.print(buf);
}

static void printConnected() {
  Serial.print("[wifi] connected ip="); Serial.print(WiFi.localIP());
  Serial.print(" rssi="); Serial.print(WiFi.RSSI());
  Serial.print(" mac="); printMac();
  Serial.println();
}

void wifiTestSetup() {
  Serial.begin(115200);
  unsigned long t0 = millis();
  while (!Serial && millis() - t0 < 2000) {
  }
  Serial.println("[wifi] start");

  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println("[wifi] no WiFi module");
    for (;;) delay(1000);
  }
  Serial.print("[wifi] firmware="); Serial.println(WiFi.firmwareVersion());
  Serial.print("[wifi] connecting to '"); Serial.print(WIFI_SSID); Serial.println("'");

  int status = WiFi.begin(WIFI_SSID, WIFI_PASS);
  t0 = millis();
  while (status != WL_CONNECTED && millis() - t0 < 30000) {
    delay(1000);
    status = WiFi.status();
    Serial.print("[wifi] waiting status="); Serial.println(status);
  }

  if (status == WL_CONNECTED) {
    Serial.println("[wifi] RESULT=PASS");
    printConnected();
  } else {
    Serial.print("[wifi] RESULT=FAIL status="); Serial.println(status);
  }
}

void wifiTestLoop() {
  static unsigned long last = 0;
  if (millis() - last < 5000) {
    return;
  }
  last = millis();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wifi] reconnecting ...");
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    return;
  }
  printConnected();
}
