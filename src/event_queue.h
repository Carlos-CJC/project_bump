#ifndef BUMP_EVENT_QUEUE_H
#define BUMP_EVENT_QUEUE_H

#include <stddef.h>
#include <stdint.h>

// 待上报偶遇事件的定长内存队列。队满时丢弃最旧一条并累计丢弃计数。
// 只有收到 MQTT PUBACK 后才调用 drop 出队，失败重试沿用原 event_id。

struct EncEvent {
  uint32_t seq;               // 入队序号，用于 event_id
  uint32_t occurredUptimeMs;  // millis()
  uint8_t  peerId[6];         // 对端广播的短 ID
  int16_t  rssiAvg;
  int16_t  rssiLast;
  uint32_t durationS;
  uint16_t samples;
  uint8_t  flags;
  int8_t   peerTxPower;
};

void eventQueueInit();
void eventQueuePush(const EncEvent& ev);
size_t eventQueueCount();
uint32_t eventQueueDropped();

// 复制队首最多 max 条到 out，不出队；返回实际复制条数。
size_t eventQueuePeek(EncEvent* out, size_t max);

// 发布确认后出队前 n 条。
void eventQueueDrop(size_t n);

#endif  // BUMP_EVENT_QUEUE_H
