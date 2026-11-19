#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "BootEntropy.h"

int main() {
  const uint8_t first[boot_identity::kBytes] =
      {0x7b, 0x89, 0x90, 0x41, 0x56, 0xc3, 0x2a, 0x38, 0x10, 0xbe};
  const uint8_t second[boot_identity::kBytes] =
      {0xe2, 0xb0, 0x8a, 0xf1, 0x80, 0x2d, 0xc3, 0x67, 0x48, 0x92};
  const uint8_t* streams[] = {first, second};
  for (const auto* bits : streams) {
    unsigned call = 0;
    uint8_t result[boot_identity::kBytes];
    auto sample = [&](unsigned delay) {
      const unsigned offset = call++;
      assert(delay == (offset % 2 ? 5 : 3));
      if (offset < 6) return false;  // Equal pairs discarded.
      const unsigned index = (offset - 6) / 2;
      const bool value = (bits[index / 8] >> (7 - index % 8)) & 1;
      return offset % 2 ? !value : value;
    };
    assert(boot_identity::collect(sample, result));
    assert(memcmp(result, bits, sizeof(result)) == 0);
    assert(call == 166);
  }
  assert(memcmp(first, second, 6) != 0 && memcmp(first + 6, second + 6, 4) != 0);
  uint8_t output[boot_identity::kBytes];
  unsigned samples = 0;
  assert(!boot_identity::collect([&](unsigned) { ++samples; return false; }, output));
  assert(samples == 2400);
  samples = 0;
  assert(!boot_identity::collect([&](unsigned) { return (++samples % 2) != 0; }, output));
  assert(samples == 160);
  puts("PASS: two entropy streams produce separate BLE/boot IDs; stuck and biased bits fail closed");
}
