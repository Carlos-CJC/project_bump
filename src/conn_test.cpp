/**
 * @file conn_test.cpp
 * @brief 双板 BLE 连接自检（仅联调使用，不影响默认偶遇固件）。
 *
 * BUMP_TEST_ROLE = 1：外设角色。可连接广播，暴露服务 a5b0 / 特征 a5b1。
 * BUMP_TEST_ROLE = 2：中心角色。扫描 a5b0，连接后读取 a5b1，保持 1s 再断开，
 *                    每 5s 重试一次，串口输出 RESULT=PASS/FAIL。
 *
 * 偶遇 MVP 使用「不可连接广播 + 时分复用」，无法回答「两块板能否建立连接」；
 * 本模式专门验证 BLE 建连、服务发现、特征读取这条链路。
 */

#include <Arduino.h>
#include <ArduinoBLE.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "conn_test.h"

#ifndef BUMP_TEST_ROLE
#define BUMP_TEST_ROLE 0
#endif

#ifndef BUMP_DEVICE_LABEL
#define BUMP_DEVICE_LABEL "?"
#endif

static const char* SERVICE_UUID = "a5b0";
static const char* CHAR_UUID = "a5b1";

static const uint32_t SERIAL_WAIT_MS = 2000;    // 等待 USB 串口就绪的上限
static const uint32_t CONNECT_RETRY_MS = 5000;  // 中心断开后重新扫描的间隔
static const uint32_t HOLD_MS = 1000;           // 建连后保持时长，验证链路稳定
static const uint32_t SCAN_SESSION_MS = 4000;   // 单个扫描会话时长，到时重启协议栈

static void logf(const char* fmt, ...) {
  char buf[192];
  va_list args;
  va_start(args, fmt);
  vsnprintf(buf, sizeof(buf), fmt, args);
  va_end(args);
  Serial.println(buf);
}

static void serialBegin() {
  Serial.begin(115200);
  uint32_t start = millis();
  while (!Serial && (millis() - start) < SERIAL_WAIT_MS) {
    delay(10);
  }
}

// ---------------------------------------------------------------------------
// 角色 1：可连接外设
// ---------------------------------------------------------------------------

#if BUMP_TEST_ROLE == 1

static BLEService svc(SERVICE_UUID);
static BLEStringCharacteristic chr(CHAR_UUID, BLERead | BLEWrite, 16);
static char localName[16];
static bool wasConnected = false;

void connTestSetup() {
  serialBegin();

  if (!BLE.begin()) {
    Serial.println("[test] fatal: BLE.begin() failed. Check NINA firmware >= 3.0.1.");
    for (;;) {
      delay(1000);
    }
  }

  snprintf(localName, sizeof(localName), "Bump-Test-%s", BUMP_DEVICE_LABEL);
  BLE.setLocalName(localName);
  BLE.setAdvertisedService(svc);

  svc.addCharacteristic(chr);
  BLE.addService(svc);
  chr.writeValue("BUMP");

  BLE.setConnectable(true);
  BLE.setAdvertisingInterval(100);  // 100 * 0.625ms = 62.5ms
  BLE.advertise();

  logf("[test] role=peripheral name=%s service=%s char=%s waiting for central",
       localName, SERVICE_UUID, CHAR_UUID);
}

void connTestLoop() {
  BLE.poll();

  BLEDevice central = BLE.central();
  if (central) {
    if (!wasConnected) {
      wasConnected = true;
      logf("[test] central connected");
    }
    if (chr.written()) {
      uint8_t buf[16];
      int len = chr.readValue(buf, sizeof(buf) - 1);
      if (len < 0) {
        len = 0;
      }
      buf[len] = '\0';
      logf("[test] characteristic written, value='%s' len=%d", (char*)buf, len);
    }
  } else if (wasConnected) {
    wasConnected = false;
    // ArduinoBLE 断开后不会自动恢复广播，必须显式重新开始
    BLE.advertise();
    logf("[test] central disconnected, advertising again");
  }
}

// ---------------------------------------------------------------------------
// 角色 2：中心
// ---------------------------------------------------------------------------

#elif BUMP_TEST_ROLE == 2

static bool scanning = false;
static uint32_t nextAttemptAt = 0;
static uint32_t scanUntil = 0;

/**
 * 重启 BLE 栈并恢复中心角色配置。
 *
 * NINA 固件在一个扫描会话内对同一地址只上报一次，若该次 scan response
 * 丢失也不会补报；同时扫描后直接 stopScan/重新广播可能让 SPI 命令永远
 * 等不到响应。因此每轮扫描会话前都重启协议栈：既给对端刷新上报机会，
 * 也避开停扫死锁。begin() 首次通常失败，需要重试。
 */
static void restartBleCentral() {
  BLE.end();
  delay(500);
  uint16_t attempts = 0;
  while (!BLE.begin()) {
    if (++attempts == 10) {
      logf("[test] BLE restart slow, still retrying");
    }
    delay(400);
  }
  BLE.setLocalName("Bump-Test-Central");
  BLE.setConnectable(false);
}

void connTestSetup() {
  serialBegin();

  if (!BLE.begin()) {
    Serial.println("[test] fatal: BLE.begin() failed. Check NINA firmware >= 3.0.1.");
    for (;;) {
      delay(1000);
    }
  }

  BLE.setLocalName("Bump-Test-Central");
  BLE.setConnectable(false);

  logf("[test] role=central scanning service=%s char=%s", SERVICE_UUID, CHAR_UUID);
}

static void runConnectTest(BLEDevice peripheral) {
  logf("[test] found name='%s' rssi=%d", peripheral.localName().c_str(),
       peripheral.rssi());

  bool ok = false;
  if (!peripheral.connect()) {
    logf("[test] connect failed");
  } else {
    logf("[test] connected, discovering attributes ...");

    if (!peripheral.discoverAttributes()) {
      logf("[test] attribute discovery failed");
    } else {
      BLECharacteristic characteristic = peripheral.characteristic(CHAR_UUID);
      if (!characteristic) {
        logf("[test] characteristic %s not found", CHAR_UUID);
      } else if (!characteristic.canRead()) {
        logf("[test] characteristic %s not readable", CHAR_UUID);
      } else {
        uint8_t value[16];
        int len = characteristic.readValue(value, sizeof(value) - 1);
        if (len < 0) {
          logf("[test] characteristic read failed");
        } else {
          value[len] = '\0';
          ok = (len == 4 && memcmp(value, "BUMP", 4) == 0);
          logf("[test] read len=%d value='%s'", len, (char*)value);
        }
      }
    }

    // 保持连接一小段时间，确认不是「连上即断」的不稳定链路
    uint32_t holdStart = millis();
    while (peripheral.connected() &&
           (int32_t)(millis() - holdStart) < (int32_t)HOLD_MS) {
      BLE.poll();
      delay(10);
    }

    peripheral.disconnect();
    logf("[test] disconnected");
  }

  logf("[test] RESULT=%s, retry in %lus", ok ? "PASS" : "FAIL",
       (unsigned long)(CONNECT_RETRY_MS / 1000));
  nextAttemptAt = millis() + CONNECT_RETRY_MS;
}

void connTestLoop() {
  BLE.poll();
  uint32_t now = millis();

  if (!scanning) {
    if ((int32_t)(now - nextAttemptAt) < 0) {
      return;
    }
    restartBleCentral();
    delay(300);  // 给控制器留出稳定时间再开扫
    BLE.scanForUuid(SERVICE_UUID, false);
    scanning = true;
    scanUntil = millis() + SCAN_SESSION_MS;
    logf("[test] scanning for %s ...", SERVICE_UUID);
    return;
  }

  BLEDevice peripheral = BLE.available();
  if (peripheral) {
    scanning = false;
    // 同上：不用 stopScan，重启协议栈后 BLEDevice 里的地址仍然有效
    restartBleCentral();
    runConnectTest(peripheral);
    return;
  }

  if ((int32_t)(now - scanUntil) >= 0) {
    // 本会话没发现目标：重启协议栈开始下一轮（同一设备每会话只上报一次）
    scanning = false;
    nextAttemptAt = now;
    logf("[test] no target this session, restarting scan");
  }
}

// ---------------------------------------------------------------------------
// 未选择角色：连接自检模块不参与编译逻辑
// ---------------------------------------------------------------------------

#else

void connTestSetup() {}
void connTestLoop() {}

#endif
