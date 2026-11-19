#include "BumpCore.h"

// 1: serial/scripted acceleration, 0: actual LSM6DSOX read API.
// Both modes still use simulated BLE and a serial-only transport.
#ifndef BUMP_MOCK_IO
#define BUMP_MOCK_IO 1
#endif
#if !BUMP_MOCK_IO
#include <Arduino_LSM6DSOX.h>
#endif

// Enable only after the group has agreed and checked the physical wiring.
#ifndef BUMP_PAUSE_PIN
#define BUMP_PAUSE_PIN -1
#endif

bump::Identity identity = {"badge-a", "serial-only-no-production-boot-id",
                           "demo-001", "A00001"};
bump::Badge badge(identity);
bump::ShakeDetector shakeDetector;
bump::UptimeClock uptimeClock;
uint8_t peerAdvertisement[bump::kAdvertisementSize];
bool subscribedMock = true;
bool imuReady = false;
uint64_t simulatedAt = 0;
int lastButton = HIGH, stableButton = HIGH;
uint64_t buttonChangedAt = 0;

void printState() {
  const char* states[] = {"IDLE", "DISCOVERY", "SYNC", "PAUSED"};
  Serial.print("STATE ");
  Serial.print(states[static_cast<int>(badge.state())]);
  Serial.print(" queued="); Serial.print(badge.queueSize());
  Serial.print(" overflow="); Serial.println(badge.overflowCount());
}

void setup() {
  Serial.begin(115200);  // Never wait for an attached monitor.
  pinMode(LED_BUILTIN, OUTPUT);
#if BUMP_PAUSE_PIN >= 0
  pinMode(BUMP_PAUSE_PIN, INPUT_PULLUP);
#endif
  bump::makeAdvertisement("B00002", peerAdvertisement, sizeof(peerAdvertisement));
#if !BUMP_MOCK_IO
  imuReady = IMU.begin();
  Serial.println(imuReady ? "IMU API initialized; physical readings need validation."
                          : "IMU initialization failed; serial controls remain available.");
#endif
  Serial.println("Bump serial scaffold. ALL radio records are SIMULATED.");
  Serial.println("s=start; p=pause/resume; r=peer packet; a=stored ACK;");
  Serial.println("d=duplicate ACK; x=wrong ACK; o=offline/online; i=synthetic shake.");
  Serial.println("No BLE, Wi-Fi, MQTT or UTC synchronisation adapter is active.");
}

void loop() {
  uint64_t now = uptimeClock.update(millis());
  bump::State previousState = badge.state();
  if (Serial.available()) {
    char command = static_cast<char>(Serial.read());
    if (command == 's') badge.shake(now);
    if (command == 'p') badge.togglePause();
    if (command == 'r')
      badge.observe(peerAdvertisement, sizeof(peerAdvertisement), -55, now);
    if (command == 'i') simulatedAt = now + 1;
    if (command == 'o') subscribedMock = !subscribedMock;
    if (command == 'a' || command == 'd' || command == 'x') {
      const bump::Event* event = badge.head();
      if (event) badge.acknowledge(
          1, identity.deviceId,
          command == 'x' ? "wrong-boot-id" : identity.bootId, event->seq,
          command == 'd' ? bump::AckStatus::DUPLICATE : bump::AckStatus::STORED);
    }
    printState();
  }

  // Optional raw sample input exercises the same detector as actual IMU data.
  if (simulatedAt && now >= simulatedAt) {
    uint64_t age = now - simulatedAt;
    float z = ((age >= 100 && age < 150) || (age >= 400 && age < 450))
                  ? 2.2f : 1.0f;
    if (shakeDetector.sample(0, 0, z, now)) badge.shake(now);
    if (age > 700) simulatedAt = 0;
  }
#if !BUMP_MOCK_IO
  if (!simulatedAt && imuReady && IMU.accelerationAvailable()) {
    float x, y, z;
    if (IMU.readAcceleration(x, y, z) &&
        shakeDetector.sample(x, y, z, now)) badge.shake(now);
  }
#endif

#if BUMP_PAUSE_PIN >= 0
  int button = digitalRead(BUMP_PAUSE_PIN);
  if (button != lastButton) { lastButton = button; buttonChangedAt = now; }
  if (button != stableButton && now - buttonChangedAt >= 40) {
    stableButton = button;
    if (button == LOW) badge.togglePause();
  }
#endif

  // With scripted shake ('i'), peer packets can instead be entered using 'r'.
  badge.tick(now);
  const bump::Event* event = badge.nextUpload(now, subscribedMock);
  if (event) {
    char message[512];
    if (bump::eventJson(identity, *event, message, sizeof(message))) {
      Serial.print("SIMULATED_SEND "); Serial.println(message);
    }
    // Serial output does NOT cause an ACK. 'a'/'d' explicitly inject one.
  }
  bool light = badge.state() == bump::State::DISCOVERY ? (now / 200) % 2 :
               badge.state() == bump::State::PAUSED ? (now / 1000) % 2 :
               badge.queueSize() ? (now / 500) % 2 : false;
  digitalWrite(LED_BUILTIN, light ? HIGH : LOW);
  if (previousState != badge.state()) printState();
  delay(5);
}
