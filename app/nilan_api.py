"""
Nilan CTS600 — thin REST + MQTT wrapper around frodef's CTS600 protocol library.

Server-side "brain" for TUE-22 / TUE-16. The ESP32 is only a transparent
RAW serial<->TCP tunnel; the CTS600 custom protocol is driven here by
frodef's `nilan_cts600.CTS600` over a serial device (`/dev/ttyNILAN`, created
by socat from the ESP TCP stream).

Implements the API/MQTT contract from TUE-16 plan section 8:

  GET  /api/status -> { t_room, t_supply, t_exhaust, fan_level, mode, ... }
  POST /api/fan    <- { "level": 0-4 }          (0 = off)
  POST /api/mode   <- { "mode": "auto|heat|cool|off" }
  POST /api/temp   <- { "setpoint": 5-30 }
  POST /api/room   <- { "celsius": 5-35, "source": "..." }  (live room temperature)

  MQTT subscribe: nilan/fan/set, nilan/mode/set, nilan/temp/set, nilan/room/set
  MQTT publish  : nilan/state  (retained JSON, same shape as /api/status)
                  nilan/availability ("online"/"offline", retained LWT)

Modes:
  NILAN_MOCKUP=1  -> use frodef CTS600Mockup (no hardware needed; for bring-up
                     and contract verification before the ESP bridge is live).
  NILAN_MOCKUP=0  -> real device on NILAN_PORT (default /dev/ttyNILAN).

This wrapper is deliberately defensive: a CTS600 protocol error on one request
must never take the whole service down. Reads are served from the last good
poll snapshot; writes are serialized behind a single device lock.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException, Header, Depends, Query
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, JSONResponse
from pydantic import BaseModel, Field
import uvicorn
from pymodbus.exceptions import ConnectionException

# frodef protocol library (vendored under ../vendor, added to sys.path by entrypoint)
from nilan_cts600 import (  # type: ignore
    CTS600, CTS600Mockup, NilanCTS600Exception, nilanADToCelsius, nilanCelsiusToAD,
)

logging.basicConfig(level=os.environ.get("NILAN_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("nilan-api")

# ---- config -----------------------------------------------------------------
MOCKUP = os.environ.get("NILAN_MOCKUP", "0").strip() not in ("0", "", "false", "False")
PORT_DEV = os.environ.get("NILAN_PORT", "/dev/ttyNILAN")
RETRIES = int(os.environ.get("NILAN_RETRIES", "3"))
POLL_SECONDS = float(os.environ.get("NILAN_POLL_SECONDS", "30"))
T15_FALLBACK = float(os.environ.get("NILAN_T15_FALLBACK", "21"))
# Live room temperature (POST /api/room, MQTT <base>/room/set). A live value is used
# for at most ROOM_TTL_SECONDS after it was received; then the daemon falls back to
# T15_FALLBACK. 0 disables live values entirely (always fallback).
ROOM_TTL_SECONDS = float(os.environ.get("NILAN_ROOM_TTL_SECONDS", "900"))
ROOM_MIN_C = 5.0
ROOM_MAX_C = 35.0
API_TOKEN = os.environ.get("NILAN_API_TOKEN", "").strip()  # optional bearer for direct access
READ_ONLY = os.environ.get("NILAN_READ_ONLY", "0").strip().lower() in ("1", "true", "yes", "on")
ACTIVITY_LOG_MAX = int(os.environ.get("NILAN_ACTIVITY_LOG_MAX", "300"))
ACTIVITY_READ_SUMMARY_SECONDS = float(os.environ.get("NILAN_ACTIVITY_READ_SUMMARY_SECONDS", "60"))
ACTIVITY_READ_SUMMARY_COUNT = int(os.environ.get("NILAN_ACTIVITY_READ_SUMMARY_COUNT", "10"))
HTTP_HOST = os.environ.get("NILAN_HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("NILAN_HTTP_PORT", "8642"))
ESP_IP = os.environ.get("ESP_IP", "").strip()       # live ESP bridge IP (real mode)
ESP_PORT = os.environ.get("ESP_PORT", "6638").strip()

# Worker watchdog (TUE-307). The API remains alive and reports `/readyz` as
# degraded while the bridge is unreachable. Container restart is reserved for
# a truly stalled poll/reconnect worker, not normal offline retry cycles.
WATCHDOG_ENABLED = os.environ.get("NILAN_WATCHDOG_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
WATCHDOG_THRESHOLD_SECONDS = float(os.environ.get("NILAN_WATCHDOG_THRESHOLD_SECONDS", "180"))
WATCHDOG_CHECK_SECONDS = float(os.environ.get("NILAN_WATCHDOG_CHECK_SECONDS", "15"))
WATCHDOG_STARTUP_GRACE_SECONDS = float(os.environ.get("NILAN_WATCHDOG_STARTUP_GRACE_SECONDS", "90"))
# A fresh physical sample must arrive before this age. Readiness remains
# explicitly false for mockup mode, which has its own declared test semantics.
HEALTH_STALE_SECONDS = float(os.environ.get(
    "NILAN_HEALTH_STALE_SECONDS", str(max(90.0, POLL_SECONDS * 3))))
TUNNEL_RECOVERY_FILE = os.environ.get("NILAN_TUNNEL_RECOVERY_FILE", "/run/nilan-tunnel-recover")
TUNNEL_RECOVERY_THRESHOLD_SECONDS = float(
    os.environ.get("NILAN_TUNNEL_RECOVERY_THRESHOLD_SECONDS", "180"))
TUNNEL_RECOVERY_COOLDOWN_SECONDS = float(
    os.environ.get("NILAN_TUNNEL_RECOVERY_COOLDOWN_SECONDS", "180"))
# Panel language to set at connect so frodef's English SHOW-DATA menu regexes
# match (temperatures). Empty string disables. Default ENGLISH.
SET_LANGUAGE = os.environ.get("NILAN_SET_LANGUAGE", "ENGLISH").strip()

APP_VERSION = "1.2"  # 1.2 adds activity log (TUE-78)
DASHBOARD_HTML = (Path(__file__).resolve().parent / "dashboard.html")
MANIFEST_FILE = (Path(__file__).resolve().parent / "manifest.webmanifest")
SERVICE_WORKER_FILE = (Path(__file__).resolve().parent / "sw.js")
ICON_DIR = (Path(__file__).resolve().parent / "icons")

MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()  # empty -> MQTT disabled
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "").strip()
MQTT_PASS = os.environ.get("MQTT_PASS", "").strip()
MQTT_BASE = os.environ.get("MQTT_BASE_TOPIC", "nilan").strip().rstrip("/")

# Sensor->contract mapping. PROVISIONAL: exact supply/exhaust sensor ids are
# confirmed against the real unit's nameplate (TUE-16 plan section 11). Override
# via env without code changes once the live register dump is available.
T_ROOM_KEY = os.environ.get("NILAN_T_ROOM_KEY", "T15")
T_SUPPLY_KEY = os.environ.get("NILAN_T_SUPPLY_KEY", "T1")
T_EXHAUST_KEY = os.environ.get("NILAN_T_EXHAUST_KEY", "T5")

MODE_TO_FRODEF = {"auto": "AUTO", "heat": "HEAT", "cool": "COOL"}


class ActivityLog:
    """Small in-memory activity log retained by the backend process.

    This intentionally avoids writing audit data to disk on the home automation
    host. Read/status polls are summarized so the log remains useful while the
    dashboard auto-refreshes.
    """

    def __init__(self, maxlen: int) -> None:
        self.maxlen = max(50, maxlen)
        self._events = deque(maxlen=self.maxlen)
        self._lock = threading.Lock()
        self._next_id = 1
        self._read_summary: dict[str, dict[str, Any]] = {}

    def _now(self) -> tuple[float, str]:
        ts = time.time()
        return ts, datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")

    def record(
        self,
        *,
        source: str,
        action_type: str,
        target: str,
        result: str,
        detail: Optional[str] = None,
        value: Optional[Any] = None,
        count: Optional[int] = None,
    ) -> dict[str, Any]:
        ts, iso = self._now()
        event: dict[str, Any] = {
            "ts": ts,
            "timestamp": iso,
            "actor": source,
            "source": source,
            "action_type": action_type,
            "target": target,
            "result": result,
            "safety_state": "read_only" if READ_ONLY else "write_enabled",
        }
        if detail:
            event["detail"] = str(detail)[:240]
        if value is not None:
            event["value"] = value
        if count is not None:
            event["count"] = count
        with self._lock:
            event["id"] = self._next_id
            self._next_id += 1
            self._events.appendleft(event)
        return event

    def record_status_poll(self, source: str, result: str, detail: Optional[str] = None) -> None:
        now = time.time()
        key = f"{source}:{result}"
        with self._lock:
            summary = self._read_summary.setdefault(
                key,
                {"source": source, "result": result, "count": 0, "first_ts": now, "last_ts": now, "detail": None},
            )
            summary["count"] += 1
            summary["last_ts"] = now
            summary["detail"] = detail
            should_flush = (
                summary["count"] >= ACTIVITY_READ_SUMMARY_COUNT
                or now - summary["first_ts"] >= ACTIVITY_READ_SUMMARY_SECONDS
            )
        if should_flush:
            self.flush_status_summaries()

    def flush_status_summaries(self) -> None:
        with self._lock:
            summaries = list(self._read_summary.values())
            self._read_summary.clear()
        for summary in summaries:
            self.record(
                source=summary["source"],
                action_type="status_poll",
                target="/api/status",
                result=summary["result"],
                detail=summary.get("detail"),
                count=summary["count"],
            )

    def list_events(self, *, limit: int = 100, include_reads: bool = False) -> dict[str, Any]:
        self.flush_status_summaries()
        limit = max(1, min(limit, self.maxlen))
        with self._lock:
            events = list(self._events)
        if not include_reads:
            events = [e for e in events if e.get("action_type") != "status_poll"]
        return {
            "events": events[:limit],
            "limit": limit,
            "retention_max_events": self.maxlen,
            "include_reads": include_reads,
            "read_summary": {
                "window_seconds": ACTIVITY_READ_SUMMARY_SECONDS,
                "flush_count": ACTIVITY_READ_SUMMARY_COUNT,
            },
        }


activity_log = ActivityLog(ACTIVITY_LOG_MAX)


def _safe_activity_value(value: Any) -> Any:
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            if any(token in str(key).lower() for token in ("pass", "password", "token", "secret", "key")):
                safe[key] = "[redacted]"
            else:
                safe[key] = _safe_activity_value(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_safe_activity_value(item) for item in value[:10]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value) if isinstance(value, str) else value
        return text[:120] if isinstance(text, str) else text
    return str(value)[:120]


def _canon_mode(raw) -> Optional[str]:
    """Normalise the frodef mode token to canonical auto|heat|cool|off.

    The token is language-dependent (DE unit reports KÜHLEN/HEIZEN/AUTO, and the
    CTS600 charset mangles umlauts e.g. 'KÚHLEN'), so match on ascii substrings."""
    if not raw:
        return None
    t = str(raw).upper()
    if "AUTO" in t:
        return "auto"
    if "HEIZ" in t or "HEAT" in t:
        return "heat"
    if "K" in t and "HL" in t or "COOL" in t:  # KÜHLEN/KÚHLEN/COOL
        return "cool"
    if "AUS" in t or "OFF" in t:
        return "off"
    return str(raw).lower()


# ---- device manager ---------------------------------------------------------
class NilanDevice:
    """Owns the single CTS600 connection, a poll thread, and a device lock.

    All serial access is serialized through `self._lock` so REST writes, MQTT
    writes, and the background poller never interleave on the bus.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] = {}
        self._connected = False
        self._last_error: Optional[str] = None
        self._last_update_ts: float = 0.0
        self._last_update_monotonic: float = 0.0
        self._connect_failures = 0
        self._worker_last_progress_monotonic = time.monotonic()
        self._cts: Optional[CTS600] = None
        self._stop = threading.Event()
        self._on_state_change = None  # callback(dict) -> None, set by MQTT layer
        # Room temperature fed to the unit as T15. `_room_live` is None while the
        # fallback is in use, else (celsius, source, received_monotonic).
        self._clock = time.monotonic
        self._room_live: Optional[tuple[float, str, float]] = None

    def set_state_callback(self, cb) -> None:
        self._on_state_change = cb

    # -- lifecycle --
    def connect(self) -> None:
        with self._lock:
            # Ensure we don't keep a stale serial client/file-descriptor open
            # when reconnecting through a wedged /dev/ttyNILAN path.
            if self._cts is not None:
                try:
                    client = getattr(self._cts, "client", None)
                    if client is not None:
                        client.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("Failed closing previous CTS600 client before reconnect: %s", e)
            self._connected = False
            cls = CTS600Mockup if MOCKUP else CTS600
            log.info("Connecting CTS600 (%s, port=%s)", cls.__name__, "mockup" if MOCKUP else PORT_DEV)
            self._cts = cls(port=None if MOCKUP else PORT_DEV)
            self._cts.connect()
            self._cts.initialize()
            # Force the panel language to English: frodef's SHOW-DATA menu scan
            # matches English labels (STATUS, Tnn …°C). On a German unit those
            # regexes never match, so temperatures come back null. The physical
            # CTS600 panel is removed (ESP is the only master), so the display
            # language only affects what our scraper reads — safe to set.
            if not MOCKUP and SET_LANGUAGE:
                try:
                    ok = self._cts.setLanguage(SET_LANGUAGE)
                    log.info("setLanguage(%s) -> %s", SET_LANGUAGE, ok)
                except Exception as e:  # noqa: BLE001
                    log.warning("setLanguage(%s) failed (non-fatal): %s", SET_LANGUAGE, e)
            if READ_ONLY:
                log.info("Skipping setT15 injection because NILAN_READ_ONLY is enabled")
            else:
                # a fresh connection starts from the fallback; re-apply a live
                # room value that is still within its TTL.
                live = self._live_room_if_fresh()
                try:
                    self._cts.setT15(live[0] if live else T15_FALLBACK)
                    if live:
                        log.info("Re-applied live room temperature %.2f C after reconnect", live[0])
                except Exception as e:  # noqa: BLE001
                    log.warning("setT15 injection failed (non-fatal): %s", e)
            self._connected = True
            self._last_error = None
            self._connect_failures = 0

    def start(self) -> None:
        t = threading.Thread(target=self._poll_loop, name="nilan-poll", daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop.set()

    def _worker_progress(self) -> None:
        """Heartbeat for actual poll/reconnect worker progress, not device health."""
        self._worker_last_progress_monotonic = time.monotonic()

    def _wait_with_progress(self, seconds: float) -> None:
        """Wait without making normal offline retries look like a dead worker."""
        seconds = max(0.0, seconds)
        if seconds == 0:
            self._worker_progress()
            self._stop.wait(0)
            return
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            self._worker_progress()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            interval = WATCHDOG_CHECK_SECONDS if WATCHDOG_CHECK_SECONDS > 0 else remaining
            if self._stop.wait(min(remaining, interval)):
                return

    def _poll_loop(self) -> None:
        # initial connect with retry
        while not self._stop.is_set():
            self._worker_progress()
            try:
                self.connect()
                self._worker_progress()
                break
            except Exception as e:  # noqa: BLE001
                self._connected = False
                self._last_error = str(e)
                delay = min(60.0, 10.0 * (2 ** min(self._connect_failures, 3)))
                self._connect_failures += 1
                log.warning("Connect failed, retrying in %.0fs: %s", delay, e)
                self._worker_progress()
                self._wait_with_progress(delay)
        # poll
        while not self._stop.is_set():
            self._worker_progress()
            try:
                self.room_watchdog_tick()
                self.refresh()
                self._worker_progress()
                activity_log.record_status_poll("device-poller", "ok")
            except (OSError, ConnectionException) as e:
                self._worker_progress()
                self._connected = False
                self._last_error = str(e)
                activity_log.record_status_poll("device-poller", "error", str(e))
                log.warning("Poll I/O error (%s) -> waiting for PTY, then reconnecting", e)
                if not MOCKUP:
                    for _ in range(20):
                        if os.path.exists(PORT_DEV):
                            break
                        self._wait_with_progress(0.5)
                try:
                    self._worker_progress()
                    self.connect()
                    self._worker_progress()
                except Exception as ce:  # noqa: BLE001
                    self._worker_progress()
                    self._last_error = str(ce)
                    log.warning("Reconnect after poll I/O error failed: %s", ce)
            except Exception as e:  # noqa: BLE001
                self._worker_progress()
                self._connected = False
                self._last_error = str(e)
                activity_log.record_status_poll("device-poller", "error", str(e))
                log.warning("Poll failed: %s", e)
            self._wait_with_progress(POLL_SECONDS)

    # -- core device ops (all under lock) --
    def refresh(self) -> dict[str, Any]:
        with self._lock:
            if not self._cts:
                raise RuntimeError("device not connected")
            # frodef's updateData(updateShowData) refreshes self.data from the unit.
            # scanData seeds its result with self.data.copy(); clear the previous
            # snapshot first so omitted/unreadable menu values cannot masquerade
            # as fresh sensor data indefinitely.
            if not MOCKUP:
                self._cts.data = {}
                self._cts.metaData = {}
            try:
                self._cts.updateData(True)
            except TypeError:
                # signature is updateData(updateDisplayData) in some revisions
                self._cts.updateData(updateDisplayData=True)
            # mockup populates self.data asynchronously; give it a moment
            if MOCKUP and not getattr(self._cts, "data", None):
                time.sleep(2.2)
            data = dict(getattr(self._cts, "data", {}) or {})
            missing = self._missing_required_fields(data)
            if not MOCKUP and missing:
                raise RuntimeError("physical poll missing required fields: " + ", ".join(missing))
            self._snapshot = data
            self._last_update_ts = time.time()
            self._last_update_monotonic = time.monotonic()
            self._connected = True
            self._connect_failures = 0
        if self._on_state_change:
            try:
                self._on_state_change(self.status())
            except Exception as e:  # noqa: BLE001
                log.warning("state callback failed: %s", e)
        return data

    def readiness(self) -> dict[str, Any]:
        """Describe whether consumers may trust the latest physical snapshot."""
        now = time.monotonic()
        age = (now - self._last_update_monotonic
               if self._last_update_monotonic > 0 else None)
        missing = self._missing_required_fields(self._snapshot)
        fresh = age is not None and age <= HEALTH_STALE_SECONDS
        ready = MOCKUP or (self._connected and fresh and not missing)
        return {
            "ready": ready,
            "mockup": MOCKUP,
            "connected": self._connected,
            "physical": not MOCKUP,
            "age_seconds": round(age, 1) if age is not None else None,
            "max_age_seconds": HEALTH_STALE_SECONDS,
            "missing_fields": missing,
            "last_error": self._last_error,
        }

    @staticmethod
    def _missing_required_fields(data: dict[str, Any]) -> list[str]:
        missing = []
        for key in (T_ROOM_KEY, T_SUPPLY_KEY, T_EXHAUST_KEY):
            value = data.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                missing.append(key)
        status = data.get("status")
        if not isinstance(status, str) or not status.strip():
            missing.append("status")
        return missing

    def _retry(self, method: str, *args):
        """Run a device op (by method name) with resilient retries.

        The method is resolved on ``self._cts`` *each attempt* so that a
        reconnect — which replaces ``self._cts`` — is picked up on the next try.

        Retries on:
          * ``NilanCTS600Exception`` / ``TimeoutError`` — transient protocol/bus
            timeouts (single garbled frame). Plain retry.
          * ``OSError`` or pymodbus ``ConnectionException`` — the socat PTY
            blipped/was re-created, so the serial handle is stale. Reconnect
            first, then retry. pymodbus 3.16 wraps serial OSErrors in
            ConnectionException.
        """
        last = None
        for attempt in range(1, RETRIES + 1):
            try:
                return getattr(self._cts, method)(*args)
            except (NilanCTS600Exception, TimeoutError) as e:
                last = e
                log.warning("device op %s attempt %d/%d failed: %s",
                            method, attempt, RETRIES, e)
                time.sleep(0.3)
            except (OSError, ConnectionException) as e:
                last = e
                log.warning("device op %s attempt %d/%d I/O error (%s) -> reconnecting",
                            method, attempt, RETRIES, e)
                self._connected = False
                try:
                    self.connect()
                except Exception as ce:  # noqa: BLE001
                    self._connected = False
                    self._last_error = str(ce)
                    log.warning("reconnect after I/O error failed: %s", ce)
                time.sleep(0.6)
        raise last if last else RuntimeError("device op failed")

    def set_fan(self, level: int) -> None:
        with self._lock:
            if level == 0:
                self._retry("key_off")
            else:
                # ensure unit is on, then set flow 1-4
                try:
                    self._retry("key_on")
                except Exception:  # noqa: BLE001
                    pass
                self._retry("setFlow", level)
        self.refresh()

    def set_mode(self, mode: str) -> None:
        with self._lock:
            if mode == "off":
                self._retry("key_off")
            else:
                try:
                    self._retry("key_on")
                except Exception:  # noqa: BLE001
                    pass
                self._retry("setMode", MODE_TO_FRODEF[mode])
        self.refresh()

    def set_temp(self, setpoint: int) -> None:
        with self._lock:
            self._retry("setThermostat", setpoint)
        self.refresh()

    # -- live room temperature --
    @staticmethod
    def _snap_room(celsius: float) -> float:
        """Round to what the unit can represent (AD-converter resolution)."""
        return round(nilanADToCelsius(nilanCelsiusToAD(celsius)), 2)

    def _live_room_if_fresh(self) -> Optional[tuple[float, str, float]]:
        live = self._room_live
        if live is None or ROOM_TTL_SECONDS <= 0:
            return None
        return live if self._clock() - live[2] <= ROOM_TTL_SECONDS else None

    def _mock_t15(self, celsius: float) -> None:
        # the mockup scrapes a static T15; reflect what was injected instead
        if MOCKUP:
            self._snapshot = {**self._snapshot, T_ROOM_KEY: celsius}

    def set_room(self, celsius: float, source: str) -> float:
        """Inject a live room temperature as T15. Returns the value the unit uses."""
        applied = self._snap_room(celsius)
        with self._lock:
            self._retry("setT15", applied)
            self._room_live = (applied, source, self._clock())
            self._mock_t15(applied)
        self._notify_state()
        return applied

    def room_watchdog_tick(self) -> bool:
        """Fall back to T15_FALLBACK when the live value is older than the TTL.

        Returns True if a fallback was applied. Logged once per expiry.
        """
        with self._lock:
            live = self._room_live
            if live is None:
                return False
            if ROOM_TTL_SECONDS > 0 and self._clock() - live[2] <= ROOM_TTL_SECONDS:
                return False
            age = self._clock() - live[2]
            if not READ_ONLY and self._cts is not None:
                self._retry("setT15", T15_FALLBACK)
            self._room_live = None
            self._mock_t15(T15_FALLBACK)
        log.warning("Live room temperature expired (age %.0fs > TTL %.0fs) -> fallback %.1f C",
                    age, ROOM_TTL_SECONDS, T15_FALLBACK)
        activity_log.record(
            source="watchdog",
            action_type="room_ttl",
            target="room",
            result="ok",
            detail=f"live value from {live[1]} expired after {age:.0f}s; fallback {T15_FALLBACK}",
            value=_safe_activity_value({"celsius": T15_FALLBACK}),
        )
        self._notify_state()
        return True

    def room_source(self) -> dict[str, Any]:
        live = self._room_live
        if live is None:
            return {"mode": "fallback", "value": T15_FALLBACK, "fallback": T15_FALLBACK,
                    "age_s": None, "source": None, "ttl_s": ROOM_TTL_SECONDS}
        return {"mode": "live", "value": live[0], "fallback": T15_FALLBACK,
                "age_s": round(self._clock() - live[2], 1), "source": live[1],
                "ttl_s": ROOM_TTL_SECONDS}

    def _notify_state(self) -> None:
        if self._on_state_change:
            try:
                self._on_state_change(self.status())
            except Exception as e:  # noqa: BLE001
                log.warning("state callback failed: %s", e)

    # -- contract projection (plan section 8) --
    def status(self) -> dict[str, Any]:
        d = self._snapshot
        freshness = self.readiness()
        status_txt = str(d.get("status", "")).upper()
        # 'off' is inferred when the unit reports an OFF/standby status
        if status_txt in ("OFF", "STANDBY", "STOP", "AUS"):
            mode = "off"
        else:
            mode = _canon_mode(d.get("mode"))

        def num(key):
            v = d.get(key)
            return float(v) if isinstance(v, (int, float)) else None

        return {
            "t_room": num(T_ROOM_KEY),
            "room_source": self.room_source(),
            "t_supply": num(T_SUPPLY_KEY),
            "t_exhaust": num(T_EXHAUST_KEY),
            "fan_level": d.get("flow"),
            "mode": mode,
            "setpoint": d.get("thermostat"),
            "status": d.get("status"),
            "display": d.get("display"),
            "sensors": {k: v for k, v in d.items() if isinstance(k, str) and k.startswith("T")},
            # full decoded snapshot so the dashboard can show *every* value the
            # frodef driver scraped (humidity/filter/hours/etc. appear here once
            # the live unit is on the bus — keys are model/menu dependent).
            "raw": {k: v for k, v in d.items()},
            "connected": self._connected,
            "mockup": MOCKUP,
            "read_only": READ_ONLY,
            "last_update_ts": self._last_update_ts,
            "sample_age_seconds": freshness["age_seconds"],
            "telemetry_ready": freshness["ready"],
            "last_error": self._last_error,
        }


device = NilanDevice()


# ---- MQTT layer -------------------------------------------------------------
class MqttLayer:
    def __init__(self, dev: NilanDevice) -> None:
        self.dev = dev
        self.client = None

    def start(self) -> None:
        if not MQTT_HOST:
            log.info("MQTT disabled (MQTT_HOST unset)")
            return
        import paho.mqtt.client as mqtt  # local import so REST works without paho

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="nilan-api")
        if MQTT_USER:
            c.username_pw_set(MQTT_USER, MQTT_PASS)
        c.will_set(f"{MQTT_BASE}/availability", "offline", qos=1, retain=True)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        self.client = c
        self.dev.set_state_callback(self.publish_state)
        c.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        c.loop_start()
        log.info("MQTT connecting to %s:%s base=%s", MQTT_HOST, MQTT_PORT, MQTT_BASE)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info("MQTT connected rc=%s", reason_code)
        client.publish(f"{MQTT_BASE}/availability", "online", qos=1, retain=True)
        for sub in ("fan/set", "mode/set", "temp/set", "room/set"):
            client.subscribe(f"{MQTT_BASE}/{sub}", qos=1)
        self.publish_state(self.dev.status())

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", "ignore").strip()
        topic = msg.topic
        log.info("MQTT msg %s = %s", topic, payload)
        target = topic.replace(f"{MQTT_BASE}/", "", 1) if topic.startswith(f"{MQTT_BASE}/") else topic
        if READ_ONLY:
            activity_log.record(
                source="mqtt",
                action_type="command",
                target=target,
                result="blocked",
                detail="ignored because NILAN_READ_ONLY is enabled",
                value=_safe_activity_value(payload),
            )
            log.warning("Ignoring MQTT command in read-only mode: %s", topic)
            return
        try:
            if topic.endswith("/fan/set"):
                value = _coerce_level(payload)
                self.dev.set_fan(value)
            elif topic.endswith("/mode/set"):
                value = _coerce_mode(payload)
                self.dev.set_mode(value)
            elif topic.endswith("/temp/set"):
                value = _coerce_setpoint(payload)
                self.dev.set_temp(value)
            elif topic.endswith("/room/set"):
                try:
                    value = _coerce_room(payload)
                    if ROOM_TTL_SECONDS <= 0:
                        raise ValueError("live room feed disabled (NILAN_ROOM_TTL_SECONDS=0)")
                except ValueError as e:
                    activity_log.record(
                        source="mqtt", action_type="command", target=target,
                        result="blocked", detail=str(e), value=_safe_activity_value(payload),
                    )
                    log.warning("Ignoring MQTT room value %r: %s", payload, e)
                    return
                self.dev.set_room(value, "mqtt")
            else:
                value = payload
            activity_log.record(
                source="mqtt",
                action_type="command",
                target=target,
                result="ok",
                value=_safe_activity_value(value),
            )
        except Exception as e:  # noqa: BLE001
            activity_log.record(
                source="mqtt",
                action_type="command",
                target=target,
                result="error",
                detail=str(e),
                value=_safe_activity_value(payload),
            )
            log.warning("MQTT command failed on %s: %s", topic, e)

    def publish_state(self, state: dict) -> None:
        if not self.client:
            return
        self.client.publish(f"{MQTT_BASE}/state", json.dumps(state), qos=1, retain=True)


mqtt_layer = MqttLayer(device)


class LivenessWatchdog:
    """Restart only if the poll/reconnect worker stops making progress.

    An unreachable device is a degraded but expected condition: the worker must
    keep retrying and readiness stays false. Restarting just because the bridge
    is offline caused a container restart loop without improving connectivity.
    A hung serial/vendor call is different: its heartbeat stops, so a container
    restart discards that stuck client and recreates the socat tunnel.
    """

    def __init__(self, dev: NilanDevice) -> None:
        self._dev = dev
        self._stop = threading.Event()
        self._started_ts = 0.0
        self._degraded_since: Optional[float] = None
        self._last_tunnel_recovery_request: Optional[float] = None

    def start(self) -> None:
        if not WATCHDOG_ENABLED or MOCKUP:
            log.info("Worker watchdog disabled (enabled=%s, mockup=%s)", WATCHDOG_ENABLED, MOCKUP)
            return
        self._started_ts = time.monotonic()
        t = threading.Thread(target=self._loop, name="nilan-watchdog", daemon=True)
        t.start()
        log.info("Worker watchdog active: restart only after no poll progress for >%.0fs "
                 "(check=%.0fs, startup_grace=%.0fs)",
                 WATCHDOG_THRESHOLD_SECONDS, WATCHDOG_CHECK_SECONDS, WATCHDOG_STARTUP_GRACE_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        ds = self._degraded_since
        worker_age = max(0.0, now - self._dev._worker_last_progress_monotonic)
        return {
            "enabled": bool(WATCHDOG_ENABLED and not MOCKUP),
            "threshold_seconds": WATCHDOG_THRESHOLD_SECONDS,
            "degraded_for_seconds": round(now - ds, 1) if ds is not None else 0.0,
            "worker_progress_age_seconds": round(worker_age, 1),
            "worker_stalled": worker_age >= WATCHDOG_THRESHOLD_SECONDS,
        }

    def _loop(self) -> None:
        while not self._stop.wait(WATCHDOG_CHECK_SECONDS):
            now = time.monotonic()
            if now - self._started_ts < WATCHDOG_STARTUP_GRACE_SECONDS:
                continue
            worker_stalled_for = max(0.0, now - self._dev._worker_last_progress_monotonic)
            if worker_stalled_for >= WATCHDOG_THRESHOLD_SECONDS:
                self._trigger_restart(worker_stalled_for)
                continue
            state = self._dev.readiness()
            if state["ready"]:
                if self._degraded_since is not None:
                    log.info("Watchdog: physical telemetry recovered after %.0fs",
                             now - self._degraded_since)
                self._degraded_since = None
                self._last_tunnel_recovery_request = None
                continue
            if self._degraded_since is None:
                self._degraded_since = now
                log.warning("Watchdog: physical telemetry degraded; readiness remains false; worker recovery continues (%s)",
                            state)
            degraded_for = now - self._degraded_since
            cooldown_elapsed = (
                self._last_tunnel_recovery_request is None
                or now - self._last_tunnel_recovery_request >= TUNNEL_RECOVERY_COOLDOWN_SECONDS
            )
            if degraded_for >= TUNNEL_RECOVERY_THRESHOLD_SECONDS and cooldown_elapsed:
                try:
                    fd = os.open(TUNNEL_RECOVERY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    # A request already awaits consumption. Start the cooldown
                    # so a slow tunnel transition cannot spam/rewrite the marker.
                    self._last_tunnel_recovery_request = now
                except OSError:
                    log.exception("Watchdog could not request tunnel recovery")
                    self._last_tunnel_recovery_request = now
                else:
                    os.close(fd)
                    self._last_tunnel_recovery_request = now
                    log.warning("Watchdog requested a single-owner tunnel reconnect after %.0fs degraded telemetry",
                                degraded_for)

    def _trigger_restart(self, elapsed: float) -> None:
        log.error("Watchdog: poll worker made no progress for %.0fs (>%.0fs threshold) — "
                  "exiting for container restart (fresh socat tunnel). last_error=%s",
                  elapsed, WATCHDOG_THRESHOLD_SECONDS, self._dev._last_error)
        try:
            activity_log.record_status_poll(
                "watchdog", "error",
                f"poll worker stalled {elapsed:.0f}s -> container restart")
        except Exception:  # noqa: BLE001
            pass
        # flush stdio so the reason survives the abrupt exit, then hard-exit.
        # os._exit avoids being swallowed by any handler; non-zero so Docker's
        # restart policy (unless-stopped) recreates the container.
        import sys
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)


watchdog = LivenessWatchdog(device)


def _coerce_level(payload: str) -> int:
    try:
        v = json.loads(payload)
        v = v.get("level") if isinstance(v, dict) else v
    except Exception:  # noqa: BLE001
        v = payload
    level = int(v)
    if not 0 <= level <= 4:
        raise ValueError("level out of range 0-4")
    return level


def _coerce_mode(payload: str) -> str:
    try:
        v = json.loads(payload)
        v = v.get("mode") if isinstance(v, dict) else v
    except Exception:  # noqa: BLE001
        v = payload
    mode = str(v).strip().lower()
    if mode not in ("auto", "heat", "cool", "off"):
        raise ValueError("mode must be auto|heat|cool|off")
    return mode


def _coerce_setpoint(payload: str) -> int:
    try:
        v = json.loads(payload)
        v = v.get("setpoint") if isinstance(v, dict) else v
    except Exception:  # noqa: BLE001
        v = payload
    sp = int(round(float(v)))
    if not 5 <= sp <= 30:
        raise ValueError("setpoint out of range 5-30")
    return sp


def _coerce_room(payload: str) -> float:
    v = float(payload)
    if not ROOM_MIN_C <= v <= ROOM_MAX_C:  # also rejects nan/inf
        raise ValueError(f"room temperature out of range {ROOM_MIN_C:g}-{ROOM_MAX_C:g}")
    return v


# ---- FastAPI ----------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    device.start()
    mqtt_layer.start()
    watchdog.start()
    yield
    watchdog.stop()
    device.stop()


app = FastAPI(title="Nilan CTS600 API", version="1.0", lifespan=lifespan)


def auth(authorization: Optional[str] = Header(default=None)) -> None:
    if not API_TOKEN:
        return  # auth enforced at reverse proxy; direct token optional
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


class FanBody(BaseModel):
    level: int = Field(ge=0, le=4)


class ModeBody(BaseModel):
    mode: str


class TempBody(BaseModel):
    setpoint: int = Field(ge=5, le=30)


class RoomBody(BaseModel):
    celsius: float = Field(ge=ROOM_MIN_C, le=ROOM_MAX_C, allow_inf_nan=False)
    source: str = Field(default="api", min_length=1, max_length=40)


@app.get("/healthz")
def healthz():
    """Process liveness only. Docker restart decisions use /readyz separately."""
    return {"ok": True, "connected": device._connected, "mockup": MOCKUP,
            "telemetry_ready": device.readiness()["ready"],
            "read_only": READ_ONLY, "watchdog": watchdog.status()}


@app.get("/readyz")
def readyz():
    """Physical readiness: only a recent, complete unit poll counts as ready."""
    state = device.readiness()
    if not state["ready"]:
        return JSONResponse(status_code=503, content=state)
    return state


@app.get("/api/meta")
def get_meta(_: None = Depends(auth)):
    """Self-describing config + UI metadata. The dashboard reads this once to
    render labels/units, the current device configuration, and to know whether
    control (Phase 2) is unlocked (read_only=false + a live bus)."""
    return {
        "app_version": APP_VERSION,
        "mockup": MOCKUP,
        "read_only": READ_ONLY,
        "poll_seconds": POLL_SECONDS,
        "base_topic": MQTT_BASE,
        "activity_log": {
            "retention_max_events": activity_log.maxlen,
            "read_summary_seconds": ACTIVITY_READ_SUMMARY_SECONDS,
            "read_summary_count": ACTIVITY_READ_SUMMARY_COUNT,
        },
        "esp_bridge": {"ip": ESP_IP or None, "port": ESP_PORT},
        "sensor_mapping": {
            "t_room": T_ROOM_KEY,
            "t_supply": T_SUPPLY_KEY,
            "t_exhaust": T_EXHAUST_KEY,
        },
        # Control envelope (Phase 2). Surfaced now so the UI can validate input
        # the moment writes are unlocked.
        "controls": {
            "fan": {"min": 0, "max": 4, "off_level": 0},
            "mode": ["auto", "heat", "cool", "off"],
            "setpoint": {"min": 5, "max": 30, "unit": "°C"},
        },
        # Friendly German labels for the curated fields + common raw keys.
        # Unknown raw keys fall back to the key name in the UI.
        "labels": {
            "t_room": "Raumtemperatur",
            "t_supply": "Zulufttemperatur",
            "t_exhaust": "Außen/Frischluft",
            "fan_level": "Lüfterstufe",
            "mode": "Betriebsmodus",
            "setpoint": "Solltemperatur",
            "status": "Status",
            "display": "Display-Text",
            # Sensor labels confirmed live from the unit's "ANZEIGE DATEN" menu
            # (TUE-55): RAUM=T15, ZULUFT=T2, FRISCHL.=T1, KONDENS.=T5, VERDAMP.=T6.
            "T15": "Raum (T15)", "T2": "Zuluft (T2)", "T1": "Frischluft/Außen (T1)",
            "T5": "Kondensator (T5)", "T6": "Verdampfer (T6)",
            "ZULUFT_STUFE": "Zuluft-Stufe", "ABLUFT_STUFE": "Abluft-Stufe",
            "flow": "Lüfterstufe", "thermostat": "Solltemperatur",
            "program": "Programm",
            "humidity": "Luftfeuchte", "RH": "Luftfeuchte",
            "filter": "Filter", "led": "LED",
        },
        "units": {
            "t_room": "°C", "t_supply": "°C", "t_exhaust": "°C",
            "setpoint": "°C", "thermostat": "°C",
            "T15": "°C", "T1": "°C", "T2": "°C", "T5": "°C", "T6": "°C", "T7": "°C",
            "humidity": "%", "RH": "%",
        },
        # Plain-language meaning of each parameter, from the Nilan CTS600 manual +
        # frodef's reverse-engineering (TUE-55). Lets the UI explain values inline.
        "descriptions": {
            "T1": "Frischluft/Außentemperatur — angesaugte Außenluft (Fühler an der Nordseite).",
            "T2": "Zulufttemperatur am Ventilator, vor einem evtl. Nachheizregister (heißt T7 mit Heizregister).",
            "T3": "Frischluftfühler (an diesem Gerät nicht belegt).",
            "T4": "Gegenstrom-Wärmetauscher (an diesem Gerät nicht belegt).",
            "T5": "Kondensatortemperatur der Wärmepumpe.",
            "T6": "Verdampfertemperatur der Wärmepumpe.",
            "T15": "Fühler im CTS600-Bedienpanel. Das Panel ist durch den ESP ersetzt, "
                   "daher injiziert der Daemon diesen Raumwert: live aus einem externen Fühler "
                   "(POST /api/room, MQTT room/set; verfällt nach dem TTL) oder, ohne frischen "
                   "Messwert, als fester Fallback — dann KEIN echter Raumfühler.",
            "t_room": "Raumtemperatur, die das Gerät zur Regelung nutzt (= T15). Modus live: zuletzt "
                      "per POST /api/room bzw. MQTT room/set gemeldeter Messwert (gilt nur bis zum TTL); "
                      "Modus fallback: fester Ersatzwert des Daemons. Siehe room_source im Status.",
            "t_supply": "Zulufttemperatur (T2) — Luft, die in die Wohnung geblasen wird.",
            "t_exhaust": "Frischluft/Außen (T1) — angesaugte Außenluft.",
            "setpoint": "Solltemperatur (Thermostat), 5–30 °C — gewünschte Raumtemperatur.",
            "fan_level": "Lüfterstufe 0–4 (0 = aus, 4 = höchste).",
            "mode": "Betriebsmodus: auto (automatisch heizen/kühlen), heat, cool, off.",
            "status": "Aktueller Betriebszustand laut Gerät (z. B. KÜHLEN, HEIZEN).",
            "ZULUFT_STUFE": "Aktuelle Zuluft-Ventilatorstufe.",
            "ABLUFT_STUFE": "Aktuelle Abluft-Ventilatorstufe.",
            "program": "Aktives Zeitprogramm (Wochenprogramm), sofern gesetzt.",
            "led": "Betriebs-LED des Panels (an = Gerät läuft).",
        },
    }


@app.get("/api/status")
def get_status(_: None = Depends(auth)):
    activity_log.record_status_poll("rest-api", "ok")
    return device.status()


@app.get("/api/activity")
def get_activity(
    _: None = Depends(auth),
    limit: int = Query(default=100, ge=1, le=500),
    include_reads: bool = Query(default=False),
):
    return activity_log.list_events(limit=limit, include_reads=include_reads)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """Read-only web dashboard (TUE-55 Phase 1). Served same-origin so it lives
    behind the same Caddy auth as the API."""
    try:
        return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})
    except FileNotFoundError:
        return PlainTextResponse("dashboard.html missing from image", status_code=500)


@app.get("/manifest.webmanifest")
def web_manifest(_: None = Depends(auth)):
    if not MANIFEST_FILE.exists():
        return PlainTextResponse("manifest.webmanifest missing from image", status_code=500)
    return FileResponse(
        MANIFEST_FILE,
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/sw.js")
def service_worker(_: None = Depends(auth)):
    if not SERVICE_WORKER_FILE.exists():
        return PlainTextResponse("sw.js missing from image", status_code=500)
    return FileResponse(
        SERVICE_WORKER_FILE,
        media_type="text/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store", "Service-Worker-Allowed": "/"},
    )


@app.get("/icons/{filename}")
def pwa_icon(filename: str, _: None = Depends(auth)):
    allowed = {"homeboard-180.png", "homeboard-192.png", "homeboard-512.png"}
    if filename not in allowed:
        raise HTTPException(status_code=404, detail="icon not found")
    path = ICON_DIR / filename
    if not path.exists():
        return PlainTextResponse(f"{filename} missing from image", status_code=500)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


def _device_write(action_type: str, target: str, value: Any, fn, *args):
    """Run a device write, turning device/protocol failures into a clean 502."""
    if READ_ONLY:
        activity_log.record(
            source="rest-api",
            action_type=action_type,
            target=target,
            result="blocked",
            detail="rejected because NILAN_READ_ONLY is enabled",
            value=_safe_activity_value(value),
        )
        raise HTTPException(status_code=403, detail="device is in read-only bring-up mode")
    try:
        fn(*args)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        activity_log.record(
            source="rest-api",
            action_type=action_type,
            target=target,
            result="error",
            detail=str(e),
            value=_safe_activity_value(value),
        )
        log.warning("device write failed: %s", e)
        raise HTTPException(status_code=502, detail=f"device command failed: {e}")
    activity_log.record(
        source="rest-api",
        action_type=action_type,
        target=target,
        result="ok",
        value=_safe_activity_value(value),
    )
    return device.status()


@app.post("/api/fan")
def post_fan(body: FanBody, _: None = Depends(auth)):
    return _device_write("set_fan", "/api/fan", {"level": body.level}, device.set_fan, body.level)


@app.post("/api/mode")
def post_mode(body: ModeBody, _: None = Depends(auth)):
    m = body.mode.strip().lower()
    if m not in ("auto", "heat", "cool", "off"):
        activity_log.record(
            source="rest-api",
            action_type="set_mode",
            target="/api/mode",
            result="error",
            detail="mode must be auto|heat|cool|off",
            value=_safe_activity_value({"mode": body.mode}),
        )
        raise HTTPException(status_code=422, detail="mode must be auto|heat|cool|off")
    return _device_write("set_mode", "/api/mode", {"mode": m}, device.set_mode, m)


@app.post("/api/temp")
def post_temp(body: TempBody, _: None = Depends(auth)):
    return _device_write("set_temp", "/api/temp", {"setpoint": body.setpoint}, device.set_temp, body.setpoint)


@app.post("/api/room")
def post_room(body: RoomBody, _: None = Depends(auth)):
    if ROOM_TTL_SECONDS <= 0 and not READ_ONLY:
        activity_log.record(
            source="rest-api",
            action_type="set_room",
            target="/api/room",
            result="blocked",
            detail="live room feed disabled (NILAN_ROOM_TTL_SECONDS=0)",
            value=_safe_activity_value({"celsius": body.celsius, "source": body.source}),
        )
        raise HTTPException(status_code=409, detail="live room feed disabled (NILAN_ROOM_TTL_SECONDS=0)")
    return _device_write("set_room", "/api/room", {"celsius": body.celsius, "source": body.source},
                         device.set_room, body.celsius, body.source)


if __name__ == "__main__":
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, log_level="info")
