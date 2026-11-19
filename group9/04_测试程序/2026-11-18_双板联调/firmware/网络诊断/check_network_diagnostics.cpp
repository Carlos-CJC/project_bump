
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
struct IPAddress {
  uint8_t address[4];
  uint8_t operator[](size_t index) const { return address[index]; }
};
uint32_t now = 13;
uint32_t millis() { return now; }
class WiFiClient {
 public:
  virtual int connect(IPAddress address, uint16_t port) {
    assert(address[3] == 1 && port == 18883);
    now += duration;
    return outcome;
  }
  uint32_t duration = 2500;
  int outcome = 1;
};
struct TestSerial {
  void println(const char* value) { puts(value); }
} Serial;
struct TestWiFi {
  IPAddress localIP() { now += 2; return {{10, 23, 45, 61}}; }
  long RSSI() { now += 4; return -63; }
} WiFi;
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


int main() {
  WiFiClient* base = &network;
  assert(base->connect(IPAddress{{10,23,45,1}}, 18883) == 1);
  assert(network.elapsedMs == 2500 && network.result == 1);
  logSyncStage("tcp_connect", network.elapsedMs, network.result);
  network.duration = 12000;
  network.outcome = 0;
  assert(base->connect(IPAddress{{10,23,45,1}}, 18883) == 0);
  assert(network.elapsedMs == 12000 && network.result == 0);
  logSyncStage("tcp_connect", network.elapsedMs, network.result);
  logWifiLink();
}
