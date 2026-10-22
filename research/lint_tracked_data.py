"""Parse tracked YAML and JSON files with the repository's host dependencies."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml


def tracked_files(pattern: str) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", pattern],
        check=True,
        capture_output=True,
    )
    return [Path(path.decode()) for path in result.stdout.split(b"\0") if path]


def main() -> None:
    yaml_files = sorted(set(tracked_files("*.yaml") + tracked_files("*.yml")))
    json_files = tracked_files("*.json")

    for path in yaml_files:
        with path.open(encoding="utf-8") as source:
            list(yaml.safe_load_all(source))
    for path in json_files:
        with path.open(encoding="utf-8") as source:
            json.load(source)

    print(f"Parsed {len(yaml_files)} tracked YAML and {len(json_files)} tracked JSON files")


if __name__ == "__main__":
    main()
