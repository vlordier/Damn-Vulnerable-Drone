#!/usr/bin/env python3
"""Record source revisions and input digests for one local Antoine research run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".lock",
    ".py",
    ".rs",
    ".sh",
    ".sdf",
    ".toml",
    ".xml",
    ".yaml",
    ".yml",
}
GENERATED_OR_DOCUMENTATION_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "docs",
    "documentation",
    "node_modules",
    "target",
}
GENERATED_SIM_ASSET_PATHS = {"ws", "build", "install", "log", "logs", "__pycache__"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_value(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def source_tree(name: str, root: Path) -> dict[str, Any]:
    top = git_value(root, "rev-parse", "--show-toplevel")
    if top is None:
        return {"name": name, "available": False}
    root = Path(top)
    revision = git_value(root, "rev-parse", "HEAD")
    status = git_value(root, "status", "--porcelain=v1", "--untracked-files=all") or ""
    qualification_status = status
    ignored_runtime_paths: list[str] = []
    if name == "gazebo_sim_assets":
        entries = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            check=False,
            capture_output=True,
        )
        if entries.returncode == 0:
            relevant_records = []
            for record in entries.stdout.split(b"\0"):
                if not record:
                    continue
                path = record[3:].decode("utf-8", errors="surrogateescape")
                parts = Path(path).parts
                if parts and parts[0] in GENERATED_SIM_ASSET_PATHS:
                    ignored_runtime_paths.append(path)
                else:
                    relevant_records.append(record)
            qualification_status = "\0".join(
                record.decode("utf-8", errors="surrogateescape") for record in relevant_records
            )
    dirty = bytearray(status.encode("utf-8"))
    try:
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--binary", "HEAD"],
            check=False,
            capture_output=True,
        )
        if diff.returncode == 0:
            dirty.extend(diff.stdout)
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
            check=False,
            capture_output=True,
        )
        if untracked.returncode == 0:
            for raw_path in untracked.stdout.split(b"\0"):
                if not raw_path:
                    continue
                relative = Path(raw_path.decode("utf-8", errors="surrogateescape"))
                candidate = root / relative
                dirty.extend(raw_path + b"\0")
                if candidate.is_file() and not candidate.is_symlink():
                    dirty.extend(bytes.fromhex(sha256_file(candidate)))
                else:
                    dirty.extend(b"unreadable-or-symlink")
    except OSError:
        dirty.extend(b"git-diff-unavailable")
    source_digest = hashlib.sha256()
    source_files = (
        git_value(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        if name in {"dvd_research", "antoine", "gazebo_sim_assets"}
        else None
    )
    source_file_count = 0
    if source_files is not None:
        for raw_path in sorted(path for path in source_files.split("\0") if path):
            relative = Path(raw_path)
            if (
                relative.suffix.lower() not in SOURCE_SUFFIXES
                or GENERATED_OR_DOCUMENTATION_DIRS.intersection(relative.parts)
                or (
                    name == "gazebo_sim_assets"
                    and GENERATED_SIM_ASSET_PATHS.intersection(relative.parts)
                )
            ):
                continue
            if name == "antoine" and not (
                relative.parts[0] in {"sitl", "runtime", "scripts"}
                or relative.name in {"Cargo.toml", "Cargo.lock"}
            ):
                continue
            candidate = root / relative
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                digest = bytes.fromhex(sha256_file(candidate))
            except OSError:
                continue
            source_digest.update(raw_path.encode("utf-8", errors="surrogateescape"))
            source_digest.update(b"\0")
            source_digest.update(digest)
            source_file_count += 1
    return {
        "name": name,
        "available": revision is not None,
        "revision": revision,
        "worktreeDirty": bool(status),
        "worktreeStatusSha256": hashlib.sha256(dirty).hexdigest(),
        "qualificationStatusSha256": hashlib.sha256(
            qualification_status.encode("utf-8")
        ).hexdigest(),
        "ignoredGeneratedPathCount": len(ignored_runtime_paths),
        "changedPathCount": sum(bool(line.strip()) for line in status.splitlines()),
        "sourceContentSha256": source_digest.hexdigest() if source_files is not None else None,
        "sourceFileCount": source_file_count if source_files is not None else None,
    }


def source_tree_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Compare qualification-relevant source while retaining full dirty state for audit."""
    if before.get("available") is not True or after.get("available") is not True:
        return True
    if before.get("revision") != after.get("revision"):
        return True
    if (
        before.get("sourceContentSha256") is not None
        and after.get("sourceContentSha256") is not None
    ):
        return before.get("sourceContentSha256") != after.get("sourceContentSha256")
    return before.get("qualificationStatusSha256", before.get("worktreeStatusSha256")) != after.get(
        "qualificationStatusSha256", after.get("worktreeStatusSha256")
    )


def changed_source_tree_names(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[str]:
    """Return changed source trees, excluding C2's audit-only checkout state.

    The profile executes one exact C2 bridge script and its requirements file;
    both are separately hashed inputs. Other changes in that shared checkout
    are retained in the raw tree evidence but cannot affect this run's bridge.
    """
    return sorted(
        name
        for name in set(before) | set(after)
        if name not in {"antoine", "c2_bridge_source", "gazebo_sim_assets"}
        and source_tree_changed(before.get(name, {}), after.get(name, {}))
    )


def changed_input_names(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[str]:
    return sorted(
        name
        for name in set(before) | set(after)
        if before.get(name, {}).get("available") is not True
        or after.get(name, {}).get("available") is not True
        or before.get(name, {}).get("sha256") != after.get(name, {}).get("sha256")
    )


def input_digest(name: str, path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        return {"name": name, "available": False}
    return {
        "name": name,
        "available": True,
        "sizeBytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def input_tree_digest(name: str, root: Path) -> dict[str, Any]:
    if root.is_symlink() or not root.is_dir():
        return {"name": name, "available": False}
    digest = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*")):
        if name == "antoine_build_source_snapshot" and {
            ".git",
            ".cargo-target",
            "target",
            "__pycache__",
        }.intersection(path.relative_to(root).parts):
            continue
        if path.is_symlink():
            return {"name": name, "available": False, "reason": "symlink-in-tree"}
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        try:
            digest.update(bytes.fromhex(sha256_file(path)))
        except OSError:
            return {"name": name, "available": False, "reason": "unreadable-file"}
        count += 1
    return {
        "name": name,
        "available": count > 0,
        "fileCount": count,
        "sha256": digest.hexdigest(),
    }


def command_version(name: str, command: list[str]) -> str | None:
    executable = shutil.which(name)
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, *command],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or result.stderr).strip()[:1000]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--camera-only", action="store_true")
    parser.add_argument("--phase", choices=("pre", "post"), default="post")
    parser.add_argument("--run-id")
    parser.add_argument("--runtime-world", type=Path)
    parser.add_argument("--source-sim", type=Path)
    parser.add_argument("--input-world", type=Path)
    parser.add_argument("--input-x500-model", type=Path)
    parser.add_argument("--input-models-dir", type=Path)
    parser.add_argument("--input-snapshot-receipt", type=Path)
    parser.add_argument("--input-antoine-source-dir", type=Path)
    parser.add_argument("--input-antoine-snapshot-receipt", type=Path)
    parser.add_argument("--input-applier", type=Path)
    parser.add_argument("--input-forwarder", type=Path)
    parser.add_argument("--input-forwarder-build", type=Path)
    parser.add_argument("--input-bridge", type=Path)
    parser.add_argument("--input-build-receipt", type=Path)
    parser.add_argument("--antoine-patch", type=Path)
    parser.add_argument("--input-gui-config", type=Path)
    parser.add_argument("--input-runtime-config", type=Path)
    parser.add_argument("--qgc-source", type=Path)
    parser.add_argument("--qgc-binary", type=Path)
    parser.add_argument("--qgc-config", type=Path)
    parser.add_argument("--profile-receipt", type=Path)
    parser.add_argument(
        "--exclude-qgroundcontrol",
        action="store_true",
        help="Do not bind QGC source or binary inputs for profiles that do not run QGC.",
    )
    parser.add_argument("--require-input", action="append", default=[], metavar="NAME")
    parser.add_argument("--profile-launcher", type=Path)
    parser.add_argument("--expected-env", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()

    sensor_forwarding_enabled = os.environ.get("ANTOINE_GZ_SENSOR_FORWARDING") == "1"
    if args.expected_env and args.expected_env.is_file():
        for line in args.expected_env.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key == "ANTOINE_GZ_SENSOR_FORWARDING":
                sensor_forwarding_enabled = value == "1"
                break

    antoine = Path(os.environ.get("ANTOINE_ROOT", Path.home() / "Work" / "antoine"))
    antoine_context = Path(os.environ.get("ANTOINE_SOURCE_CONTEXT", str(antoine)))
    sim = args.source_sim or Path(
        os.environ.get("AGR_SIM", Path.home() / "Work" / "durandal-gz-swarm-sim")
    )
    c2_scripts = Path(
        os.environ.get("AGR_C2_SCRIPTS", Path.home() / "Work" / "furia-c2" / "scripts")
    )
    qgc_root = args.qgc_source or Path(
        os.environ.get("ANTOINE_QGC_DIR", Path.home() / "Work" / "antoine-qgroundcontrol")
    )
    gz_bin = Path(
        os.environ.get(
            "GZ_BIN", Path.home() / "Work" / "gz-macOS" / "ws" / "install" / "bin" / "gz"
        )
    )
    # Capture each independent checkout once. The earlier broad Gazebo tree
    # scan included generated/untracked build state unrelated to this run.
    tree_roots = [
        ("dvd_research", PROJECT_ROOT),
        ("gazebo_distribution", Path.home() / "Work" / "gz-macOS"),
        ("gazebo_sim_assets", sim),
        ("c2_bridge_source", c2_scripts.parent),
    ]
    if not args.exclude_qgroundcontrol:
        tree_roots.append(("qgroundcontrol", qgc_root))
    source_world = args.input_world or sim / "worlds" / "antoine-gz-flight-proof.sdf"
    runtime_world = args.runtime_world or run_dir / "research-runtime-world.sdf"
    x500_model = args.input_x500_model or sim / "models" / "x500" / "model.sdf"
    if args.input_x500_model is None and args.source_sim and args.phase == "pre":
        x500_model = args.source_sim / "models" / "x500" / "model.sdf"
    input_paths = [
        ("gazebo_binary", gz_bin),
        ("source_world", source_world),
        ("runtime_world_overlay", runtime_world),
        ("x500_model", x500_model),
        ("research_python_interpreter", Path(sys.executable).resolve()),
    ]
    if args.input_models_dir:
        input_paths.append(("gazebo_model_assets", args.input_models_dir))
    if args.input_snapshot_receipt:
        input_paths.append(("gazebo_source_snapshot_receipt", args.input_snapshot_receipt))
    if args.input_antoine_source_dir:
        input_paths.append(("antoine_build_source_snapshot", args.input_antoine_source_dir))
    if args.input_antoine_snapshot_receipt:
        input_paths.append(("antoine_source_snapshot_receipt", args.input_antoine_snapshot_receipt))
    uv_executable = shutil.which("uv")
    if uv_executable:
        input_paths.append(("uv_executable", Path(uv_executable).resolve()))
    supervisor_binary = PROJECT_ROOT / "research" / ".process-supervisor"
    if supervisor_binary.is_file() and not supervisor_binary.is_symlink():
        input_paths.append(("process_supervisor_binary", supervisor_binary))
    if runtime_world == source_world:
        input_paths = [item for item in input_paths if item[0] != "runtime_world_overlay"]
    # Bind every source file in the local harness, not a hand-maintained list
    # that can silently omit a newly added attack, summarizer, or contract.
    source_suffixes = SOURCE_SUFFIXES | {".json"}
    for path in sorted((PROJECT_ROOT / "research").rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or any(
                part in {".venv", "__pycache__"} or part.endswith(".egg-info")
                for part in path.parts
            )
        ):
            continue
        relative = path.relative_to(PROJECT_ROOT / "research")
        if relative.suffix.lower() not in source_suffixes:
            continue
        input_paths.append((f"research_source:{relative.as_posix()}", path))
    for path in sorted((PROJECT_ROOT / "scripts").glob("*.sh")):
        if path.is_file() and not path.is_symlink():
            input_paths.append((f"launcher_source:{path.name}", path))
    if not args.camera_only:
        # The C2 repository creates short-lived Python cache files while its
        # bridge runs. Hash its exact executed entrypoint below instead of
        # treating those unrelated generated files as source changes.
        tree_roots.append(("antoine", antoine_context))
        antoine_target = Path(
            os.environ.get("ANTOINE_GZ_CARGO_TARGET_DIR", antoine / ".cargo-target")
        )
        antoine_binary_dir = Path(
            os.environ.get("ANTOINE_GZ_PREBUILT_BIN_DIR", antoine_target / "debug")
        )
        input_paths.extend(
            [
                ("antoine_flight_proof_launcher", antoine / "scripts" / "gz_flight_proof.sh"),
                (
                    "antoine_nats_controller",
                    antoine / "sitl" / "antoine-sitl" / "src" / "bin" / "antoine_sitl_nats.rs",
                ),
                (
                    "antoine_local_authority",
                    antoine
                    / "sitl"
                    / "antoine-sitl"
                    / "src"
                    / "bin"
                    / "antoine_gz_local_authority.rs",
                ),
                ("antoine_sitl_manifest", antoine / "sitl" / "antoine-sitl" / "Cargo.toml"),
                ("antoine_sitl_lockfile", antoine / "sitl" / "antoine-sitl" / "Cargo.lock"),
                (
                    "antoine_boundary_manifest",
                    antoine / "runtime" / "antoine-boundary" / "Cargo.toml",
                ),
                (
                    "antoine_boundary_lockfile",
                    antoine / "runtime" / "antoine-boundary" / "Cargo.lock",
                ),
                ("antoine_sitl_binary", antoine_binary_dir / "antoine_sitl_nats"),
                (
                    "antoine_local_authority_binary",
                    antoine_binary_dir / "antoine_gz_local_authority",
                ),
                ("antoine_boundary_binary", antoine_binary_dir / "antoine-boundary-service"),
                ("c2_gazebo_bridge", c2_scripts / "c2-gz-sitl-bridge.py"),
                ("c2_python_requirements", c2_scripts.parent / "requirements-gz-sitl.txt"),
            ]
        )
        if not args.exclude_qgroundcontrol:
            input_paths.append(
                (
                    "qgroundcontrol_binary",
                    args.qgc_binary
                    or qgc_root
                    / "build"
                    / os.environ.get("QGC_CONFIG", "Debug")
                    / "QGroundControl.app"
                    / "Contents"
                    / "MacOS"
                    / "QGroundControl",
                )
            )
        if args.input_bridge:
            input_paths.append(("mavlink_nats_bridge_binary", args.input_bridge))
        if args.input_build_receipt:
            input_paths.append(("antoine_build_receipt", args.input_build_receipt))
        if args.antoine_patch:
            input_paths.append(("antoine_battery_patch", args.antoine_patch))
        applier = args.input_applier or sim / "tools" / "gz-sitl-applier" / "gz-sitl-applier"
        input_paths.append(("gazebo_applier", applier))
    sensor_overlay = (
        str(args.profile_launcher)
        if args.profile_launcher
        else os.environ.get("ANTOINE_GZ_SENSOR_PROFILE_LAUNCHER")
    )
    if sensor_overlay:
        input_paths.append(("antoine_sensor_profile_launcher", Path(sensor_overlay)))
    if args.input_gui_config:
        input_paths.append(("gazebo_gui_config", args.input_gui_config))
    if args.input_runtime_config:
        input_paths.append(("runtime_profile_config", args.input_runtime_config))
    if args.qgc_config:
        input_paths.append(("qgroundcontrol_runtime_config", args.qgc_config))
    if args.profile_receipt:
        input_paths.append(("interactive_profile_receipt", args.profile_receipt))

    for name in ("cargo", "rustc", "nats-server"):
        executable = shutil.which(name)
        if executable:
            input_paths.append((f"{name}_executable", Path(executable).resolve()))

    gz_install_lib = gz_bin.parent.parent / "lib"
    physics_name = os.environ.get(
        "ANTOINE_GZ_PHYSICS_ENGINE", "gz-physics-bullet-featherstone-plugin"
    )
    physics_name = physics_name.removeprefix("gz-physics-")
    plugins = sorted(
        path
        for path in gz_install_lib.glob(f"gz-physics-*/engine-plugins/*{physics_name}*")
        if path.is_file() and not path.is_symlink()
    )
    if not plugins:
        plugins = sorted(
            path
            for path in gz_install_lib.glob(f"lib*{physics_name}*.dylib")
            if path.is_file() and not path.is_symlink()
        )
    if plugins:
        input_paths.append(("gazebo_physics_engine_plugin", plugins[0]))
    sensor_forwarder = (
        args.input_forwarder or os.environ.get("FURIA_GZ_SENSOR_FORWARDER_BIN")
        if sensor_forwarding_enabled
        else None
    )
    if sensor_forwarder:
        input_paths.append(("signed_sensor_forwarder", Path(sensor_forwarder)))
    if sensor_forwarding_enabled and args.input_forwarder_build:
        input_paths.append(("sensor_forwarder_build_script", args.input_forwarder_build))
        build_script = args.input_forwarder_build.resolve()
        if build_script.is_file():
            build_env = os.environ.copy()
            if sensor_forwarder:
                build_env["FURIA_GZ_NATS_BRIDGE_TARGET_DIR"] = str(
                    Path(sensor_forwarder).resolve().parent.parent
                )
            try:
                command = ["uv", "run", "--script", str(build_script), "verify"]
                receipt_result = subprocess.run(
                    command, capture_output=True, text=True, env=build_env, timeout=60
                )
                if receipt_result.returncode == 0:
                    receipt = json.loads(receipt_result.stdout)
                    receipt_payload = receipt.get("receipt") if isinstance(receipt, dict) else None
                    artifact = (
                        receipt_payload.get("artifact")
                        if isinstance(receipt_payload, dict)
                        else None
                    )
                    if isinstance(artifact, dict) and isinstance(artifact.get("path"), str):
                        receipt_path = Path(artifact["path"]).with_name(
                            "gz-nav-sensor-forwarder.build.json"
                        )
                        input_paths.append(("sensor_forwarder_build_receipt", receipt_path))
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
                pass
    if args.camera_only:
        render_prefix = Path(
            os.environ.get(
                "FURIA_GZ_RENDERING_PREFIX", PROJECT_ROOT / "runs" / "gz-rendering-ogre2-prefix"
            )
        )
        for name, path in (
            ("ogre2_rendering_core", render_prefix / "lib" / "libgz-rendering8.8.dylib"),
            ("ogre2_sensor_system", render_prefix / "lib" / "libgz-sim8-sensors-system.dylib"),
        ):
            input_paths.append((name, path))

    run_dir.mkdir(parents=True, exist_ok=True)
    profile_keys = (
        "ANTOINE_GZ_SENSOR_FORWARDING",
        "ANTOINE_GZ_CAMERA_RENDERING",
        "ANTOINE_GZ_INTERACTIVE_CONTROL",
        "ANTOINE_GZ_FLIGHT_GUI",
        "ANTOINE_GZ_FLIGHT_GROUND_START",
        "ANTOINE_GZ_FLIGHT_CONTROL_HZ",
        "ANTOINE_GZ_TORQUE_GAIN",
        "ANTOINE_GZ_ATTITUDE_RATE_GAIN",
        "ANTOINE_GZ_FLIGHT_STATE_HZ",
        "ANTOINE_GZ_FLIGHT_NATS_PORT",
        "ANTOINE_GZ_MAVLINK_CALLSIGN",
        "ANTOINE_GZ_PHYSICS_ENGINE",
        "FURIA_GZ_SENSOR_SIGNING_KEY_ID",
    )
    if args.expected_env and args.expected_env.is_file():
        recorded_env = {}
        for line in args.expected_env.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key:
                recorded_env[key] = value
                os.environ[key] = value
        provenance_expected_env = recorded_env
    else:
        provenance_expected_env = None

    if args.camera_only:
        for label in ("ogre2_rendering_core", "ogre2_sensor_system"):
            input_paths = [item for item in input_paths if item[0] != label]
        render_prefix = Path(
            os.environ.get(
                "FURIA_GZ_RENDERING_PREFIX", PROJECT_ROOT / "runs" / "gz-rendering-ogre2-prefix"
            )
        )
        for label, path in (
            ("ogre2_rendering_core", render_prefix / "lib" / "libgz-rendering8.8.dylib"),
            ("ogre2_sensor_system", render_prefix / "lib" / "libgz-sim8-sensors-system.dylib"),
        ):
            input_paths.append((label, path))

    provenance = {
        "contract": "damn-vulnerable-drone.antoine-source-provenance",
        "version": "1.0.0",
        "runId": args.run_id or run_dir.name,
        "phase": args.phase,
        "capturedAtUnixMs": time.time_ns() // 1_000_000,
        "scope": "source revisions and content digests for this local simulation profile",
        "profile": {key: os.environ[key] for key in profile_keys if key in os.environ},
        "expectedProfile": provenance_expected_env,
        "toolchainVersions": {
            "rustupToolchain": os.environ.get("RUSTUP_TOOLCHAIN"),
            "cargo": command_version("cargo", ["--version"]),
            "rustc": command_version("rustc", ["--version", "--verbose"]),
            "uv": command_version("uv", ["--version"]),
            "natsServer": command_version("nats-server", ["--version"]),
            "gazebo": command_version(str(gz_bin), ["--version"]),
        },
        "patchState": None,
        "sourceTrees": [source_tree(name, root) for name, root in tree_roots],
        "inputs": [
            input_tree_digest(name, path) if path.is_dir() else input_digest(name, path)
            for name, path in input_paths
        ],
        "limitations": [
            "Git revisions and dirty-state digests identify source checkout state; they do not attest to a trusted build.",
            "Binary digests identify the inspected local executable; runtime loading is not independently attested.",
        ],
    }
    if args.antoine_patch:
        patch_check = subprocess.run(
            [
                "git",
                "-C",
                str(antoine),
                "apply",
                "--reverse",
                "--check",
                str(args.antoine_patch),
            ],
            check=False,
            capture_output=True,
        )
        provenance["patchState"] = {
            "patchAvailable": args.antoine_patch.is_file() and not args.antoine_patch.is_symlink(),
            "patchSha256": (
                sha256_file(args.antoine_patch)
                if args.antoine_patch.is_file() and not args.antoine_patch.is_symlink()
                else None
            ),
            "applied": patch_check.returncode == 0,
        }
    unavailable_required = sorted(
        name
        for name in args.require_input
        if not any(
            item.get("name") == name and item.get("available") is True
            for item in provenance["inputs"]
        )
    )
    output = args.output or run_dir / (
        "research-provenance-pre.json" if args.phase == "pre" else "research-provenance.json"
    )
    output.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if unavailable_required:
        print(
            "Required provenance inputs are missing: " + ", ".join(unavailable_required),
            file=sys.stderr,
        )
        return 1
    if args.phase == "post":
        pre_path = run_dir / "research-provenance-pre.json"
        if pre_path.is_file():
            pre = json.loads(pre_path.read_text(encoding="utf-8"))
            pre_trees = {
                item.get("name"): item
                for item in pre.get("sourceTrees", [])
                if isinstance(item, dict)
            }
            post_trees = {
                item.get("name"): item
                for item in provenance.get("sourceTrees", [])
                if isinstance(item, dict)
            }
            pre_inputs = {
                item.get("name"): item for item in pre.get("inputs", []) if isinstance(item, dict)
            }
            post_inputs = {
                item.get("name"): item
                for item in provenance.get("inputs", [])
                if isinstance(item, dict)
            }
            changed_trees = changed_source_tree_names(pre_trees, post_trees)
            changed_inputs = changed_input_names(pre_inputs, post_inputs)
            pre_patch_state = pre.get("patchState")
            post_patch_state = provenance.get("patchState")
            patch_matches = (pre_patch_state is None and post_patch_state is None) or (
                pre_patch_state == post_patch_state
                and isinstance(post_patch_state, dict)
                and post_patch_state.get("applied") is True
            )
            comparison = {
                "contract": "damn-vulnerable-drone.antoine-provenance-comparison",
                "version": "1.0.0",
                "runId": run_dir.name,
                "preCapturedAtUnixMs": pre.get("capturedAtUnixMs"),
                "postCapturedAtUnixMs": provenance.get("capturedAtUnixMs"),
                "prePhaseValid": pre.get("phase") == "pre" and pre.get("runId") == run_dir.name,
                "postPhaseValid": provenance.get("phase") == "post"
                and provenance.get("runId") == run_dir.name,
                "profileMatches": pre.get("expectedProfile") == provenance.get("expectedProfile")
                and pre.get("expectedProfile") is not None,
                "toolchainMatches": pre.get("toolchainVersions")
                == provenance.get("toolchainVersions"),
                "patchMatches": patch_matches,
                "changedSourceTrees": changed_trees,
                "contextOnlySourceTrees": ["c2_bridge_source"],
                "changedInputs": changed_inputs,
                "stable": not changed_trees
                and not changed_inputs
                and pre.get("phase") == "pre"
                and pre.get("runId") == run_dir.name
                and provenance.get("phase") == "post"
                and provenance.get("runId") == run_dir.name
                and pre.get("expectedProfile") == provenance.get("expectedProfile")
                and pre.get("expectedProfile") is not None
                and pre.get("toolchainVersions") == provenance.get("toolchainVersions")
                and patch_matches,
                "limitations": [
                    "Source-tree stability hashes code, build manifests, and runtime configuration while retaining full dirty-state digests as context; documentation-only changes do not invalidate a run.",
                    "This compares sampled source state; it does not attest to the binary actually loaded by the operating system.",
                ],
            }
            (run_dir / "research-provenance-comparison.json").write_text(
                json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
