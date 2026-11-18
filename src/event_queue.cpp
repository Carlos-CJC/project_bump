#include "event_queue.h"

#include "bump_config.h"

static EncEvent s_queue[BUMP_MQTT_QUEUE_CAP];
static size_t s_head;      // 队首下标
static size_t s_count;
static uint32_t s_dropped;

void eventQueueInit() {
  s_head = 0;
  s_count = 0;
  s_dropped = 0;
}

void eventQueuePush(const EncEvent& ev) {
  if (s_count == BUMP_MQTT_QUEUE_CAP) {
    // 新数据更有价值：丢最旧一条。
    s_head = (s_head + 1) % BUMP_MQTT_QUEUE_CAP;
    s_count--;
    s_dropped++;
  }
  s_queue[(s_head + s_count) % BUMP_MQTT_QUEUE_CAP] = ev;
  s_count++;
}

size_t eventQueueCount() {
  return s_count;
}

uint32_t eventQueueDropped() {
  return s_dropped;
}

size_t eventQueuePeek(EncEvent* out, size_t max) {
  size_t n = s_count < max ? s_count : max;
  for (size_t i = 0; i < n; i++) {
    out[i] = s_queue[(s_head + i) % BUMP_MQTT_QUEUE_CAP];
  }
  return n;
}

void eventQueueDrop(size_t n) {
  if (n > s_count) {
    n = s_count;
  }
  s_head = (s_head + n) % BUMP_MQTT_QUEUE_CAP;
  s_count -= n;
}
