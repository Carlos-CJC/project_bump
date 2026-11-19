#ifndef BUMP_NINA_RX_BATCH_H
#define BUMP_NINA_RX_BATCH_H

#include <stddef.h>
#include <stdint.h>

// NINA 3.0.2 can advertise the requested length even on a short bulk read.
// Reuse an availability count but consume genuine bytes one at a time.
class BumpNinaRxBatch {
 public:
  static constexpr int kMaxCredits = 64;
  static constexpr int decodeAvailability(const uint8_t (&bytes)[2]) {
    return (int(bytes[0]) << 8) | bytes[1];
  }

  void clear() { remaining_ = 0; }

  template <typename Count>
  int available(Count countFromRadio) {
    if (remaining_ == 0) {
      const int count = countFromRadio();
      if (count > 0) remaining_ = count < kMaxCredits ? count : kMaxCredits;
    }
    return remaining_;
  }

  template <typename Read>
  int read(Read readFromRadio) {
    if (remaining_ == 0) return -1;
    uint8_t byte = 0;
    const int received = readFromRadio(&byte, 1);
    if (received != 1) {
      clear();
      return -1;
    }
    --remaining_;
    return byte;
  }

 private:
  int remaining_ = 0;
};

#endif
