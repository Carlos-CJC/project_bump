#ifndef WIRE_ACK_H
#define WIRE_ACK_H

#include <stddef.h>
#include <string.h>

// As in the single-board ACK parser, accept flat JSON and exact tokens only.
namespace bump_ack {
inline void skipSpace(const char*& cursor) {
  while (*cursor == ' ' || *cursor == '\t' || *cursor == '\r' || *cursor == '\n')
    ++cursor;
}

inline bool take(const char*& cursor, char expected) {
  skipSpace(cursor);
  if (*cursor != expected) return false;
  ++cursor;
  return true;
}

inline bool token(const char*& cursor, char* out, size_t capacity) {
  if (!take(cursor, '"')) return false;
  size_t length = 0;
  while (*cursor && *cursor != '"') {
    char c = *cursor++;
    if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
          (c >= '0' && c <= '9') || c == '-' || c == '_') ||
        length + 1 >= capacity) return false;
    out[length++] = c;
  }
  out[length] = '\0';
  if (*cursor != '"') return false;
  ++cursor;
  return true;
}

inline bool matches(const char* payload, const char* expectedId) {
  if (!expectedId || !*expectedId || !take(payload, '{')) return false;
  char eventId[40] = {}, status[16] = {};
  for (unsigned i = 0; i < 2; ++i) {
    char key[16];
    if ((i && !take(payload, ',')) || !token(payload, key, sizeof(key)) ||
        !take(payload, ':')) return false;
    if (!strcmp(key, "event_id") && !*eventId) {
      if (!token(payload, eventId, sizeof(eventId)) || !*eventId) return false;
    } else if (!strcmp(key, "status") && !*status) {
      if (!token(payload, status, sizeof(status)) || !*status) return false;
    } else {
      return false;
    }
  }
  if (!take(payload, '}')) return false;
  skipSpace(payload);
  return !*payload && !strcmp(eventId, expectedId) &&
         (!strcmp(status, "stored") || !strcmp(status, "duplicate"));
}
}  // namespace bump_ack

#endif
