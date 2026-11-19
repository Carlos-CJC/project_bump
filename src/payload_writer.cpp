#include "payload_writer.h"

#include <stdarg.h>
#include <stdio.h>

// 追加格式化文本；截断或失败返回 false，整体构造随之作废。
static bool app(char* buf, size_t cap, size_t* len, const char* fmt, ...) {
  if (*len >= cap) {
    return false;
  }
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(buf + *len, cap - *len, fmt, ap);
  va_end(ap);
  if (n < 0 || (size_t)n >= cap - *len) {
    return false;
  }
  *len += (size_t)n;
  return true;
}

static bool appPeerId(char* buf, size_t cap, size_t* len, const uint8_t id[6]) {
  return app(buf, cap, len, "%02x%02x%02x%02x%02x%02x",
             id[0], id[1], id[2], id[3], id[4], id[5]);
}

size_t buildEncounterBatch(char* buf, size_t cap,
                           const BatchMeta& meta,
                           const EncEvent* ev, size_t n) {
  size_t len = 0;
  if (n == 0 || n > 255) {
    return 0;
  }
  if (!app(buf, cap, &len,
           "{\"schema\":1,\"batch_id\":\"%s\",\"device_id\":\"%s\","
           "\"boot_id\":\"%s\",\"self_short_id\":\"%s\",\"fw\":\"%s\","
           "\"uptime_ms\":%lu,\"time_source\":\"%s\",\"events\":[",
           meta.batchId, meta.deviceId, meta.bootId, meta.selfShortId,
           meta.fw, (unsigned long)meta.uptimeMs, meta.timeSource)) {
    return 0;
  }

  // event_id = ev_<dev12>_<boot8>_<seq8>；dev12 取 device_id 去掉 "bump-"。
  const char* dev12 = meta.deviceId + 5;
  char eventId[40];
  for (size_t i = 0; i < n; i++) {
    snprintf(eventId, sizeof(eventId), "ev_%s_%s_%08lu",
             dev12, meta.bootId, (unsigned long)ev[i].seq);
    if (!app(buf, cap, &len, "%s{\"event_id\":\"%s\",\"peer_short_id\":\"",
             i ? "," : "", eventId) ||
        !appPeerId(buf, cap, &len, ev[i].peerId) ||
        !app(buf, cap, &len,
             "\",\"rssi_avg\":%d,\"rssi_last\":%d,\"duration_s\":%lu,"
             "\"samples\":%u,\"flags\":%u,\"peer_tx_power\":%d,"
             "\"occurred_uptime_ms\":%lu}",
             (int)ev[i].rssiAvg, (int)ev[i].rssiLast,
             (unsigned long)ev[i].durationS, (unsigned)ev[i].samples,
             (unsigned)ev[i].flags, (int)ev[i].peerTxPower,
             (unsigned long)ev[i].occurredUptimeMs)) {
      return 0;
    }
  }

  if (!app(buf, cap, &len, "]}")) {
    return 0;
  }
  return len;
}

size_t buildStatus(char* buf, size_t cap, const StatusInfo& info) {
  size_t len = 0;
  if (!app(buf, cap, &len,
           "{\"schema\":1,\"device_id\":\"%s\",\"boot_id\":\"%s\","
           "\"state\":\"%s\",\"fw\":\"%s\",\"uptime_s\":%lu,"
           "\"queue_depth\":%u,\"events_dropped\":%lu",
           info.deviceId, info.bootId, info.state, info.fw,
           (unsigned long)info.uptimeS, (unsigned)info.queueDepth,
           (unsigned long)info.eventsDropped)) {
    return 0;
  }
  if (info.wifiRssi < 0 &&
      !app(buf, cap, &len, ",\"wifi_rssi\":%d", info.wifiRssi)) {
    return 0;
  }
  if (info.selfShortId &&
      !app(buf, cap, &len, ",\"self_short_id\":\"%s\"", info.selfShortId)) {
    return 0;
  }
  if (!app(buf, cap, &len, "}")) {
    return 0;
  }
  return len;
}
