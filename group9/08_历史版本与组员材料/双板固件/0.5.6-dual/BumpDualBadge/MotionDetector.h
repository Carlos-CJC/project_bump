#ifndef BUMP_MOTION_DETECTOR_H
#define BUMP_MOTION_DETECTOR_H

#include <math.h>
#include <stdint.h>

namespace bump {
// Motion logic reused from the 2026-11-15 BumpSingleBoardMotionMqtt sketch.
// Extend Arduino millis() across its 32-bit wrap. Call at least once per wrap.
class UptimeClock {
 public:
  uint64_t update(uint32_t raw) {
    if (raw < previous_) high_ += (uint64_t(1) << 32);
    previous_ = raw;
    return high_ + raw;
  }
 private:
  uint32_t previous_ = 0;
  uint64_t high_ = 0;
};

// Two rising acceleration peaks, separated by 150..800 ms, then 2 s cooldown.
// Uses magnitude minus a slowly changing gravity baseline. Values are in g.
class ShakeDetector {
 public:
  bool sample(float x, float y, float z, uint64_t now) {
    if (!isfinite(x) || !isfinite(y) || !isfinite(z) ||
        (hasSample_ && now <= lastSample_)) return false;
    float magnitude = sqrtf(x*x + y*y + z*z);
    if (!hasSample_) {
      baseline_ = magnitude; hasSample_ = true; lastSample_ = now;
      return false;
    }
    lastSample_ = now;
    float motion = fabsf(magnitude - baseline_);
    baseline_ += 0.02f * (magnitude - baseline_);
    if (motion < 0.35f) above_ = false;
    if (now < cooldownUntil_ || motion < 0.8f || above_) return false;
    above_ = true;
    if (firstPeak_ && now - peakAt_ >= 150 && now - peakAt_ <= 800) {
      firstPeak_ = false; cooldownUntil_ = now + 2000;
      return true;
    }
    if (!firstPeak_ || now - peakAt_ > 800) {
      firstPeak_ = true; peakAt_ = now;
    }
    return false;
  }
 private:
  bool hasSample_ = false, above_ = false, firstPeak_ = false;
  float baseline_ = 1.0f;
  uint64_t lastSample_ = 0, peakAt_ = 0, cooldownUntil_ = 0;
};

}  // namespace bump

#endif
