# Reliable dual-transport bridge

This is the replacement for the legacy ESPHome-only image. It keeps the board a
transparent byte tunnel; protocol parsing, sensor provenance, control settings,
and command serialization remain on the server. Native USB continues to work
while Wi-Fi retries. Neither firmware nor transport recovery changes the unit's
fan, mode, setpoint or room-temperature source.

## Recovery behavior

- Wi-Fi SSID/password are seeded into one atomic NVS record, retained across
  power loss. Normal reconnects never rewrite flash or erase credentials.
- Connection attempts last 20 seconds, then back off 5, 10, 20, 40, 60 seconds.
  No channel/BSSID is pinned, so AP replacements and channel changes can recover.
- A password-protected `Nilan-Bridge Recovery` AP and authenticated web interface
  are available even without home Wi-Fi. Initial AP channel is 1; when the station
  connects the shared radio follows its channel. Recovery AP address is
  `192.168.4.1`. A functioning RF path is still required to reach it.
  While an operator is connected to the recovery AP, background connection
  attempts pause to keep that session stable; an explicit Wi-Fi credential test
  can still change channels. Disconnect from recovery Wi-Fi to resume retries.
- A disconnected radio is reinitialized every ten minutes, except during an AP
  operator session, active TCP use or upload. Wi-Fi failure alone never reboots
  the device or interrupts working USB. A ten-second loop watchdog handles an
  actual software stall.
- Four crash/watchdog resets without two minutes of stable runtime enter recovery mode: AP/web
  firmware update remain available, bus forwarding and home-Wi-Fi attempts stop.
  Intentional administrator/OTA reboots do not count. This is a boot-loop escape,
  not proof of working physical telemetry.
- Wi-Fi changes are candidates: retain old credentials until DHCP and a continuous
  15-second connection succeed, verify the NVS write, otherwise revert after
  60 seconds. A power cut before commit retains the previous credential record.
- UART framing is the verified `19200 8N1`, TX17/RX18/RTS21, RS485 half-duplex.
  Native USB CDC carries only raw bytes; boot/debug messages never share it.

## Single controller and failure boundaries

The first active USB or allowlisted TCP client owns UART exclusively. The other
transport is rejected/drained; it cannot interleave protocol frames. USB ownership
ends when the port closes; either lease expires after 120 seconds without a new
request. Normal polling must be well below that timeout (default 30 seconds).
Orphan UART replies are flushed at ownership boundaries. Short writes and network
backpressure close the session; buffered commands are never replayed on reconnect.

This is transport arbitration, not automatic host routing. Configure the server
for exactly one selected endpoint. The optional USB proxy does not make a laptop
independent: that host must remain powered and connected. A fixed, always-on USB
host or a verified working home WLAN is needed to retire the laptop dependency.

TCP/6638 accepts only `CONTROLLER_IP`. It is a raw, unencrypted industrial stream;
source IP is not cryptographic authentication. Keep it on a trusted LAN/VLAN with
no public forwarding. The HTTP service uses administrator Basic authentication,
rejects cross-origin browser writes, and never returns stored passwords. Use it
over the protected recovery AP or trusted LAN only; it does not offer HTTPS.
NVS credentials and firmware are not encrypted at rest. Secure-boot/eFuse changes
are intentionally excluded from routine deployment because they alter recovery.

## Remote diagnosis and updates

Open `http://nilan-bridge.local/` or the current DHCP address; mDNS requires a
same-link resolver and is not assumed to work inside Docker. Username is `admin`.
The password comes from protected `ota_password` at build time. Recovery AP uses
the separate `ap_password`. `/status` reports connection/channel/BSSID/RSSI,
retry/reset causes, uptime/boot count, free heap, owner, UART counters, rejected
clients and the last sixteen recovery events. It contains no SSID/password.

Authenticated endpoints:

| Endpoint | Action |
| --- | --- |
| `GET /status` | Diagnostic JSON |
| `POST /wifi` | Test and persist `ssid` and `password` form fields |
| `POST /wifi/retry` | Queue safe radio reinitialization |
| `POST /reboot` | Explicit device reboot; interrupts the transport |
| `POST /update` | Multipart `firmware` application `.bin`; interrupts transport |

OTA writes the inactive app slot and selects it only after successful image
validation. Interrupted/invalid transfers retain the current app. The firmware
also aborts an abandoned upload after 30 seconds of inactivity,
releases maintenance mode, and discards queued serial bytes. This pinned
Arduino bootloader does **not** provide automatic rollback after a new image
successfully installs but fails at runtime. Boot-loop recovery still depends on
the new program reaching its recovery setup. Retain the known-good USB image and
full pre-change flash backup. Never advertise an untested rollback guarantee.

## Build, deploy, verify

The dependency versions are exact. This Arduino release was independently tested
on the hardware during recovery; moving to a newer framework is a separate
hardware-validation change, not an unreviewed dependency update.

1. Install PlatformIO and PyYAML on the build host.
2. `python3 seed_secrets.py /protected/firmware/secrets.yaml --controller-ip <server-ip>`.
   Header, build output and binaries contain secrets; keep them private and outside
   Git/artifact uploads. The generated file is mode 0600 and ignored.
3. `pio run`. Native policy checks:
   `g++ -std=c++17 -I include tests/policy_test.cpp -o /private/path/policy-test`
   and run that executable.
4. First migration must flash the matching bootloader, partition table and app
   from the new build. Do not upload only the app into an unknown old partition
   layout. Back up the current full flash and NVS first. Never use `erase-flash`.
5. Stop/release the USB proxy before flashing. Native CDC bootloader entry uses
   a 1200-baud open/close, then esptool `--before usb-reset`; the USB port may
   disappear immediately, producing harmless EIO. Use a stable by-id path once
   the firmware has re-enumerated. Restart the sole server transport afterward.
6. Verify fresh complete physical samples and `/readyz`, unchanged control
   settings, continued room-source feed and actual T15 readback. Test proxy
   reconnect and software reboot recovery, then bad-password fallback, network
   loss/rejoin, exclusive transport ownership, authenticated/unauthenticated
   status and interrupted OTA. Do not claim those live checks passed from build
   or unit tests alone. A live rollout interrupts the ventilation control link;
   obtain the required production approval first.
7. Keep a verified DHCP reservation or configure a deliberately managed endpoint.
   Do not invent a new static bridge address or rely on historical .15/.30 notes.
   Remove the USB proxy alias before assigning that same address to the ESP.

Wireless reconnection cannot fix an AP that is not broadcasting, severe
interference, antenna/power faults or a broken RF channel. Expose and alert on
those conditions instead of hiding them behind endless whole-device reboots.
