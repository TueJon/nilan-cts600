#include "policy.h"
#include <cassert>
#include <limits>
int main() {
  using namespace policy;
  assert(!elapsed(5, 0, 6));
  assert(elapsed(6, 0, 6));
  assert(elapsed(10, std::numeric_limits<uint32_t>::max() - 9, 20));
  assert(backoff(0) == 5000 && backoff(1) == 10000);
  assert(backoff(4) == 60000 && backoff(1000) == 60000);
  assert(canClaim(Owner::none, Owner::usb));
  assert(!canClaim(Owner::tcp, Owner::usb));
  assert(!canClaim(Owner::usb, Owner::tcp));
  assert(bootFailures(3, false) == 0); // An operator reboot must not enter recovery.
  assert(bootFailures(3, true) == 4);
  assert(bootFailures(0xffffffff, true) == 4);
}
