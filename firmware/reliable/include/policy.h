#pragma once
#include <stdint.h>
namespace policy {
// Subtraction is intentional: comparisons remain valid across millis() rollover.
inline bool elapsed(uint32_t now, uint32_t since, uint32_t duration) {
  return uint32_t(now - since) >= duration;
}
inline uint32_t backoff(unsigned failures) {
  return failures >= 4 ? 60000U : (5000U << failures);
}
enum class Owner { none, usb, tcp };
inline bool canClaim(Owner current, Owner wanted) {
  return current == Owner::none || current == wanted;
}
inline uint32_t bootFailures(uint32_t previous, bool unexpectedReset) {
  return unexpectedReset ? (previous >= 4 ? 4 : previous + 1) : 0;
}
}
