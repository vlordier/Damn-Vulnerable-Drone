"""Read readiness evidence only from a run-owned local NATS log."""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlsplit

from dvd_research.evidence_contract import contains_symlink_component


def read_run_nats_log(run_dir: Path) -> str | None:
    """Return the NATS log contents, waiting callers may treat absent logs as not ready.

    Unsafe roots and linked paths raise ``ValueError`` so callers cannot mistake an
    external log for evidence that their run-owned broker is listening.
    """
    run_dir = Path(run_dir)
    if run_dir.is_symlink():
        raise ValueError("run directory is a symlink")
    try:
        root = run_dir.resolve(strict=True)
    except FileNotFoundError:
        return None
    if not root.is_dir():
        raise ValueError("run path is not a directory")
    relative = Path("logs") / "nats.log"
    if contains_symlink_component(root, relative):
        raise ValueError("run-owned NATS log path contains a symlink")
    log_path = root / relative
    try:
        if not log_path.is_file():
            return None
        log_path.resolve(strict=True).relative_to(root)
        return log_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ValueError("run-owned NATS log cannot be verified") from error


def validate_run_nats_endpoint(server: str, run_dir: Path, deadline: float) -> int:
    """Wait for this run's broker to confirm the exact loopback endpoint."""
    try:
        endpoint = urlsplit(server)
        port = endpoint.port
    except ValueError as error:
        raise ValueError("refusing an invalid NATS endpoint") from error
    if (
        endpoint.scheme != "nats"
        or endpoint.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.path not in ("", "/")
        or endpoint.query
        or endpoint.fragment
        or endpoint.netloc != f"127.0.0.1:{port}"
    ):
        raise ValueError("refusing any NATS endpoint except an explicit IPv4 loopback URL")

    expected_line = f"Listening for client connections on 127.0.0.1:{port}"
    while time.monotonic() < deadline:
        log = read_run_nats_log(run_dir) or ""
        if expected_line in log and "Server is ready" in log:
            return port
        time.sleep(0.1)
    raise TimeoutError("run-owned NATS did not confirm the requested loopback endpoint")
