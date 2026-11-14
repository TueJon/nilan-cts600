import os
import select
import shutil
import socket
import subprocess
import threading
import time

import pytest

from tunnel_supervisor import Endpoint, endpoints_from_env, run_cycle, socat_command, supervise


def test_endpoints_from_env_keeps_primary_before_optional_usb_fallback():
    assert endpoints_from_env({"ESP_IP": "192.0.2.30"}) == [Endpoint("192.0.2.30", 6638)]
    assert endpoints_from_env({
        "ESP_IP": "192.0.2.30", "ESP_PORT": "6638",
        "ESP_FALLBACK_IP": "192.0.2.6", "ESP_FALLBACK_PORT": "16638",
    }) == [Endpoint("192.0.2.30", 6638), Endpoint("192.0.2.6", 16638)]


def test_endpoint_config_fails_closed_without_primary_and_deduplicates_fallback():
    with pytest.raises(ValueError, match="ESP_IP"):
        endpoints_from_env({})
    assert endpoints_from_env({
        "ESP_IP": "192.0.2.30", "ESP_FALLBACK_IP": "192.0.2.30",
    }) == [Endpoint("192.0.2.30", 6638)]


@pytest.mark.skipif(shutil.which("socat") is None, reason="socat is required for PTY tunnel integration")
def test_socat_tunnel_options_open_and_carry_raw_bytes_both_directions(tmp_path):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    tty = tmp_path / "nilan-pty"
    proc = None
    tcp = None
    pty_fd = None
    try:
        command = socat_command(Endpoint("127.0.0.1", listener.getsockname()[1]), str(tty))
        proc = subprocess.Popen(command, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        tcp, _ = listener.accept()
        deadline = time.monotonic() + 3
        while not tty.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"socat exited before creating PTY: {proc.stderr.read()!r}")
            time.sleep(0.01)
        assert tty.exists(), "socat did not create the PTY link"
        pty_fd = os.open(tty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

        inbound = b"\x00nilan-from-tcp\r\n"
        tcp.sendall(inbound)
        readable, _, _ = select.select([pty_fd], [], [], 3)
        assert readable, "TCP bytes did not reach the PTY"
        received = bytearray()
        while len(received) < len(inbound):
            readable, _, _ = select.select([pty_fd], [], [], 3)
            assert readable, "TCP bytes did not fully reach the PTY"
            received.extend(os.read(pty_fd, len(inbound) - len(received)))
        assert bytes(received) == inbound

        outbound = b"\x00nilan-from-pty\r\n"
        os.write(pty_fd, outbound)
        tcp.settimeout(3)
        received_tcp = bytearray()
        while len(received_tcp) < len(outbound):
            chunk = tcp.recv(len(outbound) - len(received_tcp))
            assert chunk, "PTY closed before all bytes reached TCP"
            received_tcp.extend(chunk)
        assert bytes(received_tcp) == outbound
    finally:
        if pty_fd is not None:
            os.close(pty_fd)
        if tcp is not None:
            tcp.close()
        listener.close()
        if proc is not None:
            if proc.poll() is None:
                os.killpg(proc.pid, 15)
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                proc.communicate(timeout=3)


def test_fallback_is_opened_only_after_primary_stream_closes():
    endpoints = [Endpoint("192.0.2.30", 6638), Endpoint("192.0.2.6", 6638)]
    stop = threading.Event()
    calls = []
    open_streams = 0

    def fake_attempt(endpoint, _tty, _stop, **_kwargs):
        nonlocal open_streams
        assert open_streams == 0
        open_streams += 1
        calls.append(endpoint)
        open_streams -= 1  # stream ended before next transport is attempted
        return 1.0

    run_cycle(endpoints, "/dev/test", stop, attempt=fake_attempt)

    assert calls == endpoints


def test_live_silent_protocol_request_ends_stream_and_advances_to_fallback():
    endpoints = [Endpoint("192.0.2.30", 6638), Endpoint("192.0.2.6", 6638)]
    stop = threading.Event()
    calls = []

    def fake_attempt(endpoint, _tty, _stop, *, recovery_requested):
        calls.append(endpoint)
        # Represents the API detecting sustained failed physical polls while
        # the TCP stream remains open.
        if endpoint == endpoints[0]:
            recovery_requested.set()
        return 181.0

    run_cycle(endpoints, "/dev/test", stop, attempt=fake_attempt)
    assert calls == endpoints


def test_retry_backoff_caps_and_resets_after_a_stable_session(monkeypatch):
    waits = []
    durations = iter([1.0, 1.0, 90.0])

    def one_cycle(_endpoints, _tty, _stop):
        return next(durations)

    class StopAfterThreeWaits:
        calls = 0

        def is_set(self):
            return self.calls >= 3

        def wait(self, seconds):
            self.calls += 1
            waits.append(seconds)
            return self.calls >= 3

    stop = StopAfterThreeWaits()
    monkeypatch.setattr("tunnel_supervisor.run_cycle", one_cycle)
    supervise([Endpoint("192.0.2.30", 6638)], "/dev/test", stop)

    assert waits == [5, 10, 5]
