#ifndef MOTION_ACK_H
#define MOTION_ACK_H
#include <stdint.h>
#include <string.h>

// Deliberately limited ACK JSON: exactly five flat fields, no escaped strings.
namespace motion_ack {
inline void whitespace(const char*& p) {
  while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') ++p;
}
inline bool take(const char*& p, char expected) {
  whitespace(p);
  if (*p != expected) return false;
  ++p; return true;
}
inline bool token(const char*& p, char* out, size_t size) {
  if (!take(p, '"')) return false;
  size_t n = 0;
  while (*p && *p != '"') {
    char c = *p++;
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_') || n + 1 >= size)
      return false;
    out[n++] = c;
  }
  out[n] = '\0';
  return take(p, '"');
}
inline bool number(const char*& p, uint32_t& value) {
  whitespace(p);
  if (*p < '0' || *p > '9') return false;
  if (*p == '0' && p[1] >= '0' && p[1] <= '9') return false;
  value = 0;
  while (*p >= '0' && *p <= '9') {
    uint32_t digit = static_cast<uint32_t>(*p++ - '0');
    if (value > (UINT32_MAX - digit) / 10) return false;
    value = value * 10 + digit;
  }
  return true;
}
inline bool matches(const char* json, const char* bootId, uint32_t sequence) {
  const char* p = json;
  char key[16], device[16] = {}, boot[40] = {}, status[16] = {};
  uint32_t version = 0, seq = 0;
  unsigned seen = 0;
  if (!take(p, '{')) return false;
  for (unsigned i = 0; i < 5; ++i) {
    if (i && !take(p, ',')) return false;
    if (!token(p, key, sizeof(key)) || !take(p, ':')) return false;
    unsigned bit = 0;
    if (!strcmp(key, "v")) {
      bit = 1; if (!number(p, version)) return false;
    } else if (!strcmp(key, "device_id")) {
      bit = 2; if (!token(p, device, sizeof(device))) return false;
    } else if (!strcmp(key, "boot_id")) {
      bit = 4; if (!token(p, boot, sizeof(boot))) return false;
    } else if (!strcmp(key, "seq")) {
      bit = 8; if (!number(p, seq)) return false;
    } else if (!strcmp(key, "status")) {
      bit = 16; if (!token(p, status, sizeof(status))) return false;
    } else return false;
    if (seen & bit) return false;
    seen |= bit;
  }
  if (!take(p, '}')) return false;
  whitespace(p);
  return !*p && seen == 31 && version == 1 && sequence && seq == sequence &&
         !strcmp(device, "badge-a") && !strcmp(boot, bootId) &&
         (!strcmp(status, "stored") || !strcmp(status, "duplicate"));
}
}
#endif
