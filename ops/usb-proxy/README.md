# Optional USB fallback proxy

This service exposes one USB serial stream as the existing raw TCP bridge
protocol. Use it only as an alternative transport. The firmware must ensure a
single UART owner when USB and Wi-Fi are both available; the proxy alone cannot
enforce arbitration inside the ESP. Do not run another TCP bridge client or a
second USB reader at the same time.

The service uses an explicit stable `/dev/serial/by-id/...` path, takes both an
exclusive tty open and an advisory file lock, accepts one allowed IPv4 client at
a time, and forwards directly in bounded chunks. It keeps no application queue.
On a serial unplug, write timeout, or TCP failure, it closes both sides. It never
replays bytes on a reconnected device or client. Serial bytes and credentials are
never logged. Periodic JSON journal records expose byte counters, connection
counts, active peer, disconnects, and device presence.

## Install

1. Install `python3-serial`; create an unprivileged `nilan-proxy` system user.
   Add it to `dialout` so it can use the USB CDC device.
2. Copy `nilan_usb_proxy.py` to `/usr/local/libexec/nilan-usb-proxy.py`,
   `manage_ip_alias.py` to `/usr/local/libexec/nilan-usb-proxy-alias.py`, and
   `nilan-usb-proxy.service` to `/etc/systemd/system/`.
3. Copy `nilan-usb-proxy.env.example` to `/etc/default/nilan-usb-proxy` (mode
   0640, root-owned), set the stable device symlink, server bind address, and
   narrow client allowlist. Keep the optional address alias disabled unless its
   address has been checked free and its interface chosen deliberately.
4. Run `systemctl daemon-reload`, then `systemctl enable --now nilan-usb-proxy`.
   Inspect `systemctl status nilan-usb-proxy` and
   `journalctl -u nilan-usb-proxy`.

The daemon checks the device when the server opens a TCP session. If USB is
unplugged while a session is active, it drops that TCP session; systemd restarts
the daemon on an unexpected process exit, and the normal server-side client
reconnect loop retries. The systemd service itself does not reboot the ESP or
reset the Nilan appliance.

The optional alias manager records ownership only when it adds the address
itself. It refuses to commandeer an address already present on another
interface, and service shutdown removes only the address it owns. Do not enable
it when another network manager already owns that IP.

## Verification and recovery

Run `pytest -q tests/test_usb_proxy.py`. For a live check, confirm the expected
peer is accepted, counters increase in both directions during real polling, and
the physical API reports fresh data. Disconnect and reconnect the USB cable:
the session should close, never replay buffered bytes, and the server should
reconnect after the device is available. Stop with `systemctl disable --now
nilan-usb-proxy`; this is reversible and does not remove a pre-existing IP.

The service is optional. Wi-Fi remains the preferred independent transport;
USB fallback still depends on the host that physically connects to the ESP.
