#ifndef BUMP_CONFIG_H
#define BUMP_CONFIG_H

// MQTT 集成的编译开关与默认参数。实际值优先取构建参数（-D），此处仅为兜底。
// 接口契约见 docs/INTERFACES.md，实施细节见 docs/FIRMWARE_MQTT_PLAN.md。

#ifndef BUMP_MQTT
#define BUMP_MQTT 0
#endif

#ifndef BUMP_FW_VERSION
#define BUMP_FW_VERSION "dev"
#endif

#ifndef BUMP_WIFI_SSID
#define BUMP_WIFI_SSID ""
#endif
#ifndef BUMP_WIFI_PASS
#define BUMP_WIFI_PASS ""
#endif

#ifndef BUMP_MQTT_HOST
#define BUMP_MQTT_HOST ""
#endif
#ifndef BUMP_MQTT_PORT
#define BUMP_MQTT_PORT 8883
#endif
#ifndef BUMP_MQTT_PASS
#define BUMP_MQTT_PASS ""
#endif

#ifndef BUMP_MQTT_QUEUE_CAP
#define BUMP_MQTT_QUEUE_CAP 16
#endif
#ifndef BUMP_MQTT_BATCH_MAX
#define BUMP_MQTT_BATCH_MAX 8
#endif

#ifndef BUMP_UPLOAD_BUDGET_MS
#define BUMP_UPLOAD_BUDGET_MS 40000
#endif
#ifndef BUMP_WIFI_TIMEOUT_MS
#define BUMP_WIFI_TIMEOUT_MS 20000
#endif
#ifndef BUMP_MQTT_TIMEOUT_MS
#define BUMP_MQTT_TIMEOUT_MS 10000
#endif
#ifndef BUMP_UPLOAD_MIN_INTERVAL_MS
#define BUMP_UPLOAD_MIN_INTERVAL_MS 0
#endif

#ifndef BUMP_NTP
#define BUMP_NTP 0
#endif
#ifndef BUMP_MQTT_CONFIG
#define BUMP_MQTT_CONFIG 0
#endif

#endif  // BUMP_CONFIG_H
