#!/usr/bin/env python3
"""Build a run-local camera rehearsal world from the Antoine proof world."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

PHYSICS_PLUGIN = '<plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>'
SCENE_PLUGIN = (
    '<plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>'
)
CAMERA_SYSTEMS = """    <spherical_coordinates>
      <surface_model>EARTH_WGS84</surface_model>
      <world_frame_orientation>ENU</world_frame_orientation>
      <latitude_deg>48.0002</latitude_deg>
      <longitude_deg>0.3564</longitude_deg>
      <elevation>82</elevation>
    </spherical_coordinates>
    <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu"/>
    <plugin filename="gz-sim-navsat-system" name="gz::sim::systems::NavSat"/>
    <plugin filename="gz-sim-air-pressure-system" name="gz::sim::systems::AirPressure"/>
    <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors"><render_engine>ogre2</render_engine></plugin>"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_world", type=Path)
    parser.add_argument("x500_model", type=Path)
    parser.add_argument("output_world", type=Path)
    args = parser.parse_args()
    if args.source_world.is_symlink() or args.x500_model.is_symlink():
        raise SystemExit("camera-world inputs must be regular, non-symlink files")
    source = args.source_world.resolve(strict=True)
    model = args.x500_model.resolve(strict=True)
    output = args.output_world.resolve()
    text = source.read_text(encoding="utf-8")
    model_text = model.read_text(encoding="utf-8")
    if text.count(PHYSICS_PLUGIN) != 1 or text.count(SCENE_PLUGIN) != 1:
        raise SystemExit(
            "Antoine proof-world anchors changed; refusing an unreviewed camera-world patch"
        )
    if '<world name="antoine_gz_flight_proof">' not in text or "<name>isr</name>" not in text:
        raise SystemExit("source world does not declare the expected Antoine camera model")
    if '<sensor name="camera" type="camera">' not in model_text:
        raise SystemExit("X500 model no longer declares the expected camera sensor")
    text = text.replace(
        PHYSICS_PLUGIN,
        '<plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics">'
        "<engine><filename>gz-physics-bullet-featherstone-plugin</filename></engine></plugin>",
        1,
    )
    text = text.replace(SCENE_PLUGIN, SCENE_PLUGIN + "\n" + CAMERA_SYSTEMS, 1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    metadata = {
        "contract": "damn-vulnerable-drone.antoine-camera-world",
        "version": "1.0.0",
        "sourceWorldSha256": sha256(source),
        "x500ModelSha256": sha256(model),
        "cameraWorldSha256": sha256(output),
        "sourceModified": False,
        "cameraSystemEnabled": True,
        "flightControllerStarted": False,
        "worldName": "antoine_gz_flight_proof",
        "modelName": "isr",
    }
    output.with_name("camera-world.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
