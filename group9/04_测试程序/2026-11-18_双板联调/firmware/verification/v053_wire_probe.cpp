#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include "WireAck.h"
#include "MotionDetector.h"
constexpr size_t kPayloadSize = 512;
constexpr char kFirmware[] = "0.5.3-dual";
const char deviceId[] = "bump-112233445566";
const char bootId[] = "a1b2c3d4";
const char shortId[] = "a1b2c3d4e5f6";
uint32_t fixtureTime = 62000;
uint32_t millis() { return fixtureTime; }
namespace nearfield { uint16_t selfCode() { return 0xa30f; } }
enum class Kind { Encounter, Infrared };
enum class GuardStage : uint8_t {
  None, DiscoveryStart, Serial, Infrared, Imu, BleStart, BleStop, BlePoll, RestoreLed, NinaVersion
};

struct Peer {
  uint8_t shortId[6];
  uint32_t firstAt;
  uint32_t lastAt;
  int32_t rssiSum;
  int8_t lastRssi;
  int8_t txPower;
  uint8_t flags;
  uint16_t samples;
};

struct Pending {
  Kind kind;
  uint32_t sequence;
  uint32_t occurredAt;
  char id[40];
  const char* source;
  bool exported;
  Peer peer;
  uint32_t discoveryStartedAt;
  uint32_t discoveryEndedAt;
  uint16_t peerCode;
};

void hexShort(const uint8_t value[6], char out[13]) {
  snprintf(out, 13, "%02x%02x%02x%02x%02x%02x",
           value[0], value[1], value[2], value[3], value[4], value[5]);
}

bool makeEventPayload(const Pending& item, char (&out)[kPayloadSize]) {
  if (item.kind == Kind::Infrared) {
    const int length = snprintf(out, sizeof(out),
        "{\"device_id\":\"%s\",\"boot_id\":\"%s\",\"event_id\":\"%s\","
        "\"self_code16\":\"%04x\",\"peer_code16\":\"%04x\",\"uptime_ms\":%lu}",
        deviceId, bootId, item.id, nearfield::selfCode(), item.peerCode,
        (unsigned long)item.occurredAt);
    return length > 0 && size_t(length) < sizeof(out);
  }
  const Peer& peer = item.peer;
  char peerShort[13];
  hexShort(peer.shortId, peerShort);
  // Publish-time uptime lets the receiver recover the actual observation time on retry.
  const int length = snprintf(out, sizeof(out),
      "{\"schema\":1,\"device_id\":\"%s\",\"boot_id\":\"%s\",\"fw\":\"%s\","
      "\"batch_id\":\"bt_%s_%s_%08lx_0001\",\"self_short_id\":\"%s\","
      "\"uptime_ms\":%lu,\"time_source\":\"uptime\",\"events\":["
      "{\"event_id\":\"%s\",\"peer_short_id\":\"%s\",\"rssi_avg\":%ld,"
      "\"rssi_last\":%d,\"duration_s\":%lu,\"samples\":%u,\"flags\":%u,"
      "\"peer_tx_power\":%d,\"occurred_uptime_ms\":%lu,"
      "\"discovery_started_uptime_ms\":%lu,\"discovery_ended_uptime_ms\":%lu}]}",
      deviceId, bootId, kFirmware, deviceId + 5, bootId,
      (unsigned long)item.sequence, shortId, (unsigned long)millis(),
      item.id, peerShort, (long)(peer.rssiSum / peer.samples), peer.lastRssi,
      (unsigned long)((peer.lastAt - peer.firstAt) / 1000), peer.samples, peer.flags,
      peer.txPower, (unsigned long)peer.lastAt,
      (unsigned long)item.discoveryStartedAt, (unsigned long)item.discoveryEndedAt);
  return length > 0 && size_t(length) < sizeof(out);
}

int main() {
  assert(bump_ack::matches("{\"event_id\":\"ev_112233445566_a1b2c3d4_00000001\",\"status\":\"stored\"}", "ev_112233445566_a1b2c3d4_00000001"));
  assert(bump_ack::matches(" { \"status\" : \"duplicate\", \"event_id\" : \"ok\" } ", "ok"));
  assert(!bump_ack::matches("{\"event_id\":\"ok\",\"status\":\"rejected\"}", "ok"));
  assert(!bump_ack::matches("{\"event_id\":\"other\",\"status\":\"stored\"}", "ok"));
  assert(!bump_ack::matches("{\"event_id\":\"ok\",\"status\":\"stored\",\"status\":\"stored\"}", "ok"));
  assert(!bump_ack::matches("{\"event_id\":\"ok\",\"status\":\"stored\"}garbage", "ok"));
  assert(!bump_ack::matches("{\"event_id\":\"ok\",\"status\":\"stored\"}", nullptr));
    bump::ShakeDetector detector;
  unsigned triggers = 0;
  for (unsigned now = 0; now < 900; now += 10) {
    const bool peak = (now >= 120 && now < 160) || (now >= 410 && now < 450);
    triggers += detector.sample(0, 0, peak ? 2.1f : 1.0f, now);
  }
  assert(triggers == 1);
  bump::ShakeDetector still;
  for (unsigned now = 0; now < 900; now += 10)
    assert(!still.sample(0, 0, 1, now));
  Pending event {};
  event.kind = Kind::Encounter;
  event.sequence = 1;
  strcpy(event.id, "ev_112233445566_a1b2c3d4_00000001");
  event.peer = Peer{{0xfa, 0xfb, 0xfc, 0xfd, 0xfe, 0xff}, 5000, 9000, -132, -65, -4, 1, 2};
  event.discoveryStartedAt = 1000;
  event.discoveryEndedAt = 61000;
  fixtureTime = 65000;
  char output[kPayloadSize];
  assert(makeEventPayload(event, output));
  puts(output);
  fixtureTime += 45000;
  assert(makeEventPayload(event, output));
  puts(output);
  fixtureTime = UINT32_MAX;
  event.sequence = UINT32_MAX;
  strcpy(event.id, "ev_112233445566_a1b2c3d4_ffffffff");
  event.peer.firstAt = UINT32_MAX - 59600;
  event.peer.lastAt = UINT32_MAX - 1;
  event.peer.rssiSum = 65535 * -70;
  event.peer.lastRssi = -70;
  event.peer.samples = UINT16_MAX;
  event.peer.flags = UINT8_MAX;
  event.peer.txPower = INT8_MIN;
  event.discoveryStartedAt = UINT32_MAX - 60000;
  event.discoveryEndedAt = UINT32_MAX;
  assert(makeEventPayload(event, output));
  puts(output);
  event.kind = Kind::Infrared;
  strcpy(event.id, "ir_112233445566_a1b2c3d4_00000002");
  event.peerCode = 0xc371;
  event.occurredAt = 12000;
  assert(makeEventPayload(event, output));
  puts(output);
}
