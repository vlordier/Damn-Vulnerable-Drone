#!/usr/bin/env python3
"""Validate coverage from every legacy attack guide to native Antoine scenarios."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CROSSWALK_PATH = ROOT / "research" / "legacy-scenario-crosswalk.json"
CATALOG_PATH = ROOT / "research" / "scenario-catalog.json"
ATTACK_ROOT = ROOT / "simulator" / "mgmt" / "templates" / "pages" / "attacks"
STATUSES = {"adapted", "partial", "unavailable"}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON contract: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"JSON contract must be an object: {path}")
    return value


def legacy_sources() -> set[str]:
    return {
        path.relative_to(ATTACK_ROOT).as_posix()
        for path in ATTACK_ROOT.glob("*/*.yaml")
        if path.parent.name != "antoine" and not path.is_symlink()
    }


def validate() -> tuple[int, Counter[str]]:
    crosswalk = load_json(CROSSWALK_PATH)
    catalog = load_json(CATALOG_PATH)
    if crosswalk.get("contract") != "damn-vulnerable-drone.legacy-antoine-crosswalk":
        raise ValueError("unsupported legacy-to-Antoine crosswalk contract")
    scenarios = crosswalk.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("crosswalk scenarios must be a non-empty array")
    catalog_scenarios = catalog.get("scenarios")
    if not isinstance(catalog_scenarios, list):
        raise ValueError("native scenario catalog has no scenarios array")
    native_ids = {
        item.get("id")
        for item in catalog_scenarios
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

    seen: set[str] = set()
    counts: Counter[str] = Counter()
    for row in scenarios:
        if not isinstance(row, dict):
            raise ValueError("each crosswalk scenario must be an object")
        source = row.get("source")
        if (
            not isinstance(source, str)
            or PurePosixPath(source).is_absolute()
            or ".." in PurePosixPath(source).parts
        ):
            raise ValueError(f"invalid legacy source path: {source!r}")
        if source in seen:
            raise ValueError(f"duplicate legacy scenario mapping: {source}")
        seen.add(source)
        source_path = ATTACK_ROOT / source
        if source_path.is_symlink() or not source_path.is_file() or source not in legacy_sources():
            raise ValueError(f"crosswalk references a missing or non-legacy guide: {source}")
        try:
            guide = yaml.safe_load(source_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"cannot parse legacy guide: {source}") from error
        if not isinstance(guide, dict) or row.get("title") != guide.get("title"):
            raise ValueError(f"title does not match legacy guide: {source}")
        status = row.get("status")
        if status not in STATUSES:
            raise ValueError(f"unsupported native adaptation status for {source}: {status!r}")
        counts[status] += 1
        adaptation = row.get("adaptation")
        gap = row.get("gap")
        if not isinstance(adaptation, str) or not adaptation.strip():
            raise ValueError(f"crosswalk has no adaptation description: {source}")
        if status != "adapted" and (not isinstance(gap, str) or not gap.strip()):
            raise ValueError(f"partial/unavailable mapping must explain its gap: {source}")
        mapped_ids = row.get("nativeScenarioIds")
        if not isinstance(mapped_ids, list) or any(
            not isinstance(item, str) for item in mapped_ids
        ):
            raise ValueError(f"nativeScenarioIds must be an array of strings: {source}")
        if len(mapped_ids) != len(set(mapped_ids)):
            raise ValueError(f"duplicate native scenario ID in mapping: {source}")
        unknown = set(mapped_ids) - native_ids
        if unknown:
            raise ValueError(f"unknown native scenario IDs for {source}: {sorted(unknown)}")
        if status == "adapted" and not mapped_ids:
            raise ValueError(f"adapted scenario needs a native catalog mapping: {source}")

    expected = legacy_sources()
    missing, extra = expected - seen, seen - expected
    if missing or extra:
        raise ValueError(
            f"legacy guide coverage mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
        )
    adaptation_doc = (ROOT / "research" / "antoine-scenario-adaptation.md").read_text(
        encoding="utf-8"
    )
    crosswalk_section = adaptation_doc.split("## Scenario crosswalk", 1)[1].split(
        "## Priority experiments", 1
    )[0]
    document_titles = {
        columns[0].strip()
        for line in crosswalk_section.splitlines()
        if line.startswith("|")
        for columns in [line.strip("| ").split("|")]
        if len(columns) == 3 and columns[0].strip() not in {"Existing scenario", "---"}
    }
    contract_titles = {row["title"] for row in scenarios}
    if document_titles != contract_titles:
        raise ValueError(
            "scenario crosswalk Markdown and JSON differ; "
            f"missing={sorted(contract_titles - document_titles)}, "
            f"extra={sorted(document_titles - contract_titles)}"
        )
    return len(scenarios), counts


def main() -> int:
    try:
        count, statuses = validate()
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(
        f"Validated {count} legacy guides against the Antoine catalog: "
        f"{statuses['adapted']} adapted, {statuses['partial']} partial, "
        f"{statuses['unavailable']} unavailable."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
