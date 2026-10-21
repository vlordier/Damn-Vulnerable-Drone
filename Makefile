.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
SHELLCHECK ?= shellcheck
RESEARCH_PYTHON := $(UV) run --project research
RESEARCH_FLIGHT := research/run-antoine-two-drone-flight.sh
SWARM_RUNNER = RUSTUP_TOOLCHAIN="$(RUSTUP_TOOLCHAIN)" ANTOINE_GZ_CARGO_TARGET_DIR="$(ANTOINE_GZ_CARGO_TARGET_DIR)" ./$(RESEARCH_FLIGHT)
SWARM_LIVE_SECONDS ?= 75
LAB_SHELL_SCRIPTS := research/run-antoine-two-drone-flight.sh research/run-antoine-gazebo-mavlink.sh scripts/run-antoine.sh scripts/run-qgc-antoine.sh
ANTOINE_BRIDGE_DIR ?= $(CURDIR)/research/antoine-mavlink-jetstream-bridge
ANTOINE_BRIDGE_TARGET_DIR ?= /tmp/dvd-antoine-bridge-target
ANTOINE_QGC_DIR ?= $(HOME)/Work/antoine-qgroundcontrol
ANTOINE_GZ_CARGO_TARGET_DIR ?= /tmp/antoine-gz-target
QGC_CONFIG ?= Debug
ANTOINE_GZ_FLIGHT_GUI ?= 1
RUSTUP_TOOLCHAIN ?= 1.99.1
QGC_ANTOINE_PATCH := research/patches/qgroundcontrol-antoine.patch
MAVLINK_CONSOLE_DIR := research/mavlink-nats-console
MAVLINK_CONSOLE_TARGET_DIR ?= /tmp/dvd-mavlink-console-target
MAVLINK_CONSOLE_ARGS ?= --nats-url nats://127.0.0.1:14224 --callsign qgc-live --listen 127.0.0.1:14570 --bridge 127.0.0.1:14651
MAVLINK_AUDIT_STREAM ?= ANTOINE_MAVLINK
QGC_UDP_LISTEN_PORT ?= 14550
QGC_BRIDGE_PORT ?= 14651

.PHONY: help doctor antoine-gz-battery-patch antoine-gz-battery-test console-start console-status console-stop baseline attack-waypoint attack-replay observe-nats probe-gps observe-camera early-revoke state-timing swarm-smoke swarm-baseline swarm-flight swarm-live swarm-vee swarm-rendezvous swarm-split swarm-bridge swarm-3 swarm-5 swarm-blackhole swarm-rate swarm-auth-isolation test-swarm test-research bridge-test bridge-build mavlink-console mavlink-console-start mavlink-console-stop mavlink-control mavlink-arm mavlink-disarm mavlink-check mavlink-flight-check mavlink-gazebo-flight-check mavlink-full-check qgc-antoine-patch qgc-antoine-build qgc-antoine-run qgc-antoine-check qgc-gazebo-preflight qgc-gazebo qgc-gazebo-status qgc-gazebo-stop qgc-gazebo-check lint validate

help: ## Show common commands
	@printf '%s\n' \
	  'Research lab commands (run from the repository root):' \
	  '  make doctor           Check local Antoine/Gazebo prerequisites' \
	  '  make antoine-gz-battery-patch Apply the versioned Antoine SITL battery fix' \
	  '  make antoine-gz-battery-test Test, build, and record the patched SITL binary' \
	  '  make console-start     Start the local research console' \
	  '  make console-status    Show console and native launcher status' \
	  '  make console-stop      Stop the checkout-owned console' \
	  '  make baseline          Run the single-drone Antoine baseline' \
	  '  make attack-waypoint   Run the bounded waypoint-injection scenario' \
	  '  make attack-replay     Run the bounded setpoint-replay scenario' \
	  '  make observe-nats      Capture bounded read-only NATS observations' \
	  '  make probe-gps         Run the signed-GPS tampering probe' \
	  '  make observe-camera    Run the camera metadata-only rehearsal' \
	  '  make early-revoke      Measure local authority revoke response' \
	  '  make state-timing      Observe swarm state timing without injection' \
	  '  make swarm-smoke       Check two-drone NATS/actuator wiring only' \
	  '  make swarm-baseline    Run a clean 2–5 vehicle trajectory baseline' \
	  '  make swarm-flight      Fly two drones and measure pair separation' \
	  '  make swarm-live        Open Gazebo GUI while two drones fly' \
	  '  make swarm-live SWARM_LIVE_SECONDS=90 ANTOINE_SWARM_SIZE=3' \
	  '  make swarm-vee         Fly the scripted V formation (also rendezvous/split)' \
	  '  make swarm-bridge      Fly with one MAVLink telemetry bridge per drone' \
	  '  make test-swarm        Run planner, evaluator, and MAVLink probe unit tests' \
	  '  make swarm-3           Fly three drones' \
	  '  make swarm-5           Fly five drones' \
	  '  make swarm-blackhole   Partition one controller-only state stream for 2s and verify recovery' \
	  '  make swarm-rate        Send a bounded wrong-callsign setpoint burst on a run-owned broker' \
	  '  make bridge-build      Build the versioned MAVLink↔NATS bridge' \
	  '  make bridge-test       Test the versioned MAVLink↔NATS bridge' \
	  '  make mavlink-console   Listen to MAVLink and NATS telemetry/audit' \
	  '  make mavlink-console-start Start the default MAVLink/NATS console' \
	  '  make mavlink-gazebo-console Start a read-only console for the Gazebo profile' \
	  '  make mavlink-control ACTION=arm|disarm Send a control request to the running console' \
	  '  make mavlink-arm       Send a bounded MAVLink arm request' \
	  '  make mavlink-disarm    Send a bounded MAVLink disarm request' \
	  '  make mavlink-check     Verify telemetry, ARM/DISARM, ACK, audit and safe SITL state' \
	  '  make mavlink-flight-check Isolated full MAVLink mission/flight acceptance scenario' \
	  '  make mavlink-gazebo-flight-check Test live QGC bridge controls against Gazebo poses' \
	  '  make mavlink-full-check Run static, bridge, and live bench checks (requires running SITL)' \
	  '  make qgc-antoine-build Build QGC with the Antoine SITL arm control' \
	  '  make qgc-antoine-run   Launch local QGC with loopback link to the active bridge' \
	  '  make qgc-antoine-check Verify QGC-origin heartbeat/requests are audited by NATS' \
	  '  make qgc-gazebo        Open QGC and Gazebo 3D for the Antoine/NATS profile' \
	  '  make qgc-gazebo-check Verify GCS-style UDP frames reach the Gazebo audit' \
	  '  make lint              Run Ruff, ShellCheck, and shell syntax checks' \
	  '  make validate          Run lint and validate the scenario catalog'

antoine-gz-battery-patch: ## Apply the versioned Gazebo battery-drain fix to the Antoine checkout
	@antoine_root="$${ANTOINE_ROOT:-$(HOME)/Work/antoine}"; \
	patch_file="$(CURDIR)/research/patches/antoine-gazebo-battery-drain.patch"; \
	test -d "$$antoine_root/.git" || { echo "Missing Antoine Git checkout: $$antoine_root" >&2; exit 1; }; \
	if git -C "$$antoine_root" apply --reverse --check "$$patch_file" >/dev/null 2>&1; then \
	  echo 'Antoine Gazebo battery-drain fix is already applied'; \
	elif git -C "$$antoine_root" apply --check "$$patch_file" >/dev/null 2>&1; then \
	  git -C "$$antoine_root" apply "$$patch_file"; \
	  echo 'Applied Antoine Gazebo battery-drain fix'; \
	else \
	  echo 'Patch does not apply cleanly; inspect the Antoine SITL source before proceeding.' >&2; exit 1; \
	fi

antoine-gz-battery-test: antoine-gz-battery-patch ## Test, build and record the Antoine Gazebo battery fix
	@set -eu; \
	antoine_root="$${ANTOINE_ROOT:-$(HOME)/Work/antoine}"; \
	manifest="$$antoine_root/sitl/antoine-sitl/Cargo.toml"; \
	source="$$antoine_root/sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs"; \
	RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(ANTOINE_GZ_CARGO_TARGET_DIR)" cargo test --locked --manifest-path "$$manifest" --features nats --bin antoine_sitl_nats gazebo_plant_honors_configured_battery_drain_rate; \
	RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(ANTOINE_GZ_CARGO_TARGET_DIR)" cargo build --locked --manifest-path "$$manifest" --features nats --bin antoine_sitl_nats; \
	RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(ANTOINE_GZ_CARGO_TARGET_DIR)" cargo build --locked --manifest-path "$$manifest" --features nats --bin antoine_gz_local_authority; \
	RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(ANTOINE_GZ_CARGO_TARGET_DIR)" cargo build --locked --manifest-path "$$antoine_root/runtime/antoine-boundary/Cargo.toml" --features service --bin antoine-boundary-service; \
	uv run --no-project python research/record_antoine_build.py --source "$$source" --patch "$(CURDIR)/research/patches/antoine-gazebo-battery-drain.patch" --binary "$(ANTOINE_GZ_CARGO_TARGET_DIR)/debug/antoine_sitl_nats" --additional-binary "$(ANTOINE_GZ_CARGO_TARGET_DIR)/debug/antoine_gz_local_authority" --additional-binary "$(ANTOINE_GZ_CARGO_TARGET_DIR)/debug/antoine-boundary-service" --output "$(ANTOINE_GZ_CARGO_TARGET_DIR)/antoine-gz-battery-build.json" --toolchain "$(RUSTUP_TOOLCHAIN)"; \
	echo "Battery-fix build receipt: $(ANTOINE_GZ_CARGO_TARGET_DIR)/antoine-gz-battery-build.json"

doctor: ## Check the local Antoine/Gazebo prerequisites without starting a run
	@set -eu; \
	for tool in cargo nats nats-server uv; do \
	  command -v "$$tool" >/dev/null || { echo "Missing command: $$tool" >&2; exit 1; }; \
	done; \
	antoine_root="$${ANTOINE_ROOT:-$(HOME)/Work/antoine}"; \
	sim_root="$${AGR_SIM:-$(HOME)/Work/durandal-gz-swarm-sim}"; \
	c2_scripts="$${AGR_C2_SCRIPTS:-$(HOME)/Work/furia-c2/scripts}"; \
	gz_bin="$${GZ_BIN:-$(HOME)/Work/gz-macOS/ws/install/bin/gz}"; \
	for path in \
	  "$$antoine_root/scripts/gz_flight_proof.sh" \
	  "$$antoine_root/sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs" \
	  "$$sim_root/worlds/antoine-gz-flight-proof.sdf" \
	  "$$sim_root/models/x500/model.sdf" \
	  "$$sim_root/tools/gz-sitl-applier/gz-sitl-applier" \
	  "$$c2_scripts/c2-gz-sitl-bridge.py" \
	  "$$gz_bin"; do \
	  [[ -e "$$path" ]] || { echo "Missing Antoine/Gazebo input: $$path" >&2; exit 1; }; \
	done; \
	if ! rg -q 'battery_after_drain\(battery_pct, battery_drain_pct_per_s, dt\)' "$$antoine_root/sitl/antoine-sitl/src/bin/antoine_sitl_nats.rs"; then \
	  echo 'Antoine SITL is missing the versioned Gazebo battery-drain fix.' >&2; \
	  echo 'Apply research/patches/antoine-gazebo-battery-drain.patch from the repository root.' >&2; \
	  exit 1; \
	fi; \
	echo 'Antoine/Gazebo prerequisites are present.'

console-start: ## Start the local research console
	./research/console.sh start

console-status: ## Show console and native launcher status
	./status.sh

console-stop: ## Stop the checkout-owned console
	./research/console.sh stop

baseline: ## Run the single-drone Antoine baseline
	./research/run-antoine-baseline.sh

attack-waypoint: ## Run one bounded waypoint-injection scenario
	./research/run-antoine-scenario.sh waypoint-injection

attack-replay: ## Run one bounded setpoint-replay scenario
	./research/run-antoine-replay.sh

observe-nats: ## Capture bounded read-only observations from run-local NATS
	./research/run-antoine-observation.sh

probe-gps: ## Run the signed-GPS tampering probe
	./research/run-antoine-sensor-tampering.sh

observe-camera: ## Run the bounded camera metadata-only rehearsal
	./research/run-antoine-camera-observation.sh

early-revoke: ## Measure bounded local authority revoke response
	./research/run-antoine-early-revoke.sh

state-timing: ## Observe vehicle state-stream timing without injection
	ANTOINE_SWARM_FAULT=state-stream-timing $(SWARM_RUNNER)

swarm-smoke: ## Check two-drone NATS/actuator wiring without flight control
	./research/run-antoine-two-drone-smoke.sh

swarm-baseline: ## Capture a clean swarm flight baseline and evidence report
	$(SWARM_RUNNER)

swarm-flight: ## Fly the selected swarm size (defaults to two drones)
	$(SWARM_RUNNER)

swarm-live: ## Open the Gazebo GUI to watch the two-drone flight live
	ANTOINE_RESEARCH_FLIGHT_SECONDS=$(SWARM_LIVE_SECONDS) ANTOINE_GZ_FLIGHT_GUI=1 $(SWARM_RUNNER)

swarm-vee: ## Fly the scripted V formation and qualify final slots
	ANTOINE_SWARM_BEHAVIOR=vee $(SWARM_RUNNER)

swarm-rendezvous: ## Fly the scripted rendezvous ring and qualify convergence
	ANTOINE_SWARM_BEHAVIOR=rendezvous $(SWARM_RUNNER)

swarm-split: ## Fly the scripted split into two subgroups
	ANTOINE_SWARM_BEHAVIOR=split $(SWARM_RUNNER)

swarm-bridge: ## Fly with one MAVLink telemetry bridge per drone
	ANTOINE_SWARM_MAVLINK=1 $(SWARM_RUNNER)

test-swarm: ## Run planner, summarizer, and MAVLink probe unit tests
	$(RESEARCH_PYTHON) python -m unittest discover -s research -p 'test_swarm_behavior.py' -v
	$(RESEARCH_PYTHON) python -m unittest discover -s research -p 'test_swarm_cross_vehicle_probe.py' -v
	$(RESEARCH_PYTHON) python -m unittest discover -s research -p 'test_summarize_swarm_flight.py' -v
	$(RESEARCH_PYTHON) python -m unittest discover -s research -p 'test_snapshot_gazebo_inputs.py' -v
	$(RESEARCH_PYTHON) python -m unittest discover -s research -p 'test_snapshot_antoine_build_inputs.py' -v
	$(RESEARCH_PYTHON) python -m unittest discover -s research -p 'test_record_antoine_run_build.py' -v
	$(RESEARCH_PYTHON) python -m unittest discover -s research -p 'test_capture_antoine_provenance.py' -v
	$(RESEARCH_PYTHON) python -m unittest discover -s research -p 'test_mavlink_swarm_probe.py' -v

test-research: ## Run every research Python unit test
	$(RESEARCH_PYTHON) python -m unittest discover -s research -p 'test_*.py' -v

swarm-3: ## Fly three drones
	ANTOINE_SWARM_SIZE=3 $(SWARM_RUNNER)

swarm-5: ## Fly five drones
	ANTOINE_SWARM_SIZE=5 $(SWARM_RUNNER)

swarm-blackhole: ## Partition one controller-only state stream for two seconds and verify recovery
	ANTOINE_RESEARCH_FLIGHT_SECONDS=75 ANTOINE_SWARM_FAULT=state-stream-partition ANTOINE_SWARM_FAULT_SECONDS=2 $(SWARM_RUNNER)

swarm-rate: ## Send up to 40 wrong-callsign setpoints at 10 Hz on a run-owned broker
	ANTOINE_SWARM_FAULT=command-rate ANTOINE_SWARM_FAULT_RATE_HZ=10 ANTOINE_SWARM_FAULT_SECONDS=4 ANTOINE_SWARM_FAULT_MAX_MESSAGES=40 $(SWARM_RUNNER)

swarm-auth-isolation: ## Attempt one misaddressed setpoint on the run-owned NATS broker
	ANTOINE_SWARM_FAULT=cross-vehicle-authorization ANTOINE_SWARM_FAULT_MAX_MESSAGES=1 $(SWARM_RUNNER)

bridge-build: ## Build the versioned MAVLink↔NATS bridge
	RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(ANTOINE_BRIDGE_TARGET_DIR)" cargo build --locked --manifest-path "$(ANTOINE_BRIDGE_DIR)/Cargo.toml"

bridge-test: ## Test the versioned MAVLink↔NATS bridge, including bidirectional arm/disarm
	RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(ANTOINE_BRIDGE_TARGET_DIR)" cargo test --locked --all-targets --manifest-path "$(ANTOINE_BRIDGE_DIR)/Cargo.toml"

mavlink-console: ## Read MAVLink telemetry and NATS/JetStream command evidence
	RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(MAVLINK_CONSOLE_TARGET_DIR)" cargo run --locked --manifest-path "$(MAVLINK_CONSOLE_DIR)/Cargo.toml" -- listen $(MAVLINK_CONSOLE_ARGS)

mavlink-console-start: ## Start the local MAVLink/NATS console and log to /tmp
	@mkdir -p /tmp/dvd-mavlink-console
	@nohup env RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(MAVLINK_CONSOLE_TARGET_DIR)" cargo run --quiet --locked --manifest-path "$(MAVLINK_CONSOLE_DIR)/Cargo.toml" -- listen $(MAVLINK_CONSOLE_ARGS) > /tmp/dvd-mavlink-console/console.log 2>&1 </dev/null &
	@sleep 2
	@lsof -nP -iUDP:14570 -iUDP:14573
	@echo 'Console log: /tmp/dvd-mavlink-console/console.log'

mavlink-gazebo-console: ## Run a read-only, non-consuming NATS monitor for the Gazebo profile
	RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(MAVLINK_CONSOLE_TARGET_DIR)" cargo run --locked --manifest-path "$(MAVLINK_CONSOLE_DIR)/Cargo.toml" -- monitor --nats-url "$${MAVLINK_NATS_URL:-nats://127.0.0.1:14226}" --callsign "$${MAVLINK_CALLSIGN:-drone-1}"

mavlink-console-stop: ## Stop the local loopback MAVLink/NATS console
	@pids="$$(lsof -tiUDP:14570 -tiUDP:14573 | sort -u)"; if [[ -n "$$pids" ]]; then kill $$pids; else echo 'No console is listening on 14570/14573'; fi

mavlink-control: ## Send arm, disarm, or status to the running loopback console
	@case "$(ACTION)" in arm|disarm|status) ;; *) echo 'Set ACTION=arm, ACTION=disarm, or ACTION=status' >&2; exit 2;; esac
	@ACTION="$(ACTION)" uv run --no-project python -c 'import os, socket, uuid; action=os.environ["ACTION"]; request_id=uuid.uuid4().hex[:12]; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(7); s.sendto(f"{action} {request_id}".encode(), ("127.0.0.1", 14573)); response, _=s.recvfrom(512); response=response.decode(); print(response); raise SystemExit(0 if response.startswith(f"ok {request_id} ") else 1)'

mavlink-arm: ## Send a MAVLink ARM request through the running loopback console
	@$(MAKE) --no-print-directory mavlink-control ACTION=arm

mavlink-disarm: ## Send a MAVLink DISARM request through the running loopback console
	@$(MAKE) --no-print-directory mavlink-control ACTION=disarm

mavlink-check: ## Verify both MAVLink/NATS directions and leave the SITL disarmed
	@set -euo pipefail; \
	console_pid=''; \
	if ! lsof -nP -iUDP:14573 >/dev/null 2>&1; then \
	  RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(MAVLINK_CONSOLE_TARGET_DIR)" cargo build --quiet --locked --manifest-path "$(MAVLINK_CONSOLE_DIR)/Cargo.toml"; \
	  "$(MAVLINK_CONSOLE_TARGET_DIR)/debug/mavlink-nats-console" listen $(MAVLINK_CONSOLE_ARGS) > /tmp/dvd-mavlink-check-console.log 2>&1 & \
	  console_pid=$$!; \
	  trap 'kill "$$console_pid" 2>/dev/null || true; wait "$$console_pid" 2>/dev/null || true' EXIT; \
	  ready=0; \
	  for attempt in $$(seq 1 20); do \
	    if lsof -nP -iUDP:14573 >/dev/null 2>&1; then ready=1; break; fi; \
	    kill -0 "$$console_pid" 2>/dev/null || { cat /tmp/dvd-mavlink-check-console.log; exit 1; }; \
	    sleep 0.25; \
	  done; \
	  test "$$ready" -eq 1 || { cat /tmp/dvd-mavlink-check-console.log; echo 'MAVLink console did not become ready' >&2; exit 1; }; \
	fi; \
	MAVLINK_NATS_URL="$${MAVLINK_NATS_URL:-nats://127.0.0.1:14224}" MAVLINK_CALLSIGN="$${MAVLINK_CALLSIGN:-qgc-live}" MAVLINK_CONTROL_PORT=14573 MAVLINK_AUDIT_STREAM="$(MAVLINK_AUDIT_STREAM)" $(UV) run --project research --group mavlink-check research/mavlink-nats-console/check_roundtrip.py

mavlink-flight-check: ## Isolated end-to-end mission, takeoff, mode and position flight check
	@ANTOINE_BRIDGE_DIR="$(ANTOINE_BRIDGE_DIR)" MAVLINK_BRIDGE_TARGET="$(ANTOINE_BRIDGE_TARGET_DIR)" $(UV) run --project research --group mavlink-check research/mavlink-nats-console/flight_check.py

mavlink-full-check: ## Run isolated flight scenario plus local bench MAVLink/NATS checks
	@$(MAKE) --no-print-directory doctor
	@$(MAKE) --no-print-directory validate
	@$(MAKE) --no-print-directory bridge-test
	@$(MAKE) --no-print-directory mavlink-flight-check
	@RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(MAVLINK_CONSOLE_TARGET_DIR)" cargo fmt --check --manifest-path "$(MAVLINK_CONSOLE_DIR)/Cargo.toml"
	@RUSTUP_TOOLCHAIN=$(RUSTUP_TOOLCHAIN) CARGO_TARGET_DIR="$(MAVLINK_CONSOLE_TARGET_DIR)" cargo check --locked --manifest-path "$(MAVLINK_CONSOLE_DIR)/Cargo.toml"
	@$(MAKE) --no-print-directory mavlink-control ACTION=status
	@$(MAKE) --no-print-directory mavlink-check
	@$(MAKE) --no-print-directory mavlink-control ACTION=status
	@echo 'Full check passed: SITL is disarmed; MAVLink support remains limited to the tested adapter surface.'

qgc-antoine-patch: ## Apply the Antoine Fly view controls to the QGC checkout
	@test -d "$(ANTOINE_QGC_DIR)/.git" || { echo "Missing QGC source at $(ANTOINE_QGC_DIR)" >&2; exit 1; }
	@if git -C "$(ANTOINE_QGC_DIR)" apply --reverse --check "$(CURDIR)/$(QGC_ANTOINE_PATCH)" >/dev/null 2>&1; then \
	  echo 'Antoine QGC patch is already applied'; \
	else \
	  git -C "$(ANTOINE_QGC_DIR)" apply "$(CURDIR)/$(QGC_ANTOINE_PATCH)"; \
	fi

qgc-antoine-build: qgc-antoine-patch ## Build QGC with Antoine SITL Fly view controls
	@test -f "$(ANTOINE_QGC_DIR)/build/CMakeCache.txt" || { echo "Configure QGC first; see research/qgroundcontrol-antoine.md" >&2; exit 1; }
	cmake --build "$(ANTOINE_QGC_DIR)/build" --target QGroundControl --config "$(QGC_CONFIG)" --parallel 8

qgc-antoine-run: ## Launch local QGC with loopback UDP to the active Antoine bridge
	QGC_UDP_LISTEN_PORT="$(QGC_UDP_LISTEN_PORT)" QGC_BRIDGE_PORT="$(QGC_BRIDGE_PORT)" ANTOINE_QGC_DIR="$(ANTOINE_QGC_DIR)" QGC_CONFIG="$(QGC_CONFIG)" ./scripts/run-qgc-antoine.sh

qgc-antoine-check: ## Verify QGC-origin heartbeats and requests reach the bridge audit
	NATS_URL="$${MAVLINK_NATS_URL:-nats://127.0.0.1:14224}" MAVLINK_CALLSIGN="$${MAVLINK_CALLSIGN:-qgc-live}" QGC_BRIDGE_PORT="$(QGC_BRIDGE_PORT)" $(UV) run --project research --group mavlink-check research/mavlink-nats-console/check_qgc_bridge.py

qgc-gazebo-preflight: ## Check large interactive-profile prerequisites before starting any build
	@set -eu; \
	qgc_binary="$(ANTOINE_QGC_DIR)/build/$(QGC_CONFIG)/QGroundControl.app/Contents/MacOS/QGroundControl"; \
	gz_binary="$${GZ_BIN:-$(HOME)/Work/gz-macOS/ws/install/bin/gz}"; \
	if [[ ! -x "$$qgc_binary" ]]; then \
	  echo "Missing QGroundControl executable: $$qgc_binary" >&2; \
	  echo "Build the configured checkout with 'make qgc-antoine-build' before starting the Gazebo profile." >&2; \
	  exit 1; \
	fi; \
	[[ -x "$$gz_binary" ]] || { echo "Missing source-built Gazebo executable: $$gz_binary" >&2; exit 1; }; \
	echo 'Interactive QGC and Gazebo executables are present.'

qgc-gazebo: qgc-gazebo-preflight doctor antoine-gz-battery-test bridge-build ## Start the interactive loopback QGC/Antoine/Gazebo profile
	ANTOINE_GZ_CARGO_TARGET_DIR="$(ANTOINE_GZ_CARGO_TARGET_DIR)" ANTOINE_GZ_FLIGHT_GUI="$(ANTOINE_GZ_FLIGHT_GUI)" ./research/run-antoine-gazebo-mavlink.sh

qgc-gazebo-status: ## Inspect a selected QGC/Gazebo profile receipt without inferring health from PIDs alone
	@test -n "$(PROFILE_RECEIPT)" || { echo 'Set PROFILE_RECEIPT=/path/to/interactive-profile-receipt.json' >&2; exit 2; }
	$(UV) run --project research python research/interactive_profile.py status "$(PROFILE_RECEIPT)"

qgc-gazebo-stop: ## Gracefully interrupt only identity-matched run-owned children; preserve QGC
	@test -n "$(PROFILE_RECEIPT)" || { echo 'Set PROFILE_RECEIPT=/path/to/interactive-profile-receipt.json' >&2; exit 2; }
	$(UV) run --project research python research/interactive_profile.py stop "$(PROFILE_RECEIPT)"

qgc-gazebo-check: ## Verify GCS-style UDP frames are audited by the active Gazebo MAVLink bridge
	NATS_URL="$${MAVLINK_NATS_URL:-nats://127.0.0.1:14226}" MAVLINK_CALLSIGN="$${MAVLINK_CALLSIGN:-drone-1}" QGC_BRIDGE_PORT="$(QGC_BRIDGE_PORT)" $(UV) run --project research --group mavlink-check research/mavlink-nats-console/check_qgc_bridge.py

mavlink-gazebo-flight-check: ## Exercise modes, takeoff, position, mission, RTL, land and disarm against Gazebo truth
	@pose_file="$${ANTOINE_GZ_POSE_FILE:-$$(ls -td /tmp/antoine-gz-mavlink-*/gazebo-pose.jsonl 2>/dev/null | head -1)}"; \
	test -f "$$pose_file" || { echo "Set ANTOINE_GZ_POSE_FILE to the active run's gazebo-pose.jsonl" >&2; exit 1; }; \
	ANTOINE_GZ_POSE_FILE="$$pose_file" MAVLINK_NATS_URL="$${MAVLINK_NATS_URL:-nats://127.0.0.1:14226}" MAVLINK_CALLSIGN="$${MAVLINK_CALLSIGN:-drone-1}" $(UV) run --project research --group mavlink-check research/mavlink-nats-console/gazebo_flight_check.py

lint: ## Run focused Python and shell lint checks
	$(RESEARCH_PYTHON) --group lint ruff check research
	$(RESEARCH_PYTHON) --group lint ruff format --check research
	$(SHELLCHECK) --external-sources --shell=bash --enable=all --severity=warning $(LAB_SHELL_SCRIPTS)
	@for script in $(LAB_SHELL_SCRIPTS); do bash -n "$$script"; done

validate: lint ## Lint and validate scenario catalog and crosswalk structure
	$(RESEARCH_PYTHON) python -c "from pathlib import Path; from dvd_research.scenario_catalog import load_scenario_catalog; scenarios = load_scenario_catalog(Path('research/scenario-catalog.json'), Path('.'), Path('simulator/mgmt/templates/pages/attacks/antoine')); print('Validated', len(scenarios), 'research scenarios')"
	$(RESEARCH_PYTHON) research/validate_legacy_crosswalk.py
