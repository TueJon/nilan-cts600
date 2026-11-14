"""Keep the Nilan tunnel supervisor and API in one container failure domain."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence

from tunnel_supervisor import endpoints_from_env


log = logging.getLogger("nilan-process-supervisor")


def stop_children(children: dict[str, subprocess.Popen], grace_seconds: float = 8) -> None:
    """Stop all children together, then reap them; kill process groups as needed."""
    for process in children.values():
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    deadline = time.monotonic() + grace_seconds
    for name, process in children.items():
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            log.error("Child %s did not stop after SIGTERM; sending SIGKILL", name)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def supervise(
    api_command: Sequence[str],
    tunnel_command: Sequence[str] | None,
    stop: threading.Event,
    *,
    spawn=subprocess.Popen,
    poll_seconds: float = 0.25,
) -> int:
    """Run children until a signal or unexpected child exit, then clean up."""
    children: dict[str, subprocess.Popen] = {}
    failed_child: str | None = None
    try:
        if tunnel_command is not None:
            children["tunnel-supervisor"] = spawn(list(tunnel_command), start_new_session=True)
        children["nilan-api"] = spawn(list(api_command), start_new_session=True)
        log.info("Started managed children: %s", ", ".join(children))

        while not stop.wait(poll_seconds):
            for name, process in children.items():
                return_code = process.poll()
                if return_code is not None:
                    failed_child = name
                    log.critical("Managed child %s exited unexpectedly with status %s",
                                 name, return_code)
                    break
            if failed_child is not None:
                break
    except Exception:
        log.exception("Failed while supervising Nilan service processes")
        failed_child = failed_child or "process-supervisor"
    finally:
        stop_children(children)

    return 1 if failed_child else 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mockup = os.environ.get("NILAN_MOCKUP", "0").strip().lower() not in ("0", "", "false")
    tunnel_command = None
    if not mockup:
        try:
            endpoints = endpoints_from_env()
        except (ValueError, TypeError) as exc:
            log.critical("Invalid bridge endpoint configuration; refusing to start: %s", exc)
            return 1
        log.info("Validated ordered bridge endpoints: %s", ", ".join(map(str, endpoints)))
        tunnel_command = [sys.executable, "/app/tunnel_supervisor.py"]

    api_command = [
        sys.executable, "-m", "uvicorn", "nilan_api:app",
        "--host", os.environ.get("NILAN_HTTP_HOST", "0.0.0.0"),
        "--port", os.environ.get("NILAN_HTTP_PORT", "8642"),
    ]
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: stop.set())
    return supervise(api_command, tunnel_command, stop)


if __name__ == "__main__":
    raise SystemExit(main())
