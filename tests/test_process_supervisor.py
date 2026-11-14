import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "fixtures" / "run_process_supervisor.py"


def _write_child(path: Path, code: str) -> list[str]:
    path.write_text(code)
    return [sys.executable, str(path)]


def _wait_for(path: Path, process: subprocess.Popen, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not path.exists():
        if process.poll() is not None:
            raise AssertionError(f"coordinator exited early with status {process.returncode}")
        time.sleep(0.01)
    assert path.exists(), f"timed out waiting for {path.name}"


def test_unexpected_tunnel_child_exit_stops_api_and_fails_container(tmp_path):
    api_started = tmp_path / "api-started"
    api_stopped = tmp_path / "api-stopped"
    tunnel_exited = tmp_path / "tunnel-exited"
    api = _write_child(tmp_path / "fake_api.py", f"""
import pathlib, signal, time, sys
started = pathlib.Path({str(api_started)!r})
stopped = pathlib.Path({str(api_stopped)!r})
started.write_text('yes')
def shutdown(_sig, _frame):
    stopped.write_text('yes')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, shutdown)
while True: time.sleep(0.02)
""")
    tunnel = _write_child(tmp_path / "fake_tunnel_supervisor.py", f"""
import pathlib, time
pathlib.Path({str(tunnel_exited)!r}).write_text('socat child exited unexpectedly')
""")
    command_file = tmp_path / "commands.json"
    command_file.write_text(json.dumps({"api": api, "tunnel": tunnel}))

    result = subprocess.run(
        [sys.executable, str(RUNNER), str(command_file)],
        capture_output=True, text=True, timeout=5,
    )

    assert result.returncode == 1
    assert api_started.exists()
    assert api_stopped.exists()
    assert tunnel_exited.exists()
    assert "tunnel-supervisor exited unexpectedly" in result.stderr


def test_sigterm_gracefully_stops_and_reaps_both_children(tmp_path):
    api_started = tmp_path / "api-started"
    api_stopped = tmp_path / "api-stopped"
    tunnel_started = tmp_path / "tunnel-started"
    tunnel_stopped = tmp_path / "tunnel-stopped"
    api = _write_child(tmp_path / "fake_api.py", f"""
import pathlib, signal, time, sys
pathlib.Path({str(api_started)!r}).write_text('yes')
def shutdown(_sig, _frame):
    pathlib.Path({str(api_stopped)!r}).write_text('yes')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, shutdown)
while True: time.sleep(0.02)
""")
    tunnel = _write_child(tmp_path / "fake_tunnel_supervisor.py", f"""
import pathlib, signal, time, sys
pathlib.Path({str(tunnel_started)!r}).write_text('yes')
def shutdown(_sig, _frame):
    pathlib.Path({str(tunnel_stopped)!r}).write_text('yes')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, shutdown)
while True: time.sleep(0.02)
""")
    command_file = tmp_path / "commands.json"
    command_file.write_text(json.dumps({"api": api, "tunnel": tunnel}))
    coordinator = subprocess.Popen(
        [sys.executable, str(RUNNER), str(command_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )

    try:
        _wait_for(api_started, coordinator)
        _wait_for(tunnel_started, coordinator)
        coordinator.send_signal(signal.SIGTERM)
        stdout, stderr = coordinator.communicate(timeout=5)
    finally:
        if coordinator.poll() is None:
            coordinator.kill()
            coordinator.wait(timeout=2)

    assert coordinator.returncode == 0, (stdout, stderr)
    assert api_stopped.exists()
    assert tunnel_stopped.exists()
