"""Run the host research UI in loopback-only mode."""
from __future__ import annotations

import os
from pathlib import Path

from flask import redirect, request, url_for

PROJECT_ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("RESEARCH_DIR", str(PROJECT_ROOT / "research"))
os.environ.setdefault(
    "RESEARCH_EVIDENCE_DIR",
    str(PROJECT_ROOT / "build_logs" / "research" / "antoine-scenarios"),
)

from .app import app


@app.before_request
def route_research_console_root():
    """Keep the standalone console root on its research landing page."""
    if request.path == "/":
        return redirect(url_for("main.antoine_research_index"))
    return None

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("DVD_RESEARCH_UI_PORT", "8001")),
        debug=False,
        use_reloader=False,
    )
