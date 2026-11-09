"""pymodbus 3.16 wraps serial I/O errors in ConnectionException."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pymodbus.exceptions import ConnectionException

import nilan_api


def test_retry_reconnects_after_connection_exception(monkeypatch):
    monkeypatch.setattr(nilan_api, "MOCKUP", False)
    monkeypatch.setattr(nilan_api, "READ_ONLY", True)
    monkeypatch.setattr(nilan_api, "RETRIES", 2)
    monkeypatch.setattr(nilan_api.time, "sleep", lambda _: None)
    device = nilan_api.NilanDevice()
    calls = []
    reconnects = []

    class FakeCTS:
        client = SimpleNamespace(close=lambda: None)

        def __init__(self, port=None):
            pass

        def connect(self):
            reconnects.append(True)

        def initialize(self):
            pass

        def write(self):
            calls.append("write")
            if len(calls) == 1:
                raise ConnectionException("serial disconnected")
            return "ok"

    monkeypatch.setattr(nilan_api, "CTS600", FakeCTS)
    device._cts = FakeCTS()
    device._connected = True

    assert device._retry("write") == "ok"
    assert calls == ["write", "write"]
    assert reconnects == [True]
    assert device._connected is True


def test_failed_reconnect_clears_connected_for_watchdog(monkeypatch):
    monkeypatch.setattr(nilan_api, "RETRIES", 1)
    monkeypatch.setattr(nilan_api.time, "sleep", lambda _: None)
    device = nilan_api.NilanDevice()
    device._connected = True

    class BrokenCTS:
        def write(self):
            raise ConnectionException("serial disconnected")

    device._cts = BrokenCTS()

    def fail_connect():
        raise OSError("PTY unavailable")

    monkeypatch.setattr(device, "connect", fail_connect)
    with pytest.raises(ConnectionException):
        device._retry("write")
    assert device._connected is False

    monkeypatch.setattr(nilan_api, "WATCHDOG_STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr(nilan_api, "WATCHDOG_CHECK_SECONDS", 0)
    monkeypatch.setattr(nilan_api.time, "time", lambda: 1000.0)

    class StopAfterOneCheck:
        calls = 0

        def wait(self, _seconds):
            self.calls += 1
            return self.calls > 1

    watchdog = nilan_api.LivenessWatchdog(device)
    watchdog._started_ts = 0
    watchdog._stop = StopAfterOneCheck()
    watchdog._loop()
    assert watchdog._disconnected_since == 1000.0


def test_poll_reconnects_when_pymodbus_raises_connection_exception(monkeypatch):
    monkeypatch.setattr(nilan_api, "MOCKUP", False)
    monkeypatch.setattr(nilan_api, "POLL_SECONDS", 0)
    monkeypatch.setattr(nilan_api.os.path, "exists", lambda _path: True)
    device = nilan_api.NilanDevice()
    connect_calls = []

    def connect():
        connect_calls.append(True)
        device._connected = True

    def refresh():
        raise ConnectionException("serial disconnected")

    class StopAfterPoll:
        calls = 0

        def is_set(self):
            return self.calls > 0

        def wait(self, _seconds):
            self.calls += 1
            return self.calls > 0

    device.connect = connect
    device.refresh = refresh
    device.room_watchdog_tick = lambda: False
    device._stop = StopAfterPoll()

    device._poll_loop()

    assert len(connect_calls) == 2  # initial connection plus recovery
    assert device._connected is True
    assert "serial disconnected" in device._last_error


def test_poll_failed_reconnect_leaves_watchdog_armed(monkeypatch):
    monkeypatch.setattr(nilan_api, "MOCKUP", False)
    monkeypatch.setattr(nilan_api, "POLL_SECONDS", 0)
    monkeypatch.setattr(nilan_api.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(nilan_api, "WATCHDOG_STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr(nilan_api, "WATCHDOG_CHECK_SECONDS", 0)
    monkeypatch.setattr(nilan_api.time, "time", lambda: 1000.0)
    device = nilan_api.NilanDevice()
    connect_calls = []

    def connect():
        connect_calls.append(True)
        if len(connect_calls) == 1:
            device._connected = True
            return
        raise OSError("PTY unavailable")

    class StopAfterPoll:
        calls = 0

        def is_set(self):
            return self.calls > 0

        def wait(self, _seconds):
            self.calls += 1
            return self.calls > 1

    device.connect = connect
    device.refresh = lambda: (_ for _ in ()).throw(ConnectionException("serial disconnected"))
    device.room_watchdog_tick = lambda: False
    device._stop = StopAfterPoll()
    device._poll_loop()

    assert len(connect_calls) == 2
    assert device._connected is False
    assert device._last_error == "PTY unavailable"

    watchdog = nilan_api.LivenessWatchdog(device)
    watchdog._started_ts = 0
    watchdog._stop = StopAfterPoll()
    watchdog._loop()
    assert watchdog._disconnected_since == 1000.0


def test_exhausted_connection_exception_write_keeps_502_mapping():
    def fail_write(_value):
        raise ConnectionException("serial disconnected")

    with pytest.raises(HTTPException) as error:
        nilan_api._device_write("set_temp", "/api/temp", {"setpoint": 20}, fail_write, 20)

    assert error.value.status_code == 502
