"""Live room temperature feed, exercised against the FastAPI app in mockup mode."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import nilan_api
from nilan_cts600 import CTS600Mockup


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(monkeypatch):
    clock = Clock()
    sent = []
    monkeypatch.setattr(CTS600Mockup, "setT15", lambda self, c: sent.append(c))
    dev = nilan_api.device
    monkeypatch.setattr(dev, "_clock", clock)
    monkeypatch.setattr(dev, "_room_live", None)
    monkeypatch.setattr(nilan_api, "READ_ONLY", False)
    monkeypatch.setattr(nilan_api, "ROOM_TTL_SECONDS", 900.0)
    dev.connect()
    sent.clear()
    # no lifespan: no poll thread / watchdog / MQTT
    return SimpleNamespace(client=TestClient(nilan_api.app), clock=clock, sent=sent, dev=dev)


def room(env):
    return env.client.get("/api/status").json()


def test_accept_and_status_live(env):
    r = env.client.post("/api/room", json={"celsius": 23.4, "source": "sensor-a"})
    assert r.status_code == 200
    assert len(env.sent) == 1 and abs(env.sent[0] - 23.4) < 0.1  # unit resolution ~0.14 C
    st = room(env)
    rs = st["room_source"]
    assert rs["mode"] == "live" and rs["source"] == "sensor-a"
    assert rs["value"] == env.sent[0] and rs["fallback"] == 21.0
    assert rs["age_s"] == 0.0 and rs["ttl_s"] == 900.0
    assert st["t_room"] == env.sent[0]
    env.clock.t += 60
    assert room(env)["room_source"]["age_s"] == 60.0


def test_fallback_status_initially(env):
    rs = room(env)["room_source"]
    assert rs == {"mode": "fallback", "value": 21.0, "fallback": 21.0,
                  "age_s": None, "source": None, "ttl_s": 900.0}


@pytest.mark.parametrize("bad", [4.9, 35.1, -3, 1e9])
def test_out_of_range_rejected(env, bad):
    r = env.client.post("/api/room", json={"celsius": bad, "source": "x"})
    assert r.status_code == 422
    assert env.sent == [] and room(env)["room_source"]["mode"] == "fallback"


def test_read_only_blocked_and_logged(env, monkeypatch):
    monkeypatch.setattr(nilan_api, "READ_ONLY", True)
    r = env.client.post("/api/room", json={"celsius": 22, "source": "x"})
    assert r.status_code == 403
    assert env.sent == [] and room(env)["room_source"]["mode"] == "fallback"
    ev = env.client.get("/api/activity").json()["events"][0]
    assert ev["action_type"] == "set_room" and ev["result"] == "blocked"


def test_ttl_expiry_falls_back(env):
    env.client.post("/api/room", json={"celsius": 24, "source": "x"})
    live = env.sent[-1]
    env.clock.t += 900  # exactly TTL: still live
    assert env.dev.room_watchdog_tick() is False
    assert room(env)["room_source"]["mode"] == "live"
    env.clock.t += 1
    assert env.dev.room_watchdog_tick() is True
    assert env.sent[-1] == 21.0 and env.sent[-2] == live
    assert room(env)["room_source"]["mode"] == "fallback"
    assert room(env)["t_room"] == 21.0
    assert env.dev.room_watchdog_tick() is False  # logged / applied once
    assert env.sent.count(21.0) == 1


def test_reconnect_reapplies_live_within_ttl(env):
    env.client.post("/api/room", json={"celsius": 24, "source": "x"})
    live = env.sent[-1]
    env.sent.clear()
    env.clock.t += 100
    env.dev.connect()
    assert env.sent == [live]


def test_reconnect_after_ttl_uses_fallback(env):
    env.client.post("/api/room", json={"celsius": 24, "source": "x"})
    env.sent.clear()
    env.clock.t += 901
    env.dev.connect()
    assert env.sent == [21.0]


def test_ttl_zero_disables_live_values(env, monkeypatch):
    monkeypatch.setattr(nilan_api, "ROOM_TTL_SECONDS", 0.0)
    r = env.client.post("/api/room", json={"celsius": 22, "source": "x"})
    assert r.status_code == 409
    assert env.sent == [] and room(env)["room_source"]["mode"] == "fallback"


def mqtt(env, payload, topic="nilan/room/set"):
    msg = SimpleNamespace(topic=topic, payload=payload.encode())
    nilan_api.mqtt_layer._on_message(None, None, msg)


def test_mqtt_room_set(env):
    mqtt(env, "22.5")
    rs = room(env)["room_source"]
    assert rs["mode"] == "live" and rs["source"] == "mqtt" and len(env.sent) == 1


@pytest.mark.parametrize("bad", ["abc", "50", "2", "nan"])
def test_mqtt_invalid_ignored_and_logged(env, bad):
    mqtt(env, bad)
    assert env.sent == [] and room(env)["room_source"]["mode"] == "fallback"
    ev = env.client.get("/api/activity").json()["events"][0]
    assert ev["target"] == "room/set" and ev["result"] == "blocked"


def test_mqtt_read_only_blocked(env, monkeypatch):
    monkeypatch.setattr(nilan_api, "READ_ONLY", True)
    mqtt(env, "22")
    assert env.sent == []
    assert env.client.get("/api/activity").json()["events"][0]["result"] == "blocked"
