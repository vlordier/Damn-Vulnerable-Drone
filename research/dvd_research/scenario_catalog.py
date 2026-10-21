"""Load and validate the Antoine scenario catalog for every consumer."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

CATALOG_CONTRACT = "damn-vulnerable-drone.antoine-scenario-catalog"
SCENARIO_STATUSES = {"active", "partial", "planned", "unavailable"}
GATE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
RUNNER_ARGUMENTS = {
    "research/run-antoine-scenario.sh": {"waypoint-injection", "sequence-poisoning"},
    "research/run-antoine-sensor-tampering.sh": {
        "signature-mutation",
        "duplicate-sequence",
        "out-of-order-sequence",
        "stale-sample",
        "future-sample",
    },
    "research/run-antoine-two-drone-flight.sh": {
        "ANTOINE_SWARM_FAULT=state-stream-timing",
        "ANTOINE_SWARM_FAULT=state-stream-partition",
        "ANTOINE_SWARM_FAULT=command-rate",
        "ANTOINE_SWARM_FAULT=controller-state-blackhole",
        "ANTOINE_SWARM_FAULT=cross-vehicle-authorization",
        "ANTOINE_SWARM_AUTH_MUTATION=wrong-callsign",
        "ANTOINE_SWARM_AUTH_MUTATION=wrong-task",
        "ANTOINE_SWARM_AUTH_MUTATION=wrong-target",
        "ANTOINE_SWARM_AUTH_MUTATION=stale-run",
        "ANTOINE_SWARM_FAULT_MAX_MESSAGES=1",
        "ANTOINE_SWARM_SIZE=3",
        "ANTOINE_SWARM_SIZE=5",
        "ANTOINE_SWARM_BEHAVIOR=column",
        "ANTOINE_SWARM_BEHAVIOR=vee",
        "ANTOINE_SWARM_BEHAVIOR=rendezvous",
        "ANTOINE_SWARM_BEHAVIOR=split",
        "ANTOINE_SWARM_MAVLINK=1",
    },
}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def load_scenario_catalog(
    catalog_path: Path,
    project_root: Path,
    guide_root: Path,
) -> list[dict[str, Any]]:
    """Return validated catalog rows; raise ValueError on any catalog drift."""
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read scenario catalog: {catalog_path}") from error
    if not isinstance(catalog, dict) or catalog.get("contract") != CATALOG_CONTRACT:
        raise ValueError("unsupported Antoine scenario catalog")
    scenarios = catalog.get("scenarios")
    if not isinstance(scenarios, list) or not all(isinstance(item, dict) for item in scenarios):
        raise ValueError("invalid Antoine scenario catalog entries")

    root = project_root.resolve()
    guide_root = guide_root.resolve()
    ids: set[str] = set()
    guides: set[str] = set()
    valid: list[dict[str, Any]] = []
    for item in scenarios:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip() or item_id in ids:
            raise ValueError("scenario IDs must be unique non-empty strings")
        ids.add(item_id)
        if not isinstance(item.get("label"), str) or not item["label"].strip():
            raise ValueError(f"scenario {item_id} has no label")
        if item.get("status") not in SCENARIO_STATUSES:
            raise ValueError(f"scenario {item_id} has unsupported status")

        gates = item.get("evidenceGates")
        if (
            not isinstance(gates, list)
            or not gates
            or any(not isinstance(gate, str) or not GATE_NAME.fullmatch(gate) for gate in gates)
            or len(set(gates)) != len(gates)
        ):
            raise ValueError(f"scenario {item_id} has invalid evidence gates")

        guide = item.get("guide")
        if guide is not None:
            if (
                not isinstance(guide, str)
                or Path(guide).name != guide
                or Path(guide).suffix not in {".yaml", ".yml"}
                or guide in guides
            ):
                raise ValueError(f"scenario {item_id} has an invalid or duplicate guide")
            guide_path = guide_root / guide
            if guide_path.is_symlink() or not guide_path.is_file():
                raise ValueError(f"scenario {item_id} references a missing or linked guide")
            guides.add(guide)

        runner = item.get("runner")
        if runner is not None:
            if not isinstance(runner, str) or not runner.strip():
                raise ValueError(f"scenario {item_id} has an invalid runner command")
            try:
                command = shlex.split(runner)
            except ValueError as error:
                raise ValueError(f"scenario {item_id} has an invalid runner command") from error
            if len(command) != 1:
                raise ValueError(f"scenario {item_id} runner must name one script")
            script = command[0]
            if not script.startswith("research/") or ".." in Path(script).parts:
                raise ValueError(f"scenario {item_id} runner must stay in research/")
            runner_path = (root / script).resolve()
            if not _is_within(runner_path, root / "research"):
                raise ValueError(f"scenario {item_id} runner escapes the repository")
            runner_available = runner_path.is_file() and os.access(runner_path, os.X_OK)
            if item["status"] == "active" and not runner_available:
                raise ValueError(f"active scenario {item_id} has no executable runner")
            if not runner_available:
                raise ValueError(f"scenario {item_id} references an unavailable runner")
            arguments = item.get("runnerArguments", [])
            allowed_arguments = RUNNER_ARGUMENTS.get(script, set())
            if (
                not isinstance(arguments, list)
                or any(
                    not isinstance(argument, str) or argument not in allowed_arguments
                    for argument in arguments
                )
                or len(arguments) != len(set(arguments))
            ):
                raise ValueError(f"scenario {item_id} has invalid runner arguments")
        elif item.get("runnerArguments"):
            raise ValueError(f"scenario {item_id} declares arguments without a runner")
        elif item["status"] == "active":
            raise ValueError(f"active scenario {item_id} has no runner command")

        valid.append(dict(item))

    actual_guides = {
        path.name
        for path in (*guide_root.glob("*.yaml"), *guide_root.glob("*.yml"))
        if path.is_file() and not path.is_symlink()
    }
    if guides != actual_guides:
        missing = sorted(actual_guides - guides)
        absent = sorted(guides - actual_guides)
        raise ValueError(
            f"catalog guide coverage differs (unregistered={missing}, missing={absent})"
        )
    return valid
