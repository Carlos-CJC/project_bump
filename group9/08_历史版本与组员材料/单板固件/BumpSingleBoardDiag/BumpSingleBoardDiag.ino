// Independent single-board diagnostics, not the teammate's BLE MVP firmware.
// No external GPIO, simulated encounters, Wi-Fi association, or cloud upload.
#include <Arduino_LSM6DSOX.h>
#include <ArduinoBLE.h>
#include <WiFiNINA.h>
extern "C" {
#include "hardware/watchdog.h"
#include "pico/bootrom.h"
}

enum RadioMode { RADIO_NONE, RADIO_WIFI, RADIO_BLE };
RadioMode radioMode = RADIO_NONE;
bool imuReady = false, streamImu = true;
unsigned long lastSample = 0;
uint8_t manufacturerFrame[11] = {0xff, 0xff, 1, 0, 0, 0, 0, 0, 0, 1, 0xfc};

void diagError(const char *message) {
  Serial.print("{\"type\":\"error\",\"message\":\"");
  Serial.print(message); Serial.println("\"}");
}

void sampleImu() {
  if (!imuReady || !IMU.accelerationAvailable() || !IMU.gyroscopeAvailable()) return;
  float ax, ay, az, gx, gy, gz;
  if (!IMU.readAcceleration(ax, ay, az) || !IMU.readGyroscope(gx, gy, gz)) return;
  Serial.print("{\"type\":\"imu\",\"source\":\"real_sensor\",\"uptime_ms\":");
  Serial.print(millis());
  Serial.print(",\"accel_g\":["); Serial.print(ax, 5); Serial.print(',');
  Serial.print(ay, 5); Serial.print(','); Serial.print(az, 5);
  Serial.print("],\"gyro_dps\":["); Serial.print(gx, 5); Serial.print(',');
  Serial.print(gy, 5); Serial.print(','); Serial.print(gz, 5);
  Serial.println("]}");
}

void testLed() {
  pinMode(LED_BUILTIN, OUTPUT);
  for (int n = 0; n < 6; n++) {
    int requested = (n % 2 == 0) ? HIGH : LOW;
    digitalWrite(LED_BUILTIN, requested);
    Serial.print("{\"type\":\"led_gpio\",\"commanded\":"); Serial.print(requested);
    Serial.print(",\"readback\":"); Serial.print(digitalRead(LED_BUILTIN));
    Serial.println(",\"optical_confirmation\":false}");
    delay(150);
  }
}

void scanWifi() {
  if (radioMode == RADIO_BLE) { BLE.stopAdvertise(); BLE.end(); radioMode = RADIO_NONE; }
  radioMode = RADIO_WIFI;
  Serial.print("{\"type\":\"wifi_firmware\",\"version\":\"");
  Serial.print(WiFi.firmwareVersion()); Serial.println("\"}");
  int count = WiFi.scanNetworks();
  long strongest = -999, weakest = 0;
  for (int n = 0; n < count; n++) {
    long rssi = WiFi.RSSI(n);
    if (rssi > strongest) strongest = rssi;
    if (rssi < weakest) weakest = rssi;
  }
  Serial.print("{\"type\":\"wifi_scan\",\"network_count\":"); Serial.print(count);
  if (count > 0) {
    Serial.print(",\"strongest_rssi_dbm\":"); Serial.print(strongest);
    Serial.print(",\"weakest_rssi_dbm\":"); Serial.print(weakest);
  }
  Serial.println(",\"connected\":false,\"identifiers_logged\":false}");
}

void startBle() {
  if (radioMode == RADIO_WIFI) { WiFi.end(); radioMode = RADIO_NONE; }
  if (radioMode != RADIO_BLE) {
    if (!BLE.begin()) { diagError("ble_begin_failed"); return; }
    radioMode = RADIO_BLE;
    String address = BLE.address(); address.replace(":", "");
    if (address.length() != 12) { diagError("unexpected_ble_address"); BLE.end(); radioMode = RADIO_NONE; return; }
    for (int n = 0; n < 6; n++)
      manufacturerFrame[3 + n] = strtoul(address.substring(n * 2, n * 2 + 2).c_str(), nullptr, 16);
    BLEAdvertisingData freshAdvertisingData;
    BLE.setAdvertisingData(freshAdvertisingData);
    BLE.setConnectable(false);
    if (!BLE.setLocalName("BumpDiag") || !BLE.setAdvertisedServiceUuid("A5B0") ||
        !BLE.setManufacturerData(manufacturerFrame, sizeof(manufacturerFrame))) {
      diagError("advertisement_configuration_failed"); BLE.end(); radioMode = RADIO_NONE; return;
    }
  }
  int started = BLE.advertise();
  Serial.print("{\"type\":\"ble_advertise\",\"independent_diagnostic\":true,\"started\":");
  Serial.print(started);
  Serial.print(",\"service_uuid\":\"A5B0\",\"manufacturer_hex\":\"");
  for (unsigned int n = 0; n < sizeof(manufacturerFrame); n++) {
    if (manufacturerFrame[n] < 16) Serial.print('0');
    Serial.print(manufacturerFrame[n], HEX);
  }
  Serial.println("\",\"over_air_verified\":false}");
}

void scanBle() {
  if (radioMode != RADIO_BLE) { diagError("start_ble_with_b_first"); return; }
  BLE.stopAdvertise();
  int started = BLE.scan(true);
  unsigned long beginMs = millis(), reports = 0;
  while (started && millis() - beginMs < 5000) {
    BLE.poll();
    BLEDevice device = BLE.available();
    if (device) reports++;
    delay(1);
  }
  BLE.stopScan();
  int advertising = BLE.advertise();
  Serial.print("{\"type\":\"ble_scan\",\"started\":"); Serial.print(started);
  Serial.print(",\"duration_ms\":"); Serial.print(millis() - beginMs);
  Serial.print(",\"reports\":"); Serial.print(reports);
  Serial.print(",\"advertising_resumed\":"); Serial.print(advertising);
  Serial.println(",\"identifiers_logged\":false}");
}

void setup() {
  Serial.begin(115200);
  unsigned long waitStart = millis();
  while (!Serial && millis() - waitStart < 8000) delay(10);
  imuReady = IMU.begin();
  Serial.print("{\"type\":\"boot\",\"firmware\":\"BumpSingleBoardDiag\",\"imu_ready\":");
  Serial.print(imuReady ? "true" : "false");
  Serial.println(",\"commands\":\"i q l w b s r t\",\"firmware_version\":2,\"external_modules_tested\":false}");
}

void loop() {
  if (radioMode == RADIO_BLE) BLE.poll();
  if (Serial.available()) {
    char command = Serial.read();
    if (command == 'i') streamImu = true;
    else if (command == 'q') streamImu = false;
    else if (command == 'l') testLed();
    else if (command == 'w') scanWifi();
    else if (command == 'b') startBle();
    else if (command == 's') scanBle();
    else if (command == 'r') {
      Serial.println("{\"type\":\"restart\",\"mechanism\":\"rp2040_watchdog\"}");
      Serial.flush(); delay(100); watchdog_reboot(0, 0, 100);
      while (true) delay(1);
    } else if (command == 't') {
      Serial.println("{\"type\":\"bootloader\",\"mechanism\":\"rp2040_rom\"}");
      Serial.flush(); delay(100);
      reset_usb_boot(1u << digitalPinToPinName(LED_BUILTIN), 0);
    }
  }
  if (streamImu && millis() - lastSample >= 200) {
    lastSample = millis(); sampleImu();
  }
}
