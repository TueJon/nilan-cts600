import time

import pytest

import nilan_api


FIELDS = {"T15": 22, "T1": 16, "T5": 17, "status": "AUTO"}


def test_real_readiness_requires_fresh_complete_snapshot(monkeypatch):
    monkeypatch.setattr(nilan_api, "MOCKUP", False)
    monkeypatch.setattr(nilan_api, "HEALTH_STALE_SECONDS", 90)
    device = nilan_api.NilanDevice()
    assert device.readiness()["ready"] is False

    device._snapshot = dict(FIELDS)
    device._connected = True
    device._last_update_monotonic = time.monotonic() - 91
    assert device.readiness()["ready"] is False

    device._last_update_monotonic = time.monotonic()
    assert device.readiness()["ready"] is True

    device._snapshot.pop("T5")
    assert device.readiness()["ready"] is False
    assert device.readiness()["missing_fields"] == ["T5"]


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "22", True])
def test_real_readiness_rejects_nonfinite_or_nonnumeric_sensor_values(monkeypatch, value):
    monkeypatch.setattr(nilan_api, "MOCKUP", False)
    device = nilan_api.NilanDevice()
    device._snapshot = {**FIELDS, "T15": value}
    device._connected = True
    device._last_update_monotonic = time.monotonic()

    state = device.readiness()

    assert state["ready"] is False
    assert "T15" in state["missing_fields"]


@pytest.mark.parametrize("status", [None, "", "   ", 0])
def test_real_readiness_rejects_empty_or_nonnumeric_status(monkeypatch, status):
    monkeypatch.setattr(nilan_api, "MOCKUP", False)
    device = nilan_api.NilanDevice()
    device._snapshot = {**FIELDS, "status": status}
    device._connected = True
    device._last_update_monotonic = time.monotonic()

    state = device.readiness()

    assert state["ready"] is False
    assert "status" in state["missing_fields"]


def test_successful_poll_replaces_old_fields_with_current_unit_data(monkeypatch):
    monkeypatch.setattr(nilan_api, "MOCKUP", False)
    device = nilan_api.NilanDevice()
    device._snapshot = {**FIELDS, "old_stale_sensor": 99}
    device._connected = True

    class FakeCTS:
        data = {**FIELDS, "old_stale_sensor": 99}
        metaData = {"old_stale_sensor": {}}

        def updateData(self, _show_data):
            assert self.data == {}
            assert self.metaData == {}
            self.data = dict(FIELDS)

    device._cts = FakeCTS()
    result = device.refresh()

    assert result == FIELDS
    assert "old_stale_sensor" not in device.status()["raw"]
    assert device.readiness()["ready"] is True


def test_failed_refresh_is_not_ready_even_if_old_sample_is_recent(monkeypatch):
    monkeypatch.setattr(nilan_api, "MOCKUP", False)
    device = nilan_api.NilanDevice()
    device._snapshot = dict(FIELDS)
    device._connected = True
    device._last_update_monotonic = time.monotonic()

    class BrokenCTS:
        data = dict(FIELDS)
        metaData = {}

        def updateData(self, _show_data):
            raise OSError("serial read timeout")

    device._cts = BrokenCTS()
    try:
        device.refresh()
    except OSError:
        device._connected = False  # the poll loop applies this on any poll error

    assert device.readiness()["ready"] is False


def test_mockup_readiness_is_explicit_and_does_not_claim_physical(monkeypatch):
    monkeypatch.setattr(nilan_api, "MOCKUP", True)
    state = nilan_api.NilanDevice().readiness()
    assert state["ready"] is True
    assert state["mockup"] is True
    assert state["physical"] is False


def test_readyz_returns_503_when_physical_data_is_stale(monkeypatch):
    monkeypatch.setattr(nilan_api.device, "readiness", lambda: {
        "ready": False, "connected": False, "physical": True,
        "mockup": False, "age_seconds": 125.0,
        "max_age_seconds": 90.0, "missing_fields": [],
        "last_error": "serial timeout",
    })
    response = nilan_api.readyz()
    assert response.status_code == 503
    assert response.body.startswith(b'{"ready":false')


def test_socat_entrypoint_has_bounded_connect_and_backoff():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "tunnel_supervisor.py").read_text()
    assert "connect-timeout=5" in source
    assert "stop.wait(delay)" in source
    assert "min(60, delay * 2)" in source
