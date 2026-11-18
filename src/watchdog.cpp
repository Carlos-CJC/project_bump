#include "watchdog.h"

#if defined(DEVICE_WATCHDOG)
#include <mbed.h>

static bool s_running;

void watchdogStart(uint32_t timeoutMs) {
  mbed::Watchdog& wd = mbed::Watchdog::get_instance();
  uint32_t maxMs = wd.get_max_timeout();
  if (maxMs != 0 && timeoutMs > maxMs) {
    timeoutMs = maxMs;
  }
  s_running = timeoutMs > 0 && wd.start(timeoutMs);
}

void watchdogKick() {
  if (s_running) {
    mbed::Watchdog::get_instance().kick();
  }
}
#else
void watchdogStart(uint32_t) {}
void watchdogKick() {}
#endif
