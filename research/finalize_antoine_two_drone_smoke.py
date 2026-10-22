#!/usr/bin/env python3
"""Gate two-drone smoke success on complete, stable run provenance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dvd_research.run_files import read_run_text, write_run_json


def finalize(run_dir: Path, capture_exit_code: int) -> tuple[dict, int]:
    report_text = read_run_text(run_dir, Path("research-two-drone-smoke.json"))
    if report_text is None:
        raise ValueError("two-drone smoke report is unavailable")
    report = json.loads(report_text)
    if not isinstance(report, dict):
        raise ValueError("two-drone smoke report must be a JSON object")

    try:
        comparison_text = read_run_text(run_dir, Path("research-provenance-comparison.json"))
    except (OSError, ValueError):
        comparison_text = None
    try:
        comparison = json.loads(comparison_text) if comparison_text else {}
    except json.JSONDecodeError:
        comparison = {}
    if not isinstance(comparison, dict):
        comparison = {}
    stable = capture_exit_code == 0 and comparison.get("stable") is True
    report["provenanceStable"] = stable
    report["provenance"] = {
        "status": "stable" if stable else "inconclusive",
        "captureExitCode": capture_exit_code,
        "changedSourceTrees": comparison.get("changedSourceTrees", []),
        "changedInputs": comparison.get("changedInputs", []),
    }
    if not stable and report.get("status") == "passed":
        report["status"] = "inconclusive"
        report["limits"] = (
            str(report.get("limits", "")).rstrip()
            + " Run provenance was incomplete or changed during the experiment."
        ).strip()
    write_run_json(run_dir, Path("research-two-drone-smoke.json"), report)
    return report, 0 if stable else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--capture-exit-code", required=True, type=int)
    args = parser.parse_args()
    try:
        _, status = finalize(args.run_dir, args.capture_exit_code)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"could not finalize two-drone smoke provenance: {error}") from error
    return status


if __name__ == "__main__":
    raise SystemExit(main())
