import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT / "app"))

# Must be set before nilan_api is imported (module-level config).
os.environ["NILAN_MOCKUP"] = "1"
os.environ["NILAN_READ_ONLY"] = "0"
os.environ["NILAN_API_TOKEN"] = ""
os.environ["MQTT_HOST"] = ""
os.environ["NILAN_ROOM_TTL_SECONDS"] = "900"
os.environ["NILAN_T15_FALLBACK"] = "21"
