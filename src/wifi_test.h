#ifndef BUMP_WIFI_TEST_H
#define BUMP_WIFI_TEST_H

// WiFi 连接自检入口：仅联调时使用，由 BUMP_WIFI_TEST 选择固件。
void wifiTestSetup();
void wifiTestLoop();

#endif  // BUMP_WIFI_TEST_H
