#!/usr/bin/env python3
"""Create an isolated, bounded multi-X500 copy of Antoine's proof world."""

from __future__ import annotations

import argparse
import copy
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--agents", default="isr,fpv-a")
    parser.add_argument("--spacing-m", type=float, default=8.0)
    args = parser.parse_args()

    agents = args.agents.split(",")
    allowed_agents = ("isr", "fpv-a", "fpv-b", "fpv-c", "fpv-d")
    if (
        not 2 <= len(agents) <= 5
        or len(set(agents)) != len(agents)
        or agents != list(allowed_agents[: len(agents)])
        or any(not re.fullmatch(r"[a-z][a-z0-9-]{0,15}", agent) for agent in agents)
    ):
        raise SystemExit("agents must be an ordered unique roster from isr,fpv-a,fpv-b,fpv-c,fpv-d")
    if not math.isfinite(args.spacing_m) or not 2 <= args.spacing_m <= 20:
        raise SystemExit("spacing must be finite and between 2 and 20 metres")

    tree = ET.parse(args.source)
    world = tree.getroot().find("world")
    if world is None or world.get("name") != "antoine_gz_flight_proof":
        raise SystemExit("unexpected source world; refusing to generate a runtime overlay")
    includes = [
        node
        for node in world.findall("include")
        if node.findtext("name") == "isr" and node.findtext("uri") == "model://x500"
    ]
    if len(includes) != 1:
        raise SystemExit("expected exactly one isr X500 include in the source world")

    for index, agent in enumerate(agents[1:], start=1):
        vehicle = copy.deepcopy(includes[0])
        vehicle.find("name").text = agent
        pose = vehicle.find("pose")
        if pose is None:
            pose = ET.SubElement(vehicle, "pose")
        pose.text = f"{index * args.spacing_m:g} 0 8.0 0 0 0"
        world.append(vehicle)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(args.output, encoding="utf-8", xml_declaration=True)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
