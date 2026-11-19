#ifndef BUMP_CORE_H
#define BUMP_CORE_H

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

namespace bump {

enum class State { IDLE, DISCOVERY, SYNC, PAUSED };
enum class AckStatus { STORED, DUPLICATE, REJECTED };
const size_t kQueueCapacity = 128;
const size_t kAdvertisementSize = 29;
const uint64_t kDiscoveryMs = 60000;
const uint64_t kRetryMs = 5000;
const uint64_t kBackgroundRetryMs = 30000;
const uint64_t kDwellMs = 5000;
const uint64_t kSeparationMs = 15000;
const int kRssiThreshold = -70;  // Starting value; requires physical calibration.
const uint8_t kServiceUuid[16] = {
    0x01, 0xba, 0x31, 0x87, 0x20, 0x6e, 0x37, 0x9a,
    0x72, 0x4e, 0xaa, 0x8a, 0x20, 0x01, 0x5b, 0x96};

inline bool validAlias(const char* alias) {
  if (!alias || strlen(alias) != 6) return false;
  for (size_t i = 0; i < 6; ++i)
    if (!(alias[i] >= 'A' && alias[i] <= 'Z') &&
        !(alias[i] >= '0' && alias[i] <= '9')) return false;
  return true;
}

inline bool makeAdvertisement(const char* alias, uint8_t* out, size_t size) {
  if (!validAlias(alias) || !out || size < kAdvertisementSize) return false;
  out[0] = 2; out[1] = 0x01; out[2] = 0x06;
  out[3] = 25; out[4] = 0x21;
  memcpy(out + 5, kServiceUuid, 16);
  out[21] = 1; out[22] = 1;  // Protocol version, DISCOVERY.
  memcpy(out + 23, alias, 6);
  return true;
}

inline bool parseAdvertisement(const uint8_t* data, size_t size,
                               const char* selfAlias, char (&peer)[7]) {
  if (!data || size != kAdvertisementSize || data[0] != 2 ||
      data[1] != 0x01 || data[2] != 0x06 || data[3] != 25 ||
      data[4] != 0x21 || memcmp(data + 5, kServiceUuid, 16) != 0 ||
      data[21] != 1 || data[22] != 1) return false;
  char candidate[7] = {};
  memcpy(candidate, data + 23, 6);
  if (!validAlias(candidate) || strcmp(candidate, selfAlias) == 0) return false;
  memcpy(peer, candidate, 7);
  return true;
}

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

struct Event {
  uint32_t seq = 0;
  uint64_t startMs = 0, endMs = 0;
  int rssiMedian = 0;
  uint16_t sampleCount = 0;
  char peerAlias[7] = {};
};

struct Identity {
  const char* deviceId;
  const char* bootId;
  const char* sessionId;
  const char* alias;
};

inline bool validToken(const char* text) {
  if (!text || !*text) return false;
  for (const char* p = text; *p; ++p)
    if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
          (*p >= '0' && *p <= '9') || *p == '-' || *p == '_')) return false;
  return true;
}

// Only simulated, unknown-clock output exists in this serial scaffold.
// Real radio source + clock synchronisation must be wired before hardware upload.
inline bool eventJson(const Identity& id, const Event& event,
                      char* out, size_t size) {
  if (!validToken(id.deviceId) || !validToken(id.bootId) ||
      !validToken(id.sessionId) || !validAlias(event.peerAlias)) return false;
  int n = snprintf(out, size,
      "{\"schema_version\":1,\"device_id\":\"%s\",\"boot_id\":\"%s\","
      "\"seq\":%lu,\"session_id\":\"%s\",\"peer_alias\":\"%s\","
      "\"start_ms\":%llu,\"end_ms\":%llu,\"start_utc\":null,"
      "\"end_utc\":null,\"time_quality\":\"unknown\","
      "\"rssi_median\":%d,\"sample_count\":%u,\"source\":\"simulated\"}",
      id.deviceId, id.bootId, static_cast<unsigned long>(event.seq),
      id.sessionId, event.peerAlias,
      static_cast<unsigned long long>(event.startMs),
      static_cast<unsigned long long>(event.endMs), event.rssiMedian,
      static_cast<unsigned>(event.sampleCount));
  return n >= 0 && static_cast<size_t>(n) < size;
}

class Badge {
 public:
  explicit Badge(const Identity& identity) : identity_(identity) {}
  State state() const { return state_; }
  size_t queueSize() const { return count_; }
  uint32_t overflowCount() const { return overflow_; }
  uint32_t sampleOverflowCount() const { return sampleOverflow_; }
  const Event* head() const { return count_ ? &queue_[0].event : nullptr; }
  const Identity& identity() const { return identity_; }

  bool shake(uint64_t now) {
    if (state_ != State::IDLE) return false;
    state_ = State::DISCOVERY; windowStart_ = now; resetCandidate();
    return true;
  }

  void togglePause() {
    if (state_ == State::PAUSED) state_ = State::IDLE;
    else { state_ = State::PAUSED; resetCandidate(); }
  }

  void tick(uint64_t now) {
    if (state_ == State::DISCOVERY && now - windowStart_ >= kDiscoveryMs) {
      finishCandidate(); resetCandidate(); burstAttempts_ = 0;
      state_ = count_ ? State::SYNC : State::IDLE;
    }
    if (state_ == State::SYNC && !count_) state_ = State::IDLE;
  }

  bool observe(const uint8_t* data, size_t size, int rssi, uint64_t now) {
    tick(now);
    char peer[7] = {};
    if (state_ != State::DISCOVERY || rssi < -127 || rssi > 20 ||
        !parseAdvertisement(data, size, identity_.alias, peer)) return false;
    // Two-device MVP: exactly one peer per observation segment.
    if (sampleCount_ && strcmp(peer, peerAlias_) != 0) return false;
    if (sampleCount_ && now < lastSeen_) return false;
    if (sampleCount_ && now - lastSeen_ >= kSeparationMs) {
      finishCandidate(); resetCandidate();
    }
    if (!sampleCount_) {
      firstSeen_ = now; memcpy(peerAlias_, peer, 7);
    }
    if (sampleCount_ == UINT16_MAX || histogram_[rssi + 127] == UINT16_MAX) {
      ++sampleOverflow_; return false;
    }
    lastSeen_ = now;
    ++histogram_[rssi + 127]; ++sampleCount_;
    return true;
  }

  bool enqueue(const Event& event) {
    if (!validAlias(event.peerAlias) || event.endMs < event.startMs ||
        event.sampleCount == 0 || event.rssiMedian < -127 ||
        event.rssiMedian > 20) return false;
    if (count_ == kQueueCapacity || nextSeq_ == UINT32_MAX) {
      ++overflow_; return false;
    }
    Item& item = queue_[count_++];
    item = Item{}; item.event = event; item.event.seq = nextSeq_++;
    return true;
  }

  // subscribed means a real SUBACK was received, not only a subscribe request.
  // Calling this counts one attempt; the caller must leave failures queued.
  const Event* nextUpload(uint64_t now, bool subscribed) {
    tick(now);
    if (!subscribed || !count_ || state_ == State::DISCOVERY) return nullptr;
    Item& item = queue_[0];
    const uint64_t interval = item.attempts < 3 ? kRetryMs : kBackgroundRetryMs;
    if (item.sent && (now < item.lastAttempt || now - item.lastAttempt < interval))
      return nullptr;
    item.sent = true; item.lastAttempt = now;
    if (item.attempts < UINT16_MAX) ++item.attempts;
    if (state_ == State::SYNC && ++burstAttempts_ >= 3) state_ = State::IDLE;
    return &item.event;
  }

  bool acknowledge(uint8_t version, const char* deviceId, const char* bootId,
                   uint32_t seq, AckStatus status) {
    if (version != 1 || !count_ || !deviceId || !bootId ||
        strcmp(identity_.deviceId, deviceId) != 0 ||
        strcmp(identity_.bootId, bootId) != 0 ||
        (status != AckStatus::STORED && status != AckStatus::DUPLICATE))
      return false;
    // Only sent events can be removed; delayed ACKs for old heads are harmless.
    if (!queue_[0].sent || queue_[0].event.seq != seq) return false;
    for (size_t i = 1; i < count_; ++i) queue_[i - 1] = queue_[i];
    --count_;
    if (state_ == State::SYNC && !count_) state_ = State::IDLE;
    return true;
  }

 private:
  struct Item {
    Event event;
    uint64_t lastAttempt = 0;
    uint16_t attempts = 0;
    bool sent = false;
  };
  void resetCandidate() {
    memset(histogram_, 0, sizeof(histogram_));
    sampleCount_ = 0; peerAlias_[0] = '\0';
  }
  int median() const {
    uint32_t seen = 0;
    const uint32_t lowerTarget = (sampleCount_ + 1) / 2;
    const uint32_t upperTarget = sampleCount_ / 2 + 1;
    int lower = -127;
    bool foundLower = false;
    for (size_t i = 0; i < 148; ++i) {
      seen += histogram_[i];
      if (!foundLower && seen >= lowerTarget) {
        lower = static_cast<int>(i) - 127; foundLower = true;
      }
      if (seen >= upperTarget) return (lower + static_cast<int>(i) - 127) / 2;
    }
    return -127;
  }
  void finishCandidate() {
    if (sampleCount_ < 3 || lastSeen_ - firstSeen_ < kDwellMs ||
        median() < kRssiThreshold) return;
    Event event;
    event.startMs = firstSeen_; event.endMs = lastSeen_;
    event.rssiMedian = median(); event.sampleCount = sampleCount_;
    memcpy(event.peerAlias, peerAlias_, 7);
    enqueue(event);
  }
  Identity identity_;  // The pointed-to strings must remain immutable/alive.
  State state_ = State::IDLE;
  Item queue_[kQueueCapacity];
  size_t count_ = 0;
  uint32_t nextSeq_ = 1, overflow_ = 0, sampleOverflow_ = 0;
  uint64_t windowStart_ = 0, firstSeen_ = 0, lastSeen_ = 0;
  uint16_t histogram_[148] = {}, sampleCount_ = 0;
  uint8_t burstAttempts_ = 0;
  char peerAlias_[7] = {};
};

}  // namespace bump
#endif
