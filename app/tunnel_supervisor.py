"""Supervise the single-owner raw TCP tunnel with ordered transports."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from dataclasses import dataclass


log = logging.getLogger("nilan-tunnel")
RECOVERY_FILE = os.environ.get("NILAN_TUNNEL_RECOVERY_FILE", "/run/nilan-tunnel-recover")
# TCP_USER_TIMEOUT is Linux TCP option 18 at IPPROTO_TCP level 6 (milliseconds).
TCP_USER_TIMEOUT_MS = 15_000


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def endpoints_from_env(env: dict[str, str] | None = None) -> list[Endpoint]:
    env = os.environ if env is None else env
    host = env.get("ESP_IP", "").strip()
    if not host:
        raise ValueError("ESP_IP must configure the primary bridge endpoint")
    port = int(env.get("ESP_PORT", "6638"))
    if not 1 <= port <= 65535:
        raise ValueError("ESP_PORT must be 1..65535")
    endpoints = [Endpoint(host, port)]
    fallback = env.get("ESP_FALLBACK_IP", "").strip()
    if fallback:
        fallback_port = int(env.get("ESP_FALLBACK_PORT", str(port)))
        if not 1 <= fallback_port <= 65535:
            raise ValueError("ESP_FALLBACK_PORT must be 1..65535")
        candidate = Endpoint(fallback, fallback_port)
        if candidate not in endpoints:
            endpoints.append(candidate)
    return endpoints


def consume_recovery_request(path: str | Path = RECOVERY_FILE) -> bool:
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def socat_command(endpoint: Endpoint, tty: str) -> list[str]:
    """Build a tunnel with bounded TCP failure detection (Linux container)."""
    return [
        "socat", "-d", f"pty,link={tty},raw,echo=0,mode=666",
        f"tcp:{endpoint.host}:{endpoint.port},connect-timeout=5,keepalive,"
        f"keepidle=15,keepintvl=5,keepcnt=3,"
        f"setsockopt-int=6:18:{TCP_USER_TIMEOUT_MS}",
    ]


def run_socat(
    endpoint: Endpoint, tty: str, stop: threading.Event,
    recovery_file: str = RECOVERY_FILE,
    recovery_requested: threading.Event | None = None,
) -> float:
    """Own exactly one serial tunnel until it closes or shutdown is requested."""
    command = socat_command(endpoint, tty)
    started = time.monotonic()
    proc = subprocess.Popen(command, start_new_session=True)
    try:
        while proc.poll() is None:
            if consume_recovery_request(recovery_file):
                log.warning("API requested a tunnel reconnect; closing %s before failover", endpoint)
                if recovery_requested is not None:
                    recovery_requested.set()
                _terminate_process_group(proc)
                break
            if stop.wait(0.5):
                _terminate_process_group(proc)
                break
            # Polling the worker here also bounds how long shutdown waits.
        return time.monotonic() - started
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def run_cycle(
    endpoints: list[Endpoint], tty: str, stop: threading.Event,
    attempt=run_socat,
) -> float:
    """Try endpoints in order, serially; keep each selected stream until failure."""
    longest_session = 0.0
    recovery_requested = threading.Event()
    pending = list(endpoints)
    while pending:
        endpoint = pending.pop(0)
        if stop.is_set():
            break
        log.info("Opening Nilan raw serial tunnel to %s", endpoint)
        try:
            duration = attempt(endpoint, tty, stop, recovery_requested=recovery_requested)
        except OSError:
            log.exception("Could not start socat for %s", endpoint)
            duration = 0.0
        longest_session = max(longest_session, duration)
        if recovery_requested.is_set():
            recovery_requested.clear()
            if pending:
                log.warning("Tunnel recovery requested; advancing to %s", pending[0])
            else:
                log.warning("Tunnel recovery requested; no alternate endpoint remains in this cycle")
            continue
        if not stop.is_set():
            log.warning("Tunnel to %s closed after %.1fs", endpoint, duration)
    return longest_session


def supervise(endpoints: list[Endpoint], tty: str, stop: threading.Event) -> None:
    delay = 5
    while not stop.is_set():
        stable_seconds = run_cycle(endpoints, tty, stop)
        if stable_seconds >= 60:
            delay = 5
        if stop.is_set():
            break
        log.warning("All tunnel endpoints failed; retrying in %ss", delay)
        if stop.wait(delay):
            break
        delay = min(60, delay * 2)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        endpoints = endpoints_from_env()
    except (ValueError, TypeError) as exc:
        raise SystemExit(f"FATAL: invalid ESP endpoint configuration: {exc}") from exc
    tty = os.environ.get("NILAN_PORT", "/dev/ttyNILAN")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: stop.set())
    log.info("Supervised Nilan endpoints (ordered, single-owner): %s", ", ".join(map(str, endpoints)))
    supervise(endpoints, tty, stop)


if __name__ == "__main__":
    main()
