#ifndef NEARFIELD_H
#define NEARFIELD_H

#include <stdint.h>

namespace nearfield {
constexpr uint8_t kWhite = 1;
constexpr uint8_t kBlue = 2;

void begin();
void tick();
uint8_t buttons();
void blue();
void white();
bool active();
bool paired();
bool handshakeBusy();
uint16_t selfCode();
uint16_t peerCode();
void showEncounterHint();
void clearEncounterHint();
bool encounterHintActive();
void suspendLed();
void restoreLed();
void printStatus();
}  // namespace nearfield

#endif
