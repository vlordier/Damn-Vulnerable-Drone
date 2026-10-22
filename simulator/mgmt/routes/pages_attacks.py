# simulator/mgmt/routes/pages_attacks.py
import json
import os
import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

import yaml
from dvd_research.evidence_contract import (
    classify_measurements,
    verify_manifest,
    verify_signature,
)
from dvd_research.scenario_catalog import load_scenario_catalog
from flask import abort, current_app, render_template, send_file, url_for
from markupsafe import Markup

from . import bp


class _ScenarioMarkupSanitizer(HTMLParser):
    """Preserve simple guide formatting while escaping unknown markup."""

    ALLOWED_TAGS = {"a", "br", "code", "span", "table", "thead", "tbody", "tr", "th", "td"}
    VOID_TAGS = {"br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.ALLOWED_TAGS:
            rendered_attrs = "".join(
                f' {escape(name)}="{escape(value or "", quote=True)}"' for name, value in attrs
            )
            self.output.append(escape(f"<{tag}{rendered_attrs}>"))
            return

        safe_attrs = []
        for name, value in attrs:
            value = value or ""
            if tag == "a" and name == "href" and (
                value.startswith(("https://", "http://", "/attacks/"))
                and not value.startswith("//")
            ):
                safe_attrs.append(f' href="{escape(value, quote=True)}"')
            elif tag == "span" and name == "class" and value == "alert-warning":
                safe_attrs.append(' class="alert-warning"')
            elif tag == "table" and name == "border" and value.isdigit():
                safe_attrs.append(f' border="{value}"')
        self.output.append(f"<{tag}{''.join(safe_attrs)}>")

    def handle_endtag(self, tag):
        if tag in self.ALLOWED_TAGS and tag not in self.VOID_TAGS:
            self.output.append(f"</{tag}>")
        elif tag not in self.ALLOWED_TAGS:
            self.output.append(escape(f"</{tag}>"))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        self.output.append(escape(data))

    def handle_entityref(self, name):
        self.output.append(f"&amp;{escape(name)};")

    def handle_charref(self, name):
        self.output.append(f"&amp;#{escape(name)};")

    def handle_comment(self, data):
        return

    def handle_decl(self, decl):
        self.output.append(escape(f"<!{decl}>"))


@bp.app_template_filter("scenario_markup")
def scenario_markup(value):
    sanitizer = _ScenarioMarkupSanitizer()
    sanitizer.feed(str(value or ""))
    sanitizer.close()
    return Markup("".join(sanitizer.output))


@bp.app_template_filter("scenario_url")
def scenario_url(value):
    value = str(value or "")
    if value.startswith(("https://", "http://", "/attacks/")) and not value.startswith("//"):
        return value
    return "#"

def _attacks_base_dir() -> Path:
    # Allow override via config ATTACKS_DIR, else default to app/templates/pages/attacks
    default = Path(current_app.root_path) / "templates" / "pages" / "attacks"
    return Path(current_app.config.get("ATTACKS_DIR", default))

def load_yaml_files(dir_path: os.PathLike):
    p = Path(dir_path)
    if not p.exists():
        current_app.logger.warning("Attacks directory missing: %s", p)
        return []

    tactic = p.name
    items = []

    for yml in list(p.glob("*.yml")) + list(p.glob("*.yaml")):
        if yml.is_symlink() or not yml.is_file():
            current_app.logger.warning("Skipping non-regular attack scenario: %s", yml)
            continue
        try:
            with yml.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception as e:
            current_app.logger.exception("Failed to load %s: %s", yml, e)
            data = {}
        if not isinstance(data, dict):
            current_app.logger.warning("Skipping non-mapping attack scenario: %s", yml)
            continue

        raw_order = data.get("order", None)
        try:
            order = float(raw_order) if raw_order is not None else float("inf")
        except (TypeError, ValueError):
            order = float("inf")

        title = str(data.get("title", yml.stem))
        link = url_for("main.attack_scenario", tactic=tactic, filename=yml.name)

        items.append({
            "order": order,
            "title": title,
            "description": data.get("description", ""),
            "status": data.get("status", ""),
            "link": link,
            "_source_file": str(yml),
        })

    items.sort(key=lambda x: (x["order"], x["title"].lower()))
    return items


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _smoke_correlation_details(value) -> str:
    vehicles = _mapping(_mapping(value).get("vehicles"))
    details = []
    for callsign in ("drone-1", "drone-2"):
        item = _mapping(vehicles.get(callsign))
        if item.get("matched") is True:
            details.append(
                f"{callsign}: matched ({item.get('positionErrorM')} m, "
                f"{item.get('simulationTimeDeltaSeconds')} s)"
            )
        else:
            details.append(f"{callsign}: {item.get('reason', 'no matching state/truth sample')}")
    return "; ".join(details)


def _runner_command(scenario: dict) -> str:
    """Render env-style runner arguments before the script they configure."""
    script = f"./{scenario['runner']}"
    arguments = scenario.get("runnerArguments", [])
    if arguments and all(re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", argument) for argument in arguments):
        return " ".join([*arguments, script])
    return " ".join([script, *arguments])


def _research_dir() -> Path:
    configured = current_app.config.get("RESEARCH_DIR")
    if configured:
        return Path(configured)
    return Path(current_app.root_path).parents[1] / "research"


def _scenario_catalog() -> list[dict]:
    path = _research_dir() / "scenario-catalog.json"
    project_root = Path(current_app.root_path).parents[1]
    guide_root = Path(current_app.root_path) / "templates" / "pages" / "attacks" / "antoine"
    try:
        return load_scenario_catalog(path, project_root, guide_root)
    except ValueError as error:
        current_app.logger.error("Antoine scenario catalog rejected: %s", error)
        return []


def _scenario_catalog_by_guide() -> dict[str, dict]:
    return {
        item["guide"]: item
        for item in _scenario_catalog()
        if isinstance(item, dict) and isinstance(item.get("guide"), str)
    }


def _legacy_crosswalk_entries(catalog: list[dict]) -> list[dict]:
    """Resolve checked crosswalk IDs to safe guide links for the research UI."""
    path = _research_dir() / "legacy-scenario-crosswalk.json"
    if path.is_symlink() or not path.is_file():
        return []
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        current_app.logger.error("Legacy Antoine crosswalk rejected: %s", error)
        return []
    if not isinstance(contract, dict) or not isinstance(contract.get("scenarios"), list):
        current_app.logger.error("Legacy Antoine crosswalk has no scenario list")
        return []

    catalog_by_id = {item.get("id"): item for item in catalog if isinstance(item, dict)}
    guide_root = Path(current_app.root_path) / "templates" / "pages" / "attacks"
    rows = []
    for row in contract["scenarios"]:
        if not isinstance(row, dict):
            continue
        source = row.get("source")
        parts = PurePosixPath(source).parts if isinstance(source, str) else ()
        if len(parts) != 2 or parts[0] not in {
            "recon", "tampering", "injection", "dos", "exfiltration", "firmware"
        }:
            continue
        legacy_path = guide_root / parts[0] / parts[1]
        if legacy_path.is_symlink() or not legacy_path.is_file():
            continue
        mapped_scenarios = []
        native_ids = row.get("nativeScenarioIds")
        if isinstance(native_ids, list):
            for native_id in native_ids:
                native = catalog_by_id.get(native_id)
                if not native:
                    continue
                guide = native.get("guide")
                guide_url = None
                if isinstance(guide, str) and Path(guide).name == guide:
                    native_guide = guide_root / "antoine" / guide
                    if not native_guide.is_symlink() and native_guide.is_file():
                        guide_url = url_for(
                            "main.attack_scenario", tactic="antoine", filename=guide
                        )
                mapped_scenarios.append(
                    {
                        "label": native.get("label", native_id),
                        "status": native.get("status", "unknown"),
                        "guide_url": guide_url,
                        "runner": native.get("runner"),
                    }
                )
        rows.append(
            {
                "title": str(row.get("title", parts[1])),
                "source": source,
                "status": row.get("status", "unavailable"),
                "adaptation": str(row.get("adaptation", "")),
                "gap": row.get("gap"),
                "legacy_url": url_for(
                    "main.attack_scenario", tactic=parts[0], filename=parts[1]
                ),
                "native_scenarios": mapped_scenarios,
            }
        )
    return rows


def _research_evidence_dir() -> Path:
    configured = current_app.config.get("RESEARCH_EVIDENCE_DIR")
    if configured:
        return Path(configured)
    return Path(current_app.root_path).parents[1] / "build_logs" / "research" / "antoine-scenarios"


def _public_evidence_file(run_dir: Path, filename: str) -> Path | None:
    if run_dir.is_symlink() or not run_dir.is_dir():
        return None
    candidate = run_dir / filename
    if candidate.is_symlink() or not candidate.is_file():
        return None
    resolved_run = run_dir.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate.parent != resolved_run:
        return None
    return resolved_candidate

@bp.route("/attacks/all")
@bp.route("/attacks")
def attacks_index():
    base_dir = _attacks_base_dir()
    antoine_items = load_yaml_files(base_dir / "antoine")
    catalog = _scenario_catalog_by_guide()
    for item in antoine_items:
        guide = Path(item["_source_file"]).name
        catalog_entry = catalog.get(guide)
        if catalog_entry:
            item["status"] = catalog_entry.get("status", "unavailable")
            item["runner"] = catalog_entry.get("runner")
            item["scenario_id"] = catalog_entry.get("id")
        else:
            item["status"] = "unavailable"
            item["runner"] = None
    categories = {
        "Reconnaissance":     load_yaml_files(base_dir / "recon"),
        "Protocol Tampering": load_yaml_files(base_dir / "tampering"),
        "Denial of Service":  load_yaml_files(base_dir / "dos"),
        "Injection":          load_yaml_files(base_dir / "injection"),
        "Exfiltration":       load_yaml_files(base_dir / "exfiltration"),
        "Firmware Attacks":   load_yaml_files(base_dir / "firmware"),
        "Antoine / NATS":      antoine_items,
    }
    return render_template(
        "pages/attacks/list.html",
        section="attacks",
        sub_section="",
        current_page="attacks",
        categories=categories,
        LITE=current_app.config.get("LITE", False),  # ensure Lite flag is available
    )


@bp.route("/attacks/research")
def antoine_research_index():
    """Show the checked-in Antoine research status and local reproduction commands."""
    research_dir = _research_dir()
    findings_path = research_dir / "findings.md"
    evidence_root = _research_evidence_dir()
    evidence_runs = []
    scenario_coverage = []
    if evidence_root.is_dir() and not evidence_root.is_symlink():
        run_dirs = sorted(evidence_root.iterdir(), key=lambda path: path.name, reverse=True)
        valid_run_dirs = [
            path for path in run_dirs
            if not path.is_symlink()
            and path.is_dir()
            and re.fullmatch(r"antoine-[a-z0-9-]+-\d{8}T\d{6}Z-\d+", path.name)
        ][:100]
        for run_dir in valid_run_dirs:
            result_files = {
                name: _public_evidence_file(run_dir, name)
                for name in (
                    "research-result.json",
                    "research-baseline-summary.json",
                    "research-gps-tampering-summary.json",
                    "research-early-revoke.json",
                    "research-camera-observation.json",
                    "research-waypoint-replay-summary.json",
                    "research-sequence-poisoning-summary.json",
                    "research-sequence-poisoning.json",
                    "research-nats-observation.json",
                    "research-provenance.json",
                    "research-provenance-comparison.json",
                    "research-swarm-flight.json",
                    "research-mavlink-swarm.json",
                    "research-two-drone-smoke.json",
                )
                if _public_evidence_file(run_dir, name)
            }
            failure_path = _public_evidence_file(run_dir, "research-run-failure.txt")
            failure_record = {}
            if failure_path is not None:
                try:
                    failure_record = dict(
                        line.split("=", 1)
                        for line in failure_path.read_text(encoding="utf-8").splitlines()
                        if "=" in line
                    )
                except OSError:
                    failure_record = {}
            manifest_path = run_dir / "research-evidence-manifest.json"
            manifest_integrity_valid = False
            manifest_errors = ["manifest unavailable"]
            manifest_measurements = {}
            manifest = {}
            manifest_signature_status = "unsigned"
            if _public_evidence_file(run_dir, "research-evidence-manifest.json"):
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    manifest = {}
                if isinstance(manifest, dict) and isinstance(manifest.get("scenarioCoverage"), list):
                    scenario_coverage = manifest["scenarioCoverage"]
                manifest_integrity_valid, manifest_errors = verify_manifest(manifest_path)
                trusted_key = current_app.config.get("RESEARCH_TRUSTED_PUBLIC_KEY")
                manifest_signature_status, _ = verify_signature(
                    manifest_path, Path(trusted_key) if trusted_key else None
                )
                if (manifest_integrity_valid and isinstance(manifest, dict)
                        and manifest.get("runId") == run_dir.name):
                    manifest_measurements = _mapping(manifest.get("measurements"))
                else:
                    manifest_integrity_valid = False
                    manifest_errors = [*manifest_errors, "manifest runId does not match its directory"]
            public_evidence = _mapping(manifest).get("publicEvidence", [])
            public_evidence = public_evidence if isinstance(public_evidence, list) else []
            public_evidence_names = {
                item.get("name")
                for item in public_evidence
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            if (
                not manifest_integrity_valid
                or failure_record.get("runId") != run_dir.name
                or "research-run-failure.txt" not in public_evidence_names
            ):
                failure_record = {}
            qualification_status = (
                classify_measurements(manifest_measurements)
                if manifest_integrity_valid else "inconclusive"
            )
            if manifest_signature_status in {
                "invalid_signature", "manifest_digest_mismatch", "verification_unavailable",
                "trusted_key_unavailable", "untrusted_signer",
            }:
                qualification_status = "inconclusive"
            result_priority = (
                "research-gps-tampering-summary.json",
                "research-early-revoke.json",
                "research-baseline-summary.json",
                "research-result.json",
                "research-waypoint-replay-summary.json",
                "research-sequence-poisoning-summary.json",
                "research-nats-observation.json",
                "research-camera-observation.json",
                "research-two-drone-smoke.json",
                "research-swarm-flight.json",
            )
            result_name = next((name for name in result_priority if name in result_files), None)
            result = {}
            if result_name:
                try:
                    result = json.loads(result_files[result_name].read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    result = {}
                if not isinstance(result, dict):
                    result = {}
            if not result:
                result_name = None
                if not manifest_integrity_valid:
                    outcome = "Evidence manifest failed verification"
                elif manifest_signature_status in {
                    "invalid_signature",
                    "manifest_digest_mismatch",
                    "verification_unavailable",
                    "trusted_key_unavailable",
                    "untrusted_signer",
                }:
                    outcome = "Manifest signature invalid or signer untrusted"
                elif failure_record:
                    stage = failure_record.get("stage", "unknown")
                    exit_code = failure_record.get("exitCode", "unknown")
                    outcome = f"Run failed during {stage} (exit {exit_code})"
                else:
                    outcome = "Run started; result summary is missing"
                outcome_variant = "warning"
                attack = replay = gazebo = flight = comparison = sensor_flight = {}
                observed_count = 0
                flight_passed = None
                trajectory_url = None
            else:
                attack = _mapping(result.get("attack"))
                replay = _mapping(result.get("replay"))
                gazebo = _mapping(result.get("independentGazebo"))
                flight = _mapping(result.get("flightProof"))
                comparison = _mapping(result.get("comparison"))
                sensor_flight = _mapping(result.get("flightProof"))
                subjects = result.get("subjects", [])
                observed_count = sum(
                    count
                    for subject in subjects
                    if isinstance(subject, dict)
                    for count in [subject.get("capturedMessages", 0)]
                    if type(count) is int and count >= 0
                ) if isinstance(subjects, list) else 0
                if not manifest_integrity_valid:
                    outcome = "Evidence manifest failed verification"
                    outcome_variant = "warning"
                elif manifest_signature_status in {
                    "invalid_signature", "manifest_digest_mismatch", "verification_unavailable",
                    "trusted_key_unavailable", "untrusted_signer",
                }:
                    outcome = "Manifest signature invalid or signer untrusted"
                    outcome_variant = "warning"
                elif qualification_status == "qualified_baseline":
                    outcome = "Valid control: all baseline gates passed"
                    outcome_variant = "success"
                elif qualification_status == "qualified_attack_observation":
                    outcome = "Takeoff denial measured with independent evidence"
                    outcome_variant = "success"
                elif qualification_status == "qualified_signature_mutation_rejection":
                    outcome = "Altered GPS envelope not accepted; flight gates passed"
                    outcome_variant = "success"
                elif qualification_status == "qualified_replay_rejection":
                    outcome = "Replay rejected; independent flight gates passed"
                    outcome_variant = "success"
                elif qualification_status == "qualified_local_revoke_stop_observation":
                    outcome = f"Local revoke stopped actuators in {result.get('revokeToNeutralMs')} ms"
                    outcome_variant = "info"
                elif result_name == "research-two-drone-smoke.json" and (
                    qualification_status == "observation_only" and result.get("status") == "passed"
                ):
                    outcome = "Two-drone NATS/Gazebo wiring smoke passed; no controllers or flight"
                    outcome_variant = "info"
                elif qualification_status == "observation_only":
                    outcome = "Bounded local observation captured; no control authority claim"
                    outcome_variant = "info"
                elif qualification_status == "qualified_swarm_flight":
                    fault_status = _mapping(result.get("faultObservation")).get("status")
                    suffix = "; bounded state fault observed" if fault_status == "observed" else ""
                    behavior = result.get("behavior")
                    behavior_suffix = f" ({behavior})" if behavior else ""
                    mavlink_suffix = (
                        "; MAVLink telemetry fan-out passed"
                        if result.get("mavlinkSwarmPassed") is True
                        else ""
                    )
                    outcome = (
                        f"{result.get('vehicleCount')}-drone scripted flight"
                        f"{behavior_suffix} and pairwise separation passed"
                        f"{mavlink_suffix}{suffix}"
                    )
                    outcome_variant = "success"
                elif qualification_status == "failed_swarm_flight":
                    outcome = "Measured swarm flight or probe failure; inspect the Gazebo and controller evidence"
                    outcome_variant = "warning"
                elif result_name == "research-two-drone-smoke.json":
                    outcome = "Two-drone smoke inconclusive; inspect NATS/Gazebo state correlation"
                    outcome_variant = "warning"
                else:
                    outcome = "Evidence inconclusive or qualification gates missing"
                    outcome_variant = "warning"
                flight_passed = gazebo.get("localFlightPassed")
                if flight_passed is None:
                    flight_passed = flight.get("localFlightPassed")
                if flight_passed is None:
                    flight_passed = sensor_flight.get("localFlightPassed")
                if flight_passed is None:
                    flight_passed = _mapping(result.get("gates")).get(
                        "localFlightPassed", result.get("allFlightsPassed")
                    )
                trajectory_url = None
                if _public_evidence_file(run_dir, "research-swarm-trajectory.svg"):
                    trajectory_url = url_for(
                        "main.antoine_research_evidence",
                        run_id=run_dir.name,
                        filename="research-swarm-trajectory.svg",
                    )
            evidence_runs.append({
                    "run_id": run_dir.name,
                    "scenario": result.get(
                        "scenario",
                        result_name[len("research-"):-len(".json")].replace("-", " ").title()
                        if result_name else run_dir.name.split("-")[1].title(),
                    ),
                    "result_name": result_name,
                    "download_url": url_for("main.antoine_research_evidence", run_id=run_dir.name, filename=result_name) if result_name else None,
                    "flight_passed": flight_passed,
                    "trajectory_url": trajectory_url,
                    "baseline_comparable": comparison.get("comparable"),
                    "outcome": outcome,
                    "outcome_variant": outcome_variant,
                    "qualification_status": qualification_status,
                    "manifest_integrity_valid": manifest_integrity_valid,
                    "manifest_signature_status": manifest_signature_status,
                    "signature_url": url_for("main.antoine_research_evidence", run_id=run_dir.name, filename="research-evidence-signature.json") if _public_evidence_file(run_dir, "research-evidence-signature.json") else None,
                    "manifest_errors": manifest_errors[:3],
                    "interpretation": result.get("interpretation", result.get("qualificationNote", "")),
                    "messages": attack.get("publishedMessages", replay.get("publishedMessages", result.get("publishedMessages", observed_count or None))),
                    "camera_topic_listed": manifest_measurements.get("cameraTopicListed"),
                    "camera_publisher_present": manifest_measurements.get("cameraPublisherPresent"),
                    "camera_frames": manifest_measurements.get("cameraFramesCaptured"),
                    "camera_run_scope_matches": manifest_measurements.get("cameraRunScopeMatches"),
                    "camera_world_url": url_for("main.antoine_research_evidence", run_id=run_dir.name, filename="camera-world.json") if _public_evidence_file(run_dir, "camera-world.json") else None,
                    "latency_ms": attack.get("publishToStopMs"),
                    "manifest_url": url_for("main.antoine_research_evidence", run_id=run_dir.name, filename="research-evidence-manifest.json") if _public_evidence_file(run_dir, "research-evidence-manifest.json") else None,
                    "failure_url": url_for("main.antoine_research_evidence", run_id=run_dir.name, filename="research-run-failure.txt") if failure_record else None,
                    "smoke_correlation_status": _mapping(result.get("natsGazeboCorrelation")).get("status"),
                    "smoke_correlation_details": _smoke_correlation_details(result.get("natsGazeboCorrelation")),
                    "provenance_url": url_for("main.antoine_research_evidence", run_id=run_dir.name, filename="research-provenance.json") if _public_evidence_file(run_dir, "research-provenance.json") else None,
                    "provenance_stable": manifest_measurements.get("provenanceStable") is True,
                    "provenance_changes": {
                        "inputs": _mapping(_mapping(manifest.get("sourceProvenance")).get("comparison")).get("changedInputs", []),
                        "sourceTrees": _mapping(_mapping(manifest.get("sourceProvenance")).get("comparison")).get("changedSourceTrees", []),
                    } if manifest_integrity_valid and isinstance(manifest, dict) else {"inputs": [], "sourceTrees": []},
            })
        evidence_runs.sort(key=lambda run: run["run_id"], reverse=True)
    scenario_catalog = _scenario_catalog()
    return render_template(
        "pages/attacks/research.html",
        section="attacks",
        sub_section="",
        current_page="attacks-research",
        findings_available=findings_path.is_file(),
        evidence_runs=evidence_runs,
        evidence_csv_url=url_for("main.antoine_research_csv"),
        scenario_coverage=scenario_coverage,
        legacy_crosswalk=_legacy_crosswalk_entries(scenario_catalog),
        legacy_crosswalk_url=url_for("main.antoine_research_crosswalk"),
        commands=[
            {
                "title": scenario.get("label", scenario["id"]),
                "command": _runner_command(scenario),
            }
            for scenario in scenario_catalog
            if scenario.get("status") in {"active", "partial"} and scenario.get("runner")
        ],
    )


@bp.route("/attacks/research/findings")
def antoine_research_findings():
    findings_path = _research_dir() / "findings.md"
    if not findings_path.is_file():
        abort(404)
    return send_file(findings_path, mimetype="text/markdown; charset=utf-8", as_attachment=True, download_name="findings.md")


@bp.route("/attacks/research/crosswalk.json")
def antoine_research_crosswalk():
    """Download the machine-readable mapping of legacy scenarios to Antoine."""
    crosswalk_path = _research_dir() / "legacy-scenario-crosswalk.json"
    if crosswalk_path.is_symlink() or not crosswalk_path.is_file():
        abort(404)
    return send_file(
        crosswalk_path,
        mimetype="application/json",
        as_attachment=True,
        download_name="legacy-scenario-crosswalk.json",
    )


@bp.route("/attacks/research/evidence.csv")
def antoine_research_csv():
    """Export redacted measurements from local evidence manifests."""
    import subprocess
    import sys
    import tempfile

    project_root = Path(current_app.root_path).parents[1]
    script = project_root / "research" / "export_research_csv.py"
    if script.is_symlink() or not script.is_file():
        abort(404)
    output_fd, output_name = tempfile.mkstemp(prefix="dvd-antoine-evidence-", suffix=".csv")
    os.close(output_fd)
    output = Path(output_name)
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "--output", str(output)],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        output.unlink(missing_ok=True)
        abort(503)
    if completed.returncode != 0 or output.is_symlink() or not output.is_file():
        output.unlink(missing_ok=True)
        current_app.logger.error("Research CSV export failed: %s", completed.stderr[-1000:])
        abort(503)
    response = send_file(
        output,
        mimetype="text/csv",
        as_attachment=True,
        download_name="antoine-research-summary.csv",
    )
    response.call_on_close(lambda: output.unlink(missing_ok=True))
    return response


@bp.route("/attacks/research/evidence/<run_id>/<filename>")
def antoine_research_evidence(run_id: str, filename: str):
    """Serve an allowlisted public evidence file from a named research run."""
    if not re.fullmatch(r"antoine-[a-z0-9-]+-\d{8}T\d{6}Z-\d+", run_id):
        abort(404)
    if filename not in {
        "research-result.json",
        "research-baseline-summary.json",
        "research-gps-tampering-summary.json",
        "research-early-revoke.json",
        "research-camera-observation.json",
        "camera-world.json",
        "research-waypoint-injection.json",
        "research-waypoint-replay-summary.json",
        "research-sequence-poisoning-summary.json",
        "research-sequence-poisoning.json",
        "research-waypoint-replay.json",
        "research-nats-observation.json",
        "research-evidence-manifest.json",
        "research-evidence-signature.json",
        "research-provenance.json",
        "research-provenance-comparison.json",
        "research-swarm-flight.json",
        "research-swarm-plan.json",
        "research-mavlink-swarm.json",
        "research-two-drone-smoke.json",
        "research-run-failure.txt",
        "research-swarm-fault.json",
        "research-swarm-config.json",
        "research-swarm-trajectory.svg",
    }:
        abort(404)
    evidence_root = _research_evidence_dir()
    evidence_path = _public_evidence_file(evidence_root / run_id, filename)
    if evidence_path is None:
        abort(404)
    if filename == "research-swarm-trajectory.svg":
        response = send_file(
            evidence_path, mimetype="image/svg+xml", as_attachment=False
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    if filename == "research-run-failure.txt":
        return send_file(
            evidence_path,
            mimetype="text/plain",
            as_attachment=True,
            download_name=filename,
        )
    return send_file(
        evidence_path,
        mimetype="application/json",
        as_attachment=True,
        download_name=filename,
    )

@bp.route("/attacks/<tactic>/<filename>")
def attack_scenario(tactic: str, filename: str):
    if tactic not in {"recon", "tampering", "dos", "injection", "exfiltration", "firmware", "antoine"}:
        abort(404)

    base_dir = _attacks_base_dir()
    tactic_dir = base_dir / tactic
    source_path = tactic_dir / filename
    if (
        tactic_dir.is_symlink()
        or source_path.is_symlink()
        or Path(filename).name != filename
        or source_path.suffix not in {".yaml", ".yml"}
        or not source_path.is_file()
    ):
        abort(404)
    try:
        resolved_tactic = tactic_dir.resolve(strict=True)
        resolved_yaml = source_path.resolve(strict=True)
    except OSError:
        abort(404)
    if resolved_yaml.parent != resolved_tactic:
        abort(404)

    try:
        with resolved_yaml.open("r", encoding="utf-8") as f:
            scenario = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        current_app.logger.exception("Failed to load attack scenario: %s", resolved_yaml)
        abort(404)
    if not isinstance(scenario, dict):
        abort(404)

    sections = scenario.get("sections", [])
    if not isinstance(sections, list):
        sections = []
    normalized_sections = []
    for section in sections:
        if not isinstance(section, dict):
            continue
        normalized = dict(section)
        content = normalized.get("content")
        if content is not None and not isinstance(content, (str, list)):
            normalized["content"] = str(content)
        if isinstance(normalized.get("content"), list):
            normalized["content"] = [item for item in normalized["content"] if isinstance(item, dict)]
        steps = normalized.get("steps")
        normalized["steps"] = [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []
        normalized_sections.append(normalized)
    scenario["sections"] = normalized_sections

    return render_template(
        "pages/attacks/template.html",
        section="attacks",
        current_page="attacks",
        **scenario,
    )
