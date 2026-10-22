#!/usr/bin/env python3
"""Exercise isolated headless profile start, status, stop, stale metadata, restart."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import interactive_profile

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "research" / "run-antoine-gazebo-mavlink.sh"
START_TIMEOUT_SECONDS = 180.0
STOP_TIMEOUT_SECONDS = 45.0
FORCED_STOP_TIMEOUT_SECONDS = 5.0


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _wait_recorded(receipt: Path, process: subprocess.Popen[Any], log_path: Path) -> dict[str, Any]:
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"profile exited with {process.returncode} before readiness; "
                f"see {log_path}: {log_path.read_text(encoding='utf-8', errors='replace')[-5000:]}"
            )
        if receipt.is_file():
            last = interactive_profile.inspect(receipt)
            if last.get("status") == "recorded" and last.get("armed") is False:
                return last
        time.sleep(1.0)
    raise TimeoutError(f"headless profile did not become recorded/disarmed: {last}")


def _stop_and_wait(
    receipt: Path, process: subprocess.Popen[Any], log_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    stop_result = interactive_profile.stop_owned(receipt)
    if stop_result.get("status") != "stopped":
        raise RuntimeError(f"identity-checked stop was not complete: {stop_result}")
    try:
        return_code = process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(f"profile wrapper did not exit after stop; see {log_path}") from error
    if return_code not in {0, 128 + signal.SIGTERM}:
        raise RuntimeError(
            f"profile wrapper exited with {return_code}; see "
            f"{log_path}: {log_path.read_text(encoding='utf-8', errors='replace')[-5000:]}"
        )
    final_status = interactive_profile.inspect(receipt)
    if final_status.get("status") != "stopped":
        raise RuntimeError(f"post-stop status did not confirm cleanup: {final_status}")
    return stop_result, final_status


def _stop_wrapper_process(process: subprocess.Popen[Any], report: dict[str, Any]) -> None:
    """Stop only the process group created for this lifecycle run."""
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
        return
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError as error:
        report.setdefault("cleanupSignalFailures", []).append(
            f"SIGINT wrapper: {type(error).__name__}: {error}"
        )

    report["cleanupEscalation"] = []
    for stop_signal in (signal.SIGTERM, signal.SIGKILL):
        report["cleanupEscalation"].append(signal.Signals(stop_signal).name)
        try:
            # _start uses start_new_session=True, so this group contains only
            # the wrapper and processes launched by this lifecycle run.
            os.killpg(process.pid, stop_signal)
        except ProcessLookupError:
            pass
        except OSError as error:
            report.setdefault("cleanupSignalFailures", []).append(
                f"{signal.Signals(stop_signal).name}: {type(error).__name__}: {error}"
            )
            try:
                process.send_signal(stop_signal)
            except ProcessLookupError:
                pass
            except OSError as fallback_error:
                report.setdefault("cleanupSignalFailures", []).append(
                    f"{signal.Signals(stop_signal).name} wrapper fallback: "
                    f"{type(fallback_error).__name__}: {fallback_error}"
                )
        try:
            process.wait(timeout=FORCED_STOP_TIMEOUT_SECONDS)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            continue

    report["cleanupStopFailure"] = (
        "run-owned process group remained alive after SIGINT, SIGTERM, and SIGKILL"
    )


def _start(run_dir: Path, log_path: Path) -> tuple[subprocess.Popen[Any], Path]:
    if run_dir.exists():
        raise FileExistsError(f"refusing to reuse lifecycle run directory: {run_dir}")
    log = log_path.open("w", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "ANTOINE_GZ_OPEN_QGC": "0",
            "ANTOINE_GZ_HEADLESS_ACCEPTANCE": "0",
            "ANTOINE_GZ_FLIGHT_GUI": "0",
            "ANTOINE_GZ_MAVLINK_RUN_DIR": str(run_dir),
            "RUSTUP_TOOLCHAIN": environment.get("RUSTUP_TOOLCHAIN", "1.99.1"),
        }
    )
    process = subprocess.Popen(
        ["bash", str(RUNNER)],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    return process, run_dir / "interactive-profile-receipt.json"


def _exercise_stale_identity(receipt_path: Path) -> dict[str, Any]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    bridge = receipt["processes"]["mavlinkBridge"]
    stale_path = receipt_path.parent / "stale-run-receipt.json"
    stale_receipt = dict(receipt)
    stale_receipt["processes"] = {
        "mavlinkBridge": {"pid": bridge["pid"], "identity": "stale-run-identity"}
    }
    stale_path.write_text(json.dumps(stale_receipt, indent=2) + "\n", encoding="utf-8")
    result = interactive_profile.stop_owned(stale_path)
    if result.get("results", {}).get("mavlinkBridge") != "identity-mismatch-not-signalled":
        raise RuntimeError(f"stale run metadata did not fail closed: {result}")
    if interactive_profile.process_identity(bridge["pid"]) != bridge["identity"]:
        raise RuntimeError("stale metadata changed the live bridge process identity")
    return result


def run(report_path: Path) -> dict[str, Any]:
    evidence_root = Path(tempfile.mkdtemp(prefix="dvd-antoine-headless-lifecycle-"))
    report: dict[str, Any] = {
        "schema": "dvd.antoine-headless-profile-lifecycle.v1",
        "createdAtUtc": datetime.now(UTC).isoformat(),
        "passed": False,
        "qgcStarted": False,
        "evidenceDirectory": str(evidence_root),
        "runs": [],
    }
    active: tuple[Path, subprocess.Popen[Any], Path] | None = None
    try:
        for index in (1, 2):
            run_dir = evidence_root / f"run-{index}"
            log_path = evidence_root / f"run-{index}.log"
            process, receipt = _start(run_dir, log_path)
            active = (receipt, process, log_path)
            run_evidence: dict[str, Any] = {
                "runDirectory": str(run_dir),
                "receipt": str(receipt),
                "launcherLog": str(log_path),
            }
            report["runs"].append(run_evidence)
            ready = _wait_recorded(receipt, process, log_path)
            run_evidence["runId"] = ready.get("runId")
            run_evidence["readyStatus"] = ready
            if ready.get("profile") != interactive_profile.HEADLESS_PROFILE:
                raise RuntimeError(f"unexpected profile type: {ready.get('profile')}")
            if ready.get("qgcHeartbeat", {}).get("status") != "not-applicable":
                raise RuntimeError("headless profile incorrectly claimed a QGC heartbeat")
            if index == 2:
                run_evidence["staleMetadataAttempt"] = _exercise_stale_identity(receipt)
                live_status = interactive_profile.inspect(receipt)
                if live_status.get("status") != "recorded" or live_status.get("armed") is not False:
                    raise RuntimeError(f"stale metadata affected the active profile: {live_status}")
                run_evidence["statusAfterStaleAttempt"] = live_status
            stop_result, stopped = _stop_and_wait(receipt, process, log_path)
            run_evidence["stopResult"] = stop_result
            run_evidence["stoppedStatus"] = stopped
            active = None
        report["passed"] = True
        return report
    except BaseException as error:
        report["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if active is not None:
            receipt, process, _log_path = active
            try:
                if receipt.is_file():
                    report["cleanupStopResult"] = interactive_profile.stop_owned(receipt)
            except Exception as cleanup_error:
                report["cleanupStopFailure"] = f"{type(cleanup_error).__name__}: {cleanup_error}"
            _stop_wrapper_process(process, report)
        _write_report(report_path, report)


def main() -> int:
    path = Path(
        os.environ.get(
            "ANTOINE_HEADLESS_LIFECYCLE_REPORT",
            f"/tmp/dvd-antoine-headless-lifecycle-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json",
        )
    )
    try:
        report = run(path)
    except Exception as error:
        print(f"Headless lifecycle acceptance failed: {error}", file=sys.stderr)
        print(f"Lifecycle evidence: {path}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"Headless lifecycle evidence: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
