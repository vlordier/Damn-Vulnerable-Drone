#!/usr/bin/env python3
"""Check that this Python interpreter can receive local MAVLink UDP traffic."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path


def _application_firewall_blocks_python() -> bool:
    firewall_tool = Path("/usr/libexec/ApplicationFirewall/socketfilterfw")
    if sys.platform != "darwin" or not firewall_tool.exists():
        return False
    try:
        result = subprocess.run(
            [str(firewall_tool), "--getappblocked", str(Path(sys.executable).resolve())],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "blocked" in result.stdout.lower()


def local_udp_loopback_available(timeout_seconds: float = 0.5) -> bool:
    if _application_firewall_blocks_python():
        return False
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(timeout_seconds)
        expected = b"dvd-udp-loopback-check"
        sender.sendto(expected, receiver.getsockname())
        payload, _ = receiver.recvfrom(64)
        return payload == expected
    except OSError:
        return False
    finally:
        receiver.close()
        sender.close()


def main() -> int:
    if local_udp_loopback_available():
        print(f"Local UDP receive is available to {sys.executable}")
        return 0
    print(
        "Local UDP receive is unavailable to "
        f"{sys.executable}; check the host firewall and network policy.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
