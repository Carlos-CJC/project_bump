#include <Arduino.h>

#include "radio.h"

static const uint8_t NINA_RESET_PIN = 24;

void bumpNinaReset() {
  pinMode(NINA_RESET_PIN, OUTPUT);
  digitalWrite(NINA_RESET_PIN, LOW);
  delay(20);
  digitalWrite(NINA_RESET_PIN, HIGH);
  delay(600);
}
