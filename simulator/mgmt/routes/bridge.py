# simulator/mgmt/routes/telemetry.py
import logging
import os

import requests

# python-socketio client (to receive from Companion Computer)


log = logging.getLogger(__name__)

DEFAULT_COMPANION = "http://10.13.0.3:3000"

COMPANION_BASE_URL = os.getenv("COMPANION_BASE_URL", DEFAULT_COMPANION)

# ---------- Public helpers used by other modules ----------
def start_companion_telemetry(data: dict):
    url = f"{COMPANION_BASE_URL}/telemetry/start-telemetry"
    try:
        r = requests.post(url, json=data, timeout=10)
        log.info("[telemetry] start -> %s %s", r.status_code, r.text[:200])
    except Exception:
        pass

def stop_companion_telemetry():
    url = f"{COMPANION_BASE_URL}/telemetry/stop-telemetry"
    try:
        r = requests.post(url, timeout=10)
        log.info("[telemetry] stop -> %s %s", r.status_code, r.text[:200])
    except Exception:
        pass