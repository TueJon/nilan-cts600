import threading

import pytest

from tunnel_supervisor import Endpoint, endpoints_from_env, run_cycle, supervise


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
