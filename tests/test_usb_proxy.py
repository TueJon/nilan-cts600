import importlib.util
import os
import pathlib
import pty
import socket
import sys
import threading
import time
import errno
import json
import logging
import select
import pytest

import serial


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "ops/usb-proxy/nilan_usb_proxy.py"
SPEC = importlib.util.spec_from_file_location("nilan_usb_proxy", MODULE_PATH)
proxy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = proxy
assert SPEC.loader is not None
SPEC.loader.exec_module(proxy)


def test_configuration_requires_an_explicit_network_allowlist(monkeypatch):
    monkeypatch.setenv("NILAN_USB_DEVICE", "/dev/ttyUSB-test")
    monkeypatch.setenv("NILAN_USB_BIND", "127.0.0.1")
    monkeypatch.delenv("NILAN_USB_ALLOW_CIDRS", raising=False)
    try:
        proxy.Config.from_env()
    except ValueError as exc:
        assert "ALLOW_CIDRS" in str(exc)
    else:
        raise AssertionError("proxy must fail closed without an explicit allowlist")


def test_allowlist_checks_individual_ipv4_networks():
    cfg = proxy.Config(
        bind="127.0.0.1",
        port=6638,
        device="/dev/ttyUSB-test",
        baud=115200,
        allow_networks=(proxy.ipaddress.ip_network("192.0.2.50/32"),),
        status_seconds=60,
    )
    assert cfg.allows("192.0.2.50")
    assert not cfg.allows("192.0.2.51")


def _config(device: str, port: int, allow: str = "127.0.0.0/8"):
    return proxy.Config(
        bind="127.0.0.1",
        port=port,
        device=device,
        baud=115200,
        allow_networks=(proxy.ipaddress.ip_network(allow),),
        status_seconds=60,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _connect_when_ready(port: int, timeout: float = 3.0) -> socket.socket:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            sock.settimeout(2)
            return sock
        except OSError:
            time.sleep(0.02)
    raise AssertionError("USB proxy listener did not come up")


def _wait_for_event(caplog, event: str, occurrence: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        count = 0
        for record in caplog.records:
            if not record.message.startswith("{"):
                continue
            try:
                count += json.loads(record.message).get("event") == event
            except json.JSONDecodeError:
                continue
        if count >= occurrence:
            return
        time.sleep(0.01)
    raise AssertionError(f"proxy did not emit {event} event #{occurrence}")


def test_tty_exclusivity_blocks_other_openers_and_releases_on_close():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    os.close(slave_fd)
    port = proxy._open_serial(_config(slave_name, _free_port()), proxy.Stats())
    try:
        with pytest.raises(OSError) as exc:
            os.open(slave_name, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        assert exc.value.errno in {errno.EBUSY, errno.EIO}
    finally:
        proxy._close_serial(port)
    # The proxy explicitly clears TIOCEXCL so a process restart can reopen.
    reopened = os.open(slave_name, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    os.close(reopened)
    os.close(master_fd)


def test_server_rejects_disallowed_peer_before_opening_serial(caplog):
    caplog.set_level(logging.INFO, logger="nilan-usb-proxy")
    port = _free_port()
    stop = threading.Event()
    worker = threading.Thread(
        target=proxy.serve,
        args=(_config("/path/that/must/not/be/opened", port, "192.0.2.0/24"), stop),
        daemon=True,
    )
    worker.start()
    client = _connect_when_ready(port)
    try:
        assert client.recv(1) == b""
    finally:
        client.close()
        stop.set()
        worker.join(timeout=2)
    records = [json.loads(record.message) for record in caplog.records if record.message.startswith("{")]
    assert any(r.get("event") == "client_rejected" for r in records)
    assert not any(r.get("event") == "serial_unavailable" for r in records)


def test_usb_unplug_closes_session_and_replug_accepts_only_new_bytes(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="nilan-usb-proxy")
    monkeypatch.setattr(proxy, "SERIAL_RETRY_SECONDS", 0.02)
    first_master, first_slave = pty.openpty()
    device = tmp_path / "serial-device"
    device.symlink_to(os.ttyname(first_slave))
    os.close(first_slave)
    port_number = _free_port()
    stop = threading.Event()
    worker = threading.Thread(target=proxy.serve, args=(_config(str(device), port_number), stop), daemon=True)
    worker.start()
    first_client = _connect_when_ready(port_number)
    try:
        first_command = b"first-session"
        first_client.sendall(first_command)
        assert _read_exact(first_master, len(first_command)) == first_command
        first_reply = b"first-response"
        os.write(first_master, first_reply)
        assert first_client.recv(len(first_reply)) == first_reply

        os.close(first_master)  # PTY HUP models USB disconnect.
        assert first_client.recv(1) == b""
        first_client.close()

        second_master, second_slave = pty.openpty()
        replacement = tmp_path / "replacement"
        replacement.symlink_to(os.ttyname(second_slave))
        os.close(second_slave)
        replacement.replace(device)
        time.sleep(0.05)  # Let the listener finish closing the disconnected session.
        second_client = _connect_when_ready(port_number)
        try:
            _wait_for_event(caplog, "client_connected", 2)
            # No bytes queued for the prior serial client may replay on replug.
            assert not select.select([second_master], [], [], 0.2)[0], (open_calls, open_errors, keepalive_errors)
            fresh_command = b"new-session-only"
            second_client.sendall(fresh_command)
            assert _read_exact(second_master, len(fresh_command)) == fresh_command
            fresh_reply = b"fresh-response"
            os.write(second_master, fresh_reply)
            assert second_client.recv(len(fresh_reply)) == fresh_reply
        finally:
            second_client.close()
            os.close(second_master)
    finally:
        stop.set()
        worker.join(timeout=2)
        if first_client.fileno() >= 0:
            first_client.close()
    records = [json.loads(record.message) for record in caplog.records if record.message.startswith("{")]
    assert any(
        (r.get("event") == "client_closed" and r.get("reason") == "serial_hangup")
        or r.get("event") == "bridge_error"
        for r in records
    )


def _read_exact(fd: int, count: int, timeout: float = 3.0) -> bytes:
    os.set_blocking(fd, False)
    result = bytearray()
    deadline = time.monotonic() + timeout
    while len(result) < count and time.monotonic() < deadline:
        try:
            result.extend(os.read(fd, count - len(result)))
        except BlockingIOError:
            time.sleep(0.01)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            time.sleep(0.01)
    return bytes(result)


def test_raw_bytes_forward_both_directions_in_bounded_chunks():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    os.close(slave_fd)
    port = serial.Serial(
        slave_name,
        115200,
        timeout=proxy.SERIAL_READ_TIMEOUT,
        write_timeout=proxy.SERIAL_WRITE_TIMEOUT,
        exclusive=None,
    )
    tcp_server, tcp_client = socket.socketpair()
    stop = threading.Event()
    stats = proxy.Stats()
    worker = threading.Thread(target=proxy.bridge_client, args=(tcp_server, port, stats, stop))
    worker.start()
    try:
        outbound = bytes(range(256)) * 5
        tcp_client.sendall(outbound)
        assert _read_exact(master_fd, len(outbound)) == outbound

        inbound = bytes(reversed(range(256))) * 5
        os.write(master_fd, inbound)
        tcp_client.settimeout(3)
        received = bytearray()
        while len(received) < len(inbound):
            received.extend(tcp_client.recv(1024))
        assert bytes(received) == inbound
        assert stats.values["tcp_to_usb_bytes"] == len(outbound)
        assert stats.values["usb_to_tcp_bytes"] == len(inbound)
    finally:
        tcp_client.close()
        worker.join(timeout=3)
        stop.set()
        tcp_server.close()
        proxy._close_serial(port)
        os.close(master_fd)
    assert not worker.is_alive()
