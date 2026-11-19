#include <assert.h>
#include <stdio.h>
#include <string>
#include "../BumpSingleBoardMotionMqtt/BumpCore.h"
#include "../BumpSingleBoardMotionMqtt/MotionAck.h"

const char* boot = "12345678-1234-1234-1234-123456789abc";
std::string ack(const char* status = "stored") {
  return std::string("{\"v\":1,\"device_id\":\"badge-a\",\"boot_id\":\"") +
      boot + "\",\"seq\":7,\"status\":\"" + status + "\"}";
}
std::string changed(std::string text, const char* before, const char* after) {
  const size_t at = text.find(before);
  assert(at != std::string::npos);
  text.replace(at, strlen(before), after);
  return text;
}
unsigned pulses(unsigned first, unsigned second, float high = 2.2f) {
  bump::ShakeDetector detector;
  unsigned events = 0;
  for (unsigned t = 0; t < 1500; t += 5) {
    float z = ((t >= first && t < first + 50) ||
               (t >= second && t < second + 50)) ? high : 1.0f;
    events += detector.sample(0, 0, z, t);
  }
  return events;
}
int main() {
  assert(motion_ack::matches(ack().c_str(), boot, 7));
  assert(motion_ack::matches(ack("duplicate").c_str(), boot, 7));
  const std::string reordered = std::string(" { \"status\": \"stored\", \"seq\": 7, ") +
      "\"boot_id\":\"" + boot + "\",\"device_id\":\"badge-a\",\"v\":1 }\n";
  assert(motion_ack::matches(reordered.c_str(), boot, 7));
  assert(!motion_ack::matches(ack().c_str(), "wrong-boot", 7));
  assert(!motion_ack::matches(ack().c_str(), boot, 8));
  assert(!motion_ack::matches(ack().c_str(), boot, 0));
  assert(!motion_ack::matches(ack("rejected").c_str(), boot, 7));
  const char* replacements[][2] = {
    {"\"v\":1", "\"v\":2"}, {"badge-a", "badge-b"},
    {"\"seq\":7", "\"seq\":4294967296"}, {"\"seq\":7", "\"seq\":07"},
    {"\"seq\":7", "\"seq\":-7"}, {"\"seq\":7", "\"seq\":7.0"},
    {"\"seq\":7", "\"seq\":\"7\""}, {"\"v\":1", "\"seq\":7"},
    {"\"v\":1,", ""}, {"\"v\":1", "\"extra\":1"},
    {"stored", "sto\\u0072ed"},
    {"\"status\":\"stored\"", "\"status\":\"stored\",\"v\":1"}
  };
  for (const auto& replacement : replacements)
    assert(!motion_ack::matches(changed(ack(), replacement[0], replacement[1]).c_str(), boot, 7));
  assert(!motion_ack::matches((ack() + " trailing").c_str(), boot, 7));
  puts("PASS ack_full_identity_status_schema_and_malformed_rejection");

  assert(pulses(100, 400) == 1);
  assert(pulses(100, 200) == 0);
  assert(pulses(100, 1100) == 0);
  assert(pulses(100, 400, 1.5f) == 0);
  assert(pulses(100, 2000) == 0);
  assert(pulses(100, 250) == 1);  // Inclusive 150 ms.
  assert(pulses(100, 900) == 1);  // Inclusive 800 ms.
  bump::ShakeDetector stationary, sustained, cooldown;
  unsigned steady = 0, held = 0, count = 0;
  for (unsigned t = 0; t < 4000; t += 5) {
    steady += stationary.sample(0, 0, 1, t);
    held += sustained.sample(0, 0, t < 100 ? 1 : 2.2f, t);
    bool high = false;
    for (unsigned peak : {100u, 400u, 700u, 1000u, 2600u, 2900u})
      if (t >= peak && t < peak + 50) high = true;
    count += cooldown.sample(0, 0, high ? 2.2f : 1, t);
  }
  assert(steady == 0 && held == 0 && count == 2);
  bump::ShakeDetector invalid;
  assert(!invalid.sample(0, 0, 1, 0));
  assert(!invalid.sample(0, 0, NAN, 100));
  assert(!invalid.sample(0, 0, 2.2f, 100));
  assert(!invalid.sample(0, 0, 1, 200));
  assert(!invalid.sample(0, 0, 2.2f, 200));
  assert(!invalid.sample(0, 0, 2.2f, 199));
  assert(invalid.sample(0, 0, 2.2f, 400));
  bump::UptimeClock clock;
  assert(clock.update(0xfffffff0u) == 0xfffffff0ULL);
  assert(clock.update(8) == 0x100000008ULL);
  puts("PASS unchanged_motion_detector_positive_negative_cooldown_and_clock");
  puts("ALL_MOTION_PROTOCOL_SOFTWARE_TESTS_PASSED");
}
