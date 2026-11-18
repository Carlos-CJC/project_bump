#include "watchdog.h"

#if defined(DEVICE_WATCHDOG)
#include <mbed.h>

static bool s_running;

bool watchdogStart(uint32_t timeoutMs) {
  mbed::Watchdog& wd = mbed::Watchdog::get_instance();
  uint32_t maxMs = wd.get_max_timeout();
  if (maxMs != 0 && timeoutMs > maxMs) {
    timeoutMs = maxMs;
  }
  // 软复位后看门狗计时器可能仍在跑（RP2040 硬件复位不清除），此时 start()
  // 会返回 false；直接喂狗重置倒计时，避免启动阶段被上一次的残留超时打断。
  s_running = wd.is_running() || (timeoutMs > 0 && wd.start(timeoutMs));
  if (s_running) {
    wd.kick();
  }
  return s_running;
}

void watchdogKick() {
  if (s_running) {
    mbed::Watchdog::get_instance().kick();
  }
}
#else
bool watchdogStart(uint32_t) { return false; }
void watchdogKick() {}
#endif
