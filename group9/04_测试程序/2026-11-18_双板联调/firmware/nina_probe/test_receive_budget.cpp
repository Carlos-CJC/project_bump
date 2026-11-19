#include "BumpNinaRxBatch.h"
#include <assert.h>
#include <stdio.h>

int main() {
  const uint8_t availableOne[] = {0, 1};
  const uint8_t availableFull[] = {1, 0};
  assert(BumpNinaRxBatch::decodeAvailability(availableOne) == 1);
  assert(BumpNinaRxBatch::decodeAvailability(availableFull) == 256);
  BumpNinaRxBatch receiver;
  unsigned bytes = 0, availabilityChecks = 0, passes = 0;
  auto available = [&] {
    ++availabilityChecks;
    return int(619 - bytes);
  };
  auto read = [&](uint8_t* data, size_t requested) {
    assert(requested == 1);
    *data = uint8_t((bytes * 11) ^ 0xa5);
    ++bytes;
    return 1;
  };
  while (bytes < 619) {
    ++passes;
    for (unsigned count = 0; count < 192 && receiver.available(available); ++count) {
      const uint8_t expected = uint8_t((bytes * 11) ^ 0xa5);
      assert(receiver.read(read) == expected);
    }
  }
  assert(bytes == 619 && passes == 4 && availabilityChecks == 11);

  unsigned checks = 0;
  auto overestimate = [&] { ++checks; return 256; };
  assert(receiver.available(overestimate) == 64);
  assert(receiver.read([](uint8_t*, size_t requested) {
    assert(requested == 1);
    return 0;
  }) == -1);
  assert(receiver.read(read) == -1);
  assert(receiver.available(overestimate) == 64 && checks == 2);
  receiver.clear();
  assert(receiver.available(overestimate) == 64 && checks == 3);
  puts("PASS: big-endian counts 1 and 256; 619 unchanged bytes across four poll budgets; "
       "11 availability queries; failed read never inserts a byte and refreshes quota.");
}
