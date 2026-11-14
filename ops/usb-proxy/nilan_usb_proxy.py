#!/usr/bin/env python3
"""Single-client, raw TCP <-> USB serial bridge for the optional Nilan fallback."""

from __future__ import annotations

import fcntl
import ipaddress
import json
import logging
import os
import select
import signal
import socket
import sys
import threading
import termios
import time
from dataclasses import dataclass
from typing import BinaryIO

import serial

LOG = logging.getLogger("nilan-usb-proxy")
MAX_CHUNK = 512
SERIAL_READ_TIMEOUT = 0.02
SERIAL_WRITE_TIMEOUT = 2.0
SOCKET_WRITE_TIMEOUT = 2.0
SERIAL_RETRY_SECONDS = 2.0
STATUS_SECONDS = 60.0


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Config:
    bind: str
    port: int
    device: str
    baud: int
    allow_networks: tuple[ipaddress.IPv4Network, ...]
    status_seconds: int

    @classmethod
    def from_env(cls) -> "Config":
        bind = os.getenv("NILAN_USB_BIND", "127.0.0.1")
        if ipaddress.ip_address(bind).version != 4:
            raise ValueError("NILAN_USB_BIND must be an IPv4 address")
        raw_allow = os.getenv("NILAN_USB_ALLOW_CIDRS", "")
        if not raw_allow.strip():
            raise ValueError("NILAN_USB_ALLOW_CIDRS must explicitly allow TCP clients")
        networks = tuple(
            ipaddress.ip_network(value.strip(), strict=False)
            for value in raw_allow.split(",")
            if value.strip()
        )
        if not networks or any(net.version != 4 for net in networks):
            raise ValueError("NILAN_USB_ALLOW_CIDRS must contain IPv4 CIDRs")
        device = os.getenv("NILAN_USB_DEVICE", "")
        if not device.startswith("/dev/"):
            raise ValueError("NILAN_USB_DEVICE must name a serial device under /dev")
        return cls(
            bind=bind,
            port=env_int("NILAN_USB_PORT", 6638, 1, 65535),
            device=device,
            baud=env_int("NILAN_USB_BAUD", 115200, 1200, 921600),
            allow_networks=networks,
            status_seconds=env_int("NILAN_USB_STATUS_SECONDS", 60, 5, 3600),
        )

    def allows(self, address: str) -> bool:
        ip = ipaddress.ip_address(address)
        return ip.version == 4 and any(ip in network for network in self.allow_networks)


class Stats:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.values: dict[str, int | str | None] = {
            "client_connections": 0,
            "rejected_clients": 0,
            "serial_open_failures": 0,
            "serial_disconnects": 0,
            "client_disconnects": 0,
            "tcp_to_usb_bytes": 0,
            "usb_to_tcp_bytes": 0,
            "active_client": None,
            "last_error": None,
        }

    def add(self, key: str, amount: int = 1) -> None:
        with self.lock:
            self.values[key] = int(self.values[key] or 0) + amount

    def set(self, key: str, value: str | None) -> None:
        with self.lock:
            self.values[key] = value

    def event(self, event: str, **fields: object) -> None:
        with self.lock:
            record = {
                "event": event,
                "uptime_s": round(time.monotonic() - self.started, 1),
                **self.values,
                **fields,
            }
            LOG.info(json.dumps(record, separators=(",", ":"), sort_keys=True))


def _configure_keepalive(client: socket.socket) -> None:
    client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", 15), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
        option = getattr(socket, name, None)
        if option is not None:
            client.setsockopt(socket.IPPROTO_TCP, option, value)
    user_timeout = getattr(socket, "TCP_USER_TIMEOUT", None)
    if user_timeout is not None:
        client.setsockopt(socket.IPPROTO_TCP, user_timeout, 30000)


def _open_serial(config: Config, stats: Stats) -> serial.Serial:
    port = serial.Serial(
        port=None,
        baudrate=config.baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=SERIAL_READ_TIMEOUT,
        write_timeout=SERIAL_WRITE_TIMEOUT,
        # Manage TIOCEXCL ourselves so we can clear it reliably on release.
        exclusive=None,
    )
    port.port = config.device
    try:
        port.open()
        # Advisory flock coordinates compliant tools; TIOCEXCL rejects all
        # unrelated tty opens. Linux keeps TIOCEXCL set beyond close, so
        # _close_serial explicitly clears it for clean restart/recovery.
        fcntl.ioctl(port.fileno(), termios.TIOCEXCL)
        fcntl.flock(port.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        port.reset_input_buffer()
        port.reset_output_buffer()
        return port
    except Exception:
        _close_serial(port)
        raise


def _close_serial(port: serial.Serial) -> None:
    try:
        if port.is_open:
            try:
                fcntl.ioctl(port.fileno(), termios.TIOCNXCL)
            except Exception:
                # Device removal may already have invalidated this tty fd.
                pass
    finally:
        port.close()


def _serial_hung_up(fd: int) -> bool:
    poller = select.poll()
    poller.register(fd, select.POLLERR | select.POLLHUP | select.POLLNVAL)
    return any(mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL) for _, mask in poller.poll(0))


def _reject_waiting_clients(listener: socket.socket, stats: Stats) -> None:
    """Drop connections queued during the previous session; do not replay them."""
    listener.setblocking(False)
    try:
        while True:
            try:
                waiting, address = listener.accept()
            except BlockingIOError:
                break
            stats.add("rejected_clients")
            stats.event("client_rejected", peer=address[0], reason="another_session_owned_uart")
            waiting.close()
    finally:
        listener.settimeout(1.0)


def bridge_client(
    client: socket.socket,
    port: BinaryIO,
    stats: Stats,
    stop: threading.Event,
) -> str:
    """Forward bytes without an application queue; return the disconnect cause."""
    client.settimeout(SOCKET_WRITE_TIMEOUT)
    client.setblocking(False)
    serial_fd = port.fileno()
    while not stop.is_set():
        if _serial_hung_up(serial_fd):
            return "serial_hangup"
        readable, _, exceptional = select.select([client, serial_fd], [], [client], 0.5)
        if exceptional:
            return "tcp_error"
        if client in readable:
            data = client.recv(MAX_CHUNK)
            if not data:
                return "client_eof"
            # A failed/partial write closes this session. Never queue or replay
            # bytes on a newly opened serial device after a cable replug.
            port.write(data)
            stats.add("tcp_to_usb_bytes", len(data))
        if serial_fd in readable:
            data = port.read(MAX_CHUNK)
            if data:
                client.sendall(data)
                stats.add("usb_to_tcp_bytes", len(data))
    return "shutdown"


def serve(config: Config, stop: threading.Event) -> None:
    stats = Stats()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((config.bind, config.port))
    listener.listen(1)
    listener.settimeout(1.0)
    stats.event("listening", bind=config.bind, port=config.port, device=config.device)
    next_status = time.monotonic() + config.status_seconds
    try:
        while not stop.is_set():
            if time.monotonic() >= next_status:
                stats.event("status", serial_present=os.path.exists(config.device))
                next_status = time.monotonic() + config.status_seconds
            try:
                client, address = listener.accept()
            except TimeoutError:
                continue
            peer = address[0]
            if not config.allows(peer):
                stats.add("rejected_clients")
                stats.event("client_rejected", peer=peer)
                client.close()
                continue
            stats.add("client_connections")
            stats.set("active_client", peer)
            try:
                _configure_keepalive(client)
                port = _open_serial(config, stats)
            except (OSError, serial.SerialException, serial.SerialTimeoutException) as exc:
                stats.add("serial_open_failures")
                stats.set("last_error", type(exc).__name__)
                stats.event("serial_unavailable", peer=peer, error=type(exc).__name__)
                client.close()
                stats.set("active_client", None)
                _reject_waiting_clients(listener, stats)
                time.sleep(SERIAL_RETRY_SECONDS)
                continue
            stats.event("client_connected", peer=peer, device=config.device)
            try:
                reason = bridge_client(client, port, stats, stop)
                if reason == "serial_hangup":
                    stats.add("serial_disconnects")
                else:
                    stats.add("client_disconnects")
                stats.event("client_closed", peer=peer, reason=reason)
            except (OSError, serial.SerialException, serial.SerialTimeoutException) as exc:
                stats.add("serial_disconnects" if isinstance(exc, serial.SerialException) else "client_disconnects")
                stats.set("last_error", type(exc).__name__)
                stats.event("bridge_error", peer=peer, error=type(exc).__name__)
            finally:
                # Do not let late device replies leak into the next client.
                try:
                    port.reset_output_buffer()
                    port.reset_input_buffer()
                except Exception:
                    pass
                _close_serial(port)
                client.close()
                stats.set("active_client", None)
                _reject_waiting_clients(listener, stats)
    finally:
        listener.close()
        stats.event("stopped")


def main() -> int:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    try:
        config = Config.from_env()
    except (ValueError, OSError) as exc:
        LOG.error(json.dumps({"event": "configuration_error", "error": str(exc)}))
        return 2
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        serve(config, stop)
    except OSError as exc:
        LOG.error(json.dumps({"event": "listener_error", "error": type(exc).__name__}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
