import json
import signal
import sys
import threading

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
from process_supervisor import supervise  # noqa: E402

commands = json.loads(Path(sys.argv[1]).read_text())
stop = threading.Event()
for signum in (signal.SIGTERM, signal.SIGINT):
    signal.signal(signum, lambda _sig, _frame: stop.set())
raise SystemExit(supervise(
    commands["api"],
    commands.get("tunnel"),
    stop,
    poll_seconds=0.025,
))
