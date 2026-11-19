#ifndef BUMP_PAYLOAD_WRITER_H
#define BUMP_PAYLOAD_WRITER_H

#include <stddef.h>
#include <stdint.h>

#include "event_queue.h"

// 手写紧凑 JSON，固定缓冲，禁止动态分配。
// 返回写入字节数；缓冲不足（含截断）返回 0，调用方据此拒绝发布。
// 字段契约见 docs/INTERFACES.md §2.2 / §2.3。

struct BatchMeta {
  const char* batchId;      // bt_<dev12>_<boot8>_<firstseq8>_<count>
  const char* deviceId;     // bump-<12hex>
  const char* bootId;       // 8 hex
  const char* selfShortId;  // 12 hex
  const char* fw;
  uint32_t uptimeMs;
  const char* timeSource;   // "uptime" / "ntp"
};

size_t buildEncounterBatch(char* buf, size_t cap,
                           const BatchMeta& meta,
                           const EncEvent* ev, size_t n);

struct StatusInfo {
  const char* deviceId;
  const char* bootId;
  const char* fw;
  const char* state;        // "online" / "offline"
  uint32_t uptimeS;
  uint8_t queueDepth;
  uint32_t eventsDropped;
  int wifiRssi;             // < 0 时输出，否则视为未知省略
  const char* selfShortId;  // 12 hex；为空则省略该字段
};

size_t buildStatus(char* buf, size_t cap, const StatusInfo& info);

#endif  // BUMP_PAYLOAD_WRITER_H
