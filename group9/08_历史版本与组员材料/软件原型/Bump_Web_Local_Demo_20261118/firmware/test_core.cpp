#include "BumpBadge/BumpCore.h"
#include <assert.h>
#include <initializer_list>

using namespace bump;
const Identity identity = {"badge-a", "host-test-boot-1", "demo-001", "A00001"};

Event event() {
  Event value;
  value.startMs = 1000; value.endMs = 7000;
  value.rssiMedian = -55; value.sampleCount = 12;
  memcpy(value.peerAlias, "B00002", 7);
  return value;
}

void advertisement() {
  uint8_t packet[kAdvertisementSize] = {};
  char alias[7] = {};
  assert(makeAdvertisement("B00002", packet, sizeof(packet)));
  assert(packet[3] == 25 && packet[4] == 0x21 && sizeof(packet) == 29);
  assert(parseAdvertisement(packet, sizeof(packet), "A00001", alias));
  assert(strcmp(alias, "B00002") == 0);
  assert(!parseAdvertisement(packet, 28, "A00001", alias));
  assert(!parseAdvertisement(packet, 29, "B00002", alias));
  assert(!makeAdvertisement("TOOLONG", packet, sizeof(packet)));
  assert(!makeAdvertisement("A0000!", packet, sizeof(packet)));
  assert(!makeAdvertisement("A00001", packet, 28));
  for (size_t index : {size_t(0), size_t(3), size_t(4), size_t(5),
                       size_t(21), size_t(22), size_t(23)}) {
    uint8_t old = packet[index];
    packet[index] = 0;
    assert(!parseAdvertisement(packet, 29, "A00001", alias));
    packet[index] = old;
  }
}

void ackAndRetry() {
  Badge badge(identity);
  assert(badge.enqueue(event()));
  assert(!badge.acknowledge(1, "badge-a", "host-test-boot-1", 1, AckStatus::STORED));
  assert(!badge.nextUpload(0, false));
  const Event* sent = badge.nextUpload(0, true);
  assert(sent && sent->seq == 1);
  char first[512], retry[512];
  assert(eventJson(identity, *sent, first, sizeof(first)));
  assert(!badge.acknowledge(1, "badge-b", "host-test-boot-1", 1, AckStatus::STORED));
  assert(!badge.acknowledge(1, "badge-a", "old-boot", 1, AckStatus::STORED));
  assert(!badge.acknowledge(1, "badge-a", "host-test-boot-1", 2, AckStatus::STORED));
  assert(!badge.acknowledge(2, "badge-a", "host-test-boot-1", 1, AckStatus::STORED));
  assert(!badge.acknowledge(1, "badge-a", "host-test-boot-1", 1, AckStatus::REJECTED));
  assert(!badge.nextUpload(4999, true));
  sent = badge.nextUpload(5000, true);
  assert(sent && eventJson(identity, *sent, retry, sizeof(retry)));
  assert(strcmp(first, retry) == 0);
  assert(badge.nextUpload(10000, true));
  assert(!badge.nextUpload(15000, true));
  assert(badge.nextUpload(40000, true));
  assert(badge.acknowledge(1, "badge-a", "host-test-boot-1", 1, AckStatus::DUPLICATE));
  assert(badge.queueSize() == 0);
  assert(!badge.acknowledge(1, "badge-a", "host-test-boot-1", 1, AckStatus::STORED));
}

void queueAndReboot() {
  Badge badge(identity);
  for (size_t i = 0; i < kQueueCapacity; ++i) assert(badge.enqueue(event()));
  assert(!badge.enqueue(event()));
  assert(badge.queueSize() == 128 && badge.overflowCount() == 1);
  assert(badge.nextUpload(0, true)->seq == 1);
  assert(badge.acknowledge(1, identity.deviceId, identity.bootId, 1, AckStatus::STORED));
  assert(badge.enqueue(event()));
  assert(badge.head()->seq == 2);
  const Identity afterBoot = {"badge-a", "host-test-boot-2", "demo-001", "A00001"};
  Badge restarted(afterBoot);
  assert(restarted.queueSize() == 0);  // Power loss is deliberately not durable.
  assert(restarted.enqueue(event()));
  assert(restarted.nextUpload(0, true)->seq == 1);
  assert(!restarted.acknowledge(1, "badge-a", identity.bootId, 1, AckStatus::STORED));
}

void stateAndPause() {
  Badge badge(identity);
  uint8_t packet[29];
  assert(makeAdvertisement("B00002", packet, sizeof(packet)));
  assert(badge.shake(0));
  assert(!badge.shake(10000));  // Repeated shakes cannot extend the window.
  assert(!badge.nextUpload(10000, true));
  assert(badge.observe(packet, sizeof(packet), -60, 10000));
  assert(badge.observe(packet, sizeof(packet), -55, 12500));
  assert(badge.observe(packet, sizeof(packet), -50, 15000));
  badge.tick(59999);
  assert(badge.state() == State::DISCOVERY && badge.queueSize() == 0);
  badge.tick(60000);
  assert(badge.state() == State::SYNC && badge.queueSize() == 1);
  assert(badge.head()->startMs == 10000 && badge.head()->endMs == 15000);
  assert(badge.head()->rssiMedian == -55);
  badge.togglePause();
  assert(badge.state() == State::PAUSED);
  assert(!badge.shake(60001));
  assert(!badge.observe(packet, sizeof(packet), -50, 60001));
  assert(badge.nextUpload(60001, true));
  assert(badge.acknowledge(1, identity.deviceId, identity.bootId, 1, AckStatus::STORED));
  assert(badge.state() == State::PAUSED);
  badge.togglePause();
  assert(badge.state() == State::IDLE);
  assert(badge.shake(70000));
  assert(badge.observe(packet, sizeof(packet), -50, 70000));
  badge.togglePause();
  badge.tick(140000);
  assert(badge.queueSize() == 0);  // Pause discards the unfinished segment.
}

void dwellAndMedian() {
  uint8_t packet[29];
  assert(makeAdvertisement("B00002", packet, sizeof(packet)));
  Badge shortStay(identity);
  shortStay.shake(0);
  shortStay.observe(packet, 29, -50, 0);
  shortStay.observe(packet, 29, -50, 1000);
  shortStay.observe(packet, 29, -50, 2000);
  shortStay.tick(60000);
  assert(shortStay.queueSize() == 0);
  Badge weak(identity);
  weak.shake(0);
  for (uint64_t now : {uint64_t(0), uint64_t(2500), uint64_t(5000)})
    weak.observe(packet, 29, -80, now);
  weak.tick(60000);
  assert(weak.queueSize() == 0);
  Badge separate(identity);
  separate.shake(0);
  separate.observe(packet, 29, -60, 0);
  separate.observe(packet, 29, -55, 2500);
  separate.observe(packet, 29, -50, 5000);
  separate.observe(packet, 29, -40, 6000);
  separate.observe(packet, 29, -50, 21000);
  separate.observe(packet, 29, -50, 24000);
  separate.observe(packet, 29, -50, 27000);
  separate.tick(60000);
  assert(separate.queueSize() == 2);
  assert(separate.head()->rssiMedian == -52);  // (-55 + -50) / 2, toward zero.
  assert(separate.head()->sampleCount == 4 && separate.head()->endMs == 6000);
}

void shakeAndClock() {
  ShakeDetector detector;
  assert(!detector.sample(0, 0, 1, 0));
  assert(!detector.sample(0, 0, 2.2f, 100));
  assert(!detector.sample(0, 0, 1, 200));
  assert(!detector.sample(0, 0, NAN, 250));
  assert(detector.sample(0, 0, 2.2f, 400));
  assert(!detector.sample(0, 0, 1, 500));
  assert(!detector.sample(0, 0, 2.2f, 700));  // Cooldown.
  assert(!detector.sample(0, 0, 2.2f, 700));  // Reused sample.
  UptimeClock clock;
  assert(clock.update(0xfffffff0) == 0xfffffff0ULL);
  assert(clock.update(8) == 0x100000008ULL);
  assert(clock.update(16) == 0x100000010ULL);
}

int main(int argc, char** argv) {
  if (argc == 2 && strcmp(argv[1], "json") == 0) {
    Badge badge(identity);
    assert(badge.enqueue(event()));
    char output[512], small[10];
    assert(!eventJson(identity, *badge.head(), small, sizeof(small)));
    assert(eventJson(identity, *badge.head(), output, sizeof(output)));
    puts(output);
    return 0;
  }
  advertisement(); puts("PASS advertisement_roundtrip_and_rejection");
  ackAndRetry(); puts("PASS matching_ack_and_timed_immutable_retry");
  queueAndReboot(); puts("PASS_bounded_queue_overflow_and_reboot_boundary");
  stateAndPause(); puts("PASS_window_pause_and_backlog");
  dwellAndMedian(); puts("PASS_dwell_rssi_median_and_separation");
  shakeAndClock(); puts("PASS_shake_cooldown_and_millis_wrap");
  printf("ALL_FIRMWARE_CORE_TESTS_PASSED sizeof(Badge)=%zu\n", sizeof(Badge));
  return 0;
}
