#pragma once
#include <stdint.h>

namespace bump {
class ClockRetry {
 public:
  static constexpr uint32_t goodOnlineMs = 900;
  static constexpr uint32_t delayMs = 3500;
  static constexpr uint8_t limit = 2;

  static constexpr bool promptOnline(bool acknowledged, uint32_t elapsedMs) {
    return acknowledged && elapsedMs <= goodOnlineMs;
  }
  constexpr bool due(uint32_t now) const {
    return attemptsLeft && int32_t(now - nextAt) >= 0;
  }
  constexpr void afterTraffic(bool clearedRealEvents, bool prompt, uint32_t now) {
    if (prompt) attemptsLeft = 0;
    else if (clearedRealEvents) {
      attemptsLeft = limit;
      nextAt = now + delayMs;
    }
  }
  constexpr void afterProbe(bool prompt, uint32_t now) {
    if (prompt) attemptsLeft = 0;
    else if (attemptsLeft) --attemptsLeft;
    nextAt = now + delayMs;
  }
  constexpr uint8_t remaining() const { return attemptsLeft; }

 private:
  uint8_t attemptsLeft = 0;
  uint32_t nextAt = 0;
};
}
