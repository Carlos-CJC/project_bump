#include "BumpDualBadge/ClockRetry.h"
using bump::ClockRetry;

static_assert(ClockRetry::promptOnline(true, 900), "Upper fast bound");
static_assert(!ClockRetry::promptOnline(true, 901), "Slow PubACK");
static_assert(!ClockRetry::promptOnline(false, 1), "Failed presence");

constexpr bool budgetRegression() {
  ClockRetry clock;
  clock.afterTraffic(false, false, 1);
  if (clock.remaining() || clock.due(60000)) return false;
  clock.afterTraffic(true, true, 10);
  if (clock.remaining()) return false;
  clock.afterTraffic(true, false, 100);
  if (clock.remaining() != 2 || clock.due(3599) || !clock.due(3600)) return false;
  clock.afterProbe(false, 3600);
  if (clock.remaining() != 1 || clock.due(3601) || !clock.due(7100)) return false;
  clock.afterProbe(false, 7100);
  if (clock.due(999999) || clock.remaining()) return false;
  clock.afterTraffic(true, false, 0xfffffff0u);
  if (clock.due(3400) || !clock.due(3484)) return false;
  clock.afterProbe(true, 3484);
  if (clock.remaining()) return false;
  clock.afterTraffic(true, false, 4000);
  clock.afterTraffic(false, true, 4010);
  return !clock.remaining();
}
static_assert(budgetRegression(), "No idle loop, fast and slow delivery, two retries, wrap, cancellation");
