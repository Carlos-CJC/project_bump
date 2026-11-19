#ifndef BUMP_BOOT_ENTROPY_H
#define BUMP_BOOT_ENTROPY_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace boot_identity {
constexpr size_t kBytes = 10;

// Keep one bit from unequal input pairs and discard equal pairs.
template <typename SampleBit>
bool collect(SampleBit sampleBit, uint8_t (&out)[kBytes]) {
  memset(out, 0, sizeof(out));
  size_t written = 0, ones = 0;
  for (unsigned i = 0; i < 1200 && written < kBytes * 8; ++i) {
    const bool first = sampleBit(3);
    if (first == sampleBit(5)) continue;
    const size_t index = written++ / 8;
    out[index] = uint8_t((out[index] << 1) | first);
    ones += first;
  }
  return written == kBytes * 8 && ones >= 8 && ones <= 72;
}
}  // namespace boot_identity

#endif
