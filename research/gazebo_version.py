"""Read the installed Gazebo Sim version from its supported CLI command."""

from __future__ import annotations

import subprocess
from pathlib import Path


def gazebo_version(binary: str | Path) -> str | None:
    """Return the Gazebo Sim version, or ``None`` when the CLI cannot report it."""
    try:
        result = subprocess.run(
            [str(binary), "sim", "--versions"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)
