# QGroundControl for Antoine SITL

For day-to-day bench work, use the Rust MAVLink/NATS console below. QGC is an
optional UI; it is not needed to inspect telemetry or exercise the bridge.

## Rust MAVLink/NATS console

The local console uses the same `mavlink` crate version and common dialect as
the Antoine bridge. It reads MAVLink heartbeats, position and GPS from UDP,
subscribes to live Antoine telemetry and commands over Core NATS, and displays
live Antoine telemetry and command messages over Core NATS. The acceptance
checkers inspect durable JetStream command audit without consuming it. Control
requests are limited to ARM and DISARM and use the same UDP peer as telemetry.

Start the native flight console after the NATS server, native Antoine SITL and
bridge are running:

```sh
make mavlink-console-start
```

Follow output with `tail -f /tmp/dvd-mavlink-console/console.log`; use
`make mavlink-console-stop` to stop the listener. `make mavlink-console` runs
the same listener in the foreground.

The control console defaults to NATS `127.0.0.1:14224`, SITL callsign
`qgc-live`, MAVLink listen port `14570`, bridge receive port `14651`, and local
command socket `14573`. Override the values with `MAVLINK_CONSOLE_ARGS` if your
run uses other ports or a callsign. The listener sends a GCS heartbeat so the
bridge can pin its reply peer to the same UDP client. For Gazebo telemetry
inspection without a command socket or heartbeat, run
`make mavlink-gazebo-console`; it subscribes to live NATS subjects only and
does not create or acknowledge JetStream consumers.

In another terminal, send a command:

```sh
make mavlink-arm
make mavlink-disarm
make mavlink-check
make mavlink-full-check
```

Each request travels as MAVLink `COMMAND_LONG` (`MAV_CMD_COMPONENT_ARM_DISARM`)
to the bridge, is written to JetStream, then sent over the Core NATS
request/reply subject `furia.mavlink.control.<callsign>`. Antoine returns an
accept/reject response; the bridge returns `COMMAND_ACK` only after acceptance.
The local control request also waits for a recent vehicle heartbeat with the
requested armed state. `make mavlink-check` starts a temporary console if the
control listener is not already running, then subscribes to telemetry, exercises
ARM and DISARM, checks that telemetry reports both state changes, verifies the
raw ARM/DISARM parameters in JetStream, and confirms the SITL returns to ground.
If a check fails after ARM, its cleanup attempts a DISARM before exiting. Do not
use ARM on a real aircraft; this bench is intended for the native Antoine
software SITL.

`make mavlink-full-check` combines prerequisite checks, repo lint/catalog
validation, bridge unit and loopback tests, console compile/format checks, a
live ARM/DISARM round trip, JetStream audit inspection, and a final disarmed
status check. It requires the local NATS, bridge, console, and Antoine SITL to
already be running. It actively arms then disarms the native software SITL.

## Native MAVLink flight controls

The `antoine-nats` adapter now maps a bounded MAVLink control surface to the
native Antoine 6-DoF SITL. Start Antoine with `--plant antoine
--enable-mavlink-control`; this opt-in is rejected for `--plant gz`, which
continues to require Core/Vigie authority. The bridge and SITL must use the
same callsign and a loopback NATS URL.

Supported controls:

- `MAV_CMD_COMPONENT_ARM_DISARM`, `MAV_CMD_NAV_TAKEOFF` (0.4–3 m),
  `MAV_CMD_NAV_LAND`, and `MAV_CMD_NAV_RETURN_TO_LAUNCH`.
- `MAV_CMD_DO_SET_MODE` and `SET_MODE`: STABILIZE, GUIDED, AUTO, HOLD, RTL,
  LAND. AUTO requires an uploaded mission; modes that move the aircraft
  require it to be armed.
- `SET_POSITION_TARGET_LOCAL_NED` position-only targets. Horizontal distance
  from home is limited to 3 m and down to -3..0 m; velocity, acceleration,
  yaw, and yaw-rate fields
  must be masked as ignored. GUIDED position targets expire after one second
  without another setpoint.
- Mission upload and download using `MISSION_COUNT`,
  `MISSION_REQUEST_INT`, and `MISSION_ITEM_INT`. Missions contain 1–5
  `MAV_CMD_NAV_WAYPOINT` items in `MAV_FRAME_LOCAL_NED` and share the same
  position envelope. The bridge audits GCS mission frames to JetStream before
  applying them, and reads the live mission from Antoine for mission-list
  responses. Each upload handshake is bound to the loopback UDP source,
  MAVLink system ID, and component ID that sent `MISSION_COUNT`; mission items
  from another local socket or component are rejected. An upload with no item
  activity for 10 seconds expires and is rejected.

The bridge maps `MAV_CMD_NAV_RETURN_TO_LAUNCH` to a targetless `{"type":"rtl"}`
request. The frame must target this vehicle, and all seven command parameters
must be zero; the adapter rejects other targets or parameter values before
publishing. This prevents a MAVLink caller from supplying a return point through
the bridge. The native Antoine NATS controller also accepts a direct `rtl`
control request when its callsign identity matches. In an isolated native SITL
check, a same-host unsigned request carrying an irrelevant stale `runId` and a
caller-supplied target payload was accepted; the controller ignored that
payload and returned toward its fixed home before landing. The envelope does
not establish run/task authority, so any same-host process with access to this
loopback broker can invoke the supported native controls. This result is for
the opt-in native Antoine software SITL only and does not describe the separate
Gazebo controller or its authority boundary. The observed return used native
SITL telemetry, not independent Gazebo truth, so it is not an independent
trajectory or actuator-safety result.

A separate isolated Gazebo flight check exercised the local test authority
directly on its loopback NATS subject. It rejected wrong callsign, unsupported
`set_return_target`, and an out-of-envelope position request. It accepted an
separate unsigned, stale-run, unrelated-task, fake expired-grant, and caller-
target RTL requests; each returned accepted. The caller-supplied 5 m return
target was ignored, and independent Gazebo pose plus the applier actuator audit
showed a return toward the authority-captured home, followed by landing and
disarm. The local test authority signs bounded actuator grants itself; this
result does not establish furia-core authorization or production NATS exposure.
Evidence: `/private/tmp/antoine-gz-mavlink-20261021T212758Z-4dbe20b0-4d44-4f62-b1fa-ecfb51581696/gazebo-mavlink-acceptance.json`.

The local scenario uses `pymavlink` through the `uv` `mavlink-check` group and
the Rust bridge's `mavlink` 0.18 common dialect. It starts a separate
`nats-server`, native SITL, and bridge on temporary loopback ports. It tests
ARM, takeoff altitude, explicit modes, north/east movement, mission upload and
readback, AUTO execution, bridge and direct-NATS RTL behavior, landing, DISARM,
and durable MAVLink frame audit. Run it with:

```sh
make mavlink-flight-check
```

`make mavlink-full-check` includes this isolated scenario and also exercises
the currently running local bench. The isolated scenario reports its final
telemetry in `/tmp/dvd-mavlink-flight-check.json`; on failure it preserves
SITL/bridge logs alongside that report. These controls model bounded local
software SITL behavior; they do not qualify the vehicle for hardware or
Gazebo actuation.

## Build and launch

`make qgc-gazebo` first checks for the configured Antoine-patched QGroundControl
and source-built Gazebo executables, before running the Antoine or bridge
builds. The profile uses the QGC executable from the configured source
checkout; a separate app in `/Applications` is not substituted because its
source revision and Antoine patch state are not established. Build the
configured checkout with `make qgc-antoine-build` when that executable is
missing. This preflight avoids spending time or disk on the SITL build when the
desktop client required by the profile cannot launch. It also requires at least 2 GiB free on
the filesystems holding the QGC/Gazebo executables and both Rust build targets.
Set `QGC_GAZEBO_MIN_FREE_MIB` to adjust that threshold.

The interactive profile writes `interactive-profile-receipt.json` beside its
run logs. The receipt records a run ID, callsign, ports, Antoine/Gazebo/QGC
revisions and clean/dirty classification, hashes of tracked diffs and each
dirty file (not their contents), toolchain version strings, executable hashes,
patch identity, generated config/world hashes, and process identities for the
run-owned supervisor, bridge, and Gazebo GUI.
Inspect it with `make qgc-gazebo-status PROFILE_RECEIPT=/path/to/receipt.json`.
Stop only identity-matched processes recorded in that receipt with
`make qgc-gazebo-stop PROFILE_RECEIPT=/path/to/receipt.json`; this waits for
identity-matched children to exit and reports unconfirmed stops as partial.
It preserves QGroundControl because it is an operator-owned application. Both
commands leave JSON output on stdout; stop also atomically writes
`interactive-profile-stop-result.json` beside the receipt and returns a nonzero
status when cleanup is partial. A stopped result also verifies that the
run-owned NATS and bridge ports are closed; the QGC UDP port is preserved. The
stop reports partial when the run supervisor or bridge record is missing. The
stop command removes stale supervisor identity/FIFO markers only after the
recorded child is confirmed absent and both private run-local artifacts match
that child. Unsafe or mismatched markers remain in place and make cleanup
partial. A still-live orphan whose supervisor has disappeared remains
unconfirmed; the command does not signal it by broad PID or process-group kill.
status command requires matching identities for the run supervisor and MAVLink
bridge, and subscribes for at most
1.5 seconds to two messages on the run's loopback
`furia.telemetry.<callsign>` subject. It reports armed state only when both
messages have the expected schema, protocol, and callsign, and their finite
simulation timestamps advance. Missing, malformed, mismatched, or
non-advancing telemetry leaves armed state unknown and the profile partial.
`qgcPeerRouting` distinguishes the bridge's identity-pinned peer
configuration from a fresh inbound heartbeat in the run-owned `ANTOINE_MAVLINK`
audit. `qgcHeartbeat` labels this evidence explicitly: status reads the latest
command-subject message with a bounded, read-only JetStream lookup and requires
a loopback `gcs_to_antoine` heartbeat no more than two seconds old, plus a live
process matching the recorded QGC identity. This shows that the configured
path carried an inbound heartbeat while QGC was running; UDP source addresses
do not authenticate which local process sent it. The bridge separately copies
validated outgoing HEARTBEAT envelopes to
`furia.mavlink.heartbeat.<callsign>`, allowing `bridgeHeartbeat` to check the
vehicle-to-GCS direction independently of ordinary telemetry cadence. Status
requires a fresh loopback envelope, the expected callsign, direction, schema,
and HEARTBEAT message ID. This is bridge-side audit evidence, not proof that
QGC rendered or acted on the packet. Neither a receipt nor a live PID is a
complete flight-health claim. Status validates the
configured TCP/UDP port values and requires all three configured listeners;
an invalid value or failed port probe leaves the profile partial.

QGC's toolbar `Ready`/`Not Ready` label is computed in
`src/Toolbar/MainStatusIndicator.qml`. For an unarmed vehicle, QGC first checks
whether the operator's pre-flight checklist blocks arming. Otherwise, if a
health-and-arming report is supported, it uses that report's `canArm` value.
If not, it uses `Vehicle.readyToFly` when the vehicle advertises that signal;
QGC derives this from the `MAV_SYS_STATUS_PREARM_CHECK` enabled and health
bits in `SYS_STATUS`. If neither readiness report is available, QGC falls back
to all-enabled-sensors-healthy plus `autopilotPlugin.setupComplete`. An armed
vehicle follows the separate Armed/Flying/Landing status path.

The Antoine bridge does not publish a health-and-arming report or
parameter-backed setup state. It advertises only the model `PREARM_CHECK` bit.
For native Antoine SITL, the bit's health value requires reported ground
contact, non-degraded GNSS, battery at least 20%, absolute altitude at most
0.1 m, each velocity component at most 0.15 m/s, and reported thrust at most
0.05. Gazebo telemetry has no thrust or ground-contact field: the bridge uses
zero thrust for this model-only calculation and permits `safe-hold` plus
absolute altitude at most 0.15 m as a ground proxy; it also accepts the
Gazebo-specific missing GNSS-degraded field. Battery, the 0.1 m readiness
altitude bound, and velocity bounds still apply. These assumptions do not
establish contact, physical GNSS, battery voltage, thrust, or hardware
readiness.

The canonical QGC patch exposes the Antoine arm control only for a generic quad
with MAVLink system ID 1, matching the system-ID restriction in the C++
capability gate; it does not enable that control for every generic quad
connected to QGC. The Fly View guided
position command also enforces a 3 m radial distance from home. This is
stricter than the bridge/controller envelope, which independently bounds north
and east to ±3 m, so the GUI's supported area is a circle inside the adapter's
square envelope. Both paths keep down position within -3..0 m.

Gazebo telemetry must also be no more than 1,000 ms old or 250 ms in the
future; stale and future observations are dropped before they can produce a
pre-arm-ready frame. Source inspection identifies QGC's possible label inputs,
but the exact branch responsible for the previously observed `Not Ready`
label has not been reproduced in a controlled fresh desktop session. Do not
infer hardware readiness from the label or the bridge's model gate.

The QGC checkout is expected at `~/Work/antoine-qgroundcontrol`. The MAVLink ↔
NATS bridge source and lockfile are versioned in
`research/antoine-mavlink-jetstream-bridge`; only the Antoine SITL itself stays
an external dependency at `~/Work/antoine`.

```sh
make qgc-antoine-build
make qgc-antoine-run
```

`make qgc-antoine-build` applies
[`patches/qgroundcontrol-antoine.patch`](patches/qgroundcontrol-antoine.patch)
if it is not already present, then builds the local QGC checkout. The current
checkout is configured as Debug; `QGC_CONFIG` selects the build and launch
configuration. First-time configuration needs the Qt 6.12 modules used by that
checkout and is separate from the bridge build. `make qgc-antoine-run` launches
the local app with a temporary QGC settings directory at
`/tmp/dvd-qgc-antoine-config`, enables UDP auto-connect on port 14550, and
directs it to the bridge on 14651. It refuses to start if another QGC owns 14550
or the bridge is not listening. It does not edit the user's normal QGC
preferences. `QGC_UDP_LISTEN_PORT` and `QGC_BRIDGE_PORT` can override the ports.
Before the interactive profile starts, `qgc-gazebo-preflight` verifies that
the complete canonical patch is already present in the configured QGC Git
checkout. A missing or partially diverged patch fails preflight instead of
being represented by the canonical patch hash in the run receipt.
If `ANTOINE_QGC_BINARY` selects a custom build path, it must resolve inside
that same checkout; preflight, the run receipt, and the QGC launcher all use
the selected executable path.
The full profile defaults its QGC settings, log, and PID files to separate
subdirectories of the unique run directory, so another profile cannot replace
the files captured or used by this run. Standalone `make qgc-antoine-run` keeps
its separate `/tmp/dvd-qgc-antoine-config` and `/tmp/dvd-qgc-antoine` defaults.

Run `make qgc-antoine-check` to send a harmless GCS heartbeat and targeted
`REQUEST_DATA_STREAM` frame and verify both were written to the callsign's
JetStream command audit. This exercises the QGC-facing UDP ingress and audit;
it does not issue a flight command. The isolated `make mavlink-flight-check`
scenario separately verifies mode changes, position targets, missions,
takeoff, land, and disarm.

The swarm MAVLink telemetry probe and headless Gazebo acceptance need an
interpreter that can receive loopback UDP. They default to `python3.11`, which
is supported by the research project; override it with `ANTOINE_MAVLINK_PYTHON`
if needed. The headless check keeps its locked `mavlink-check` dependencies in
an isolated uv environment at `${TMPDIR:-/tmp}/dvd-antoine-mavlink-python311`,
overridable with `ANTOINE_MAVLINK_PYTHON_ENV`. Before building or starting the
profile, the runner checks the resolved interpreter's macOS firewall rule and
UDP loopback receive path.

## Loopback connection

For the native Antoine plant, keep the NATS server, SITL, and bridge on the
same machine. Configure QGC's UDP link to listen on `127.0.0.1:14550` and send
to `127.0.0.1:14651`. The bridge binds its QGC receive socket on port `14651`
and sends MAVLink telemetry to QGC on port `14550`.

Example bridge invocation:

```sh
cargo run --locked --manifest-path research/antoine-mavlink-jetstream-bridge/Cargo.toml -- \
  --mode antoine-nats --callsign antoine-sitl \
  --nats-url nats://127.0.0.1:4222 \
  --qgc-bind 127.0.0.1:14651 --qgc-peer 127.0.0.1:14550 \
  --system-id 1 --home-alt-msl 82
```

Supported MAVLink control reaches Antoine through the request/reply subject
`furia.mavlink.control.<callsign>` on Core NATS. GCS-originated MAVLink frames
are durably written to the `ANTOINE_MAVLINK` JetStream stream before control is
applied; accepted `COMMAND_LONG` commands receive a `COMMAND_ACK`. Unsupported
commands are ignored. This is native SITL behavior; do not connect this profile
to a real vehicle or to the Gazebo controller.

## Interactive Gazebo profile

Run `make qgc-gazebo` to start a run-owned NATS broker, ground-start Gazebo
world, Antoine Gazebo controller, local test authority, and the versioned Rust
MAVLink bridge. The launcher adds a visible ground material to a run-owned copy
of the world and opens Gazebo with the checked-in camera/layout config. It also
opens the local QGC build if one is not already listening. Set
`ANTOINE_GZ_FLIGHT_GUI=0` to run the profile headless.
QGC uses `127.0.0.1:14550` ↔ `127.0.0.1:14651`; the run uses NATS port `14226`
and callsign `drone-1` by default. Override those with `QGC_UDP_LISTEN_PORT`,
`QGC_BRIDGE_PORT`, `ANTOINE_GZ_MAVLINK_NATS_PORT`, and
`ANTOINE_GZ_MAVLINK_CALLSIGN`.

For an end-to-end protocol acceptance without opening or requiring QGC, run
`make mavlink-gazebo-headless-check`. It builds/verifies the patched Antoine
SITL artifacts, starts a run-owned headless Gazebo profile and MAVLink bridge,
then exercises mode, takeoff, position, mission, RTL, land, and disarm through
the bridge. The checker correlates MAVLink with the run's independent Gazebo
pose stream and stores `gazebo-mavlink-acceptance.json` in the run directory.
It allocates loopback ports and stops only its own profile processes. The
receipt identifies the profile as headless and records no QGC source or binary
as a runtime input. This acceptance proves the MAVLink test-client path; it is
not QGC UI evidence. Failed attempts also write a `passed: false` report with
the failure, final observed MAVLink/Gazebo state, and safety-cleanup result.

To rebuild a saved profile's archived inputs from a fresh sealed checkout,
run `make antoine-gz-rebuild-check PROFILE_RUN_DIR=/path/to/profile-run
REBUILD_DIR=/path/to/new-rebuild-dir`. The command verifies the profile and
snapshot receipts, requires matching Cargo, Rust, and Gazebo versions, rebuilds
the three Antoine executables offline into a new target directory, verifies the
native Antoine receipt, and runs bounded headless acceptance against that
rebuilt checkout and the archived Gazebo/C2 bundle. It does not require or
launch QGC. Both output directories are non-overwriting; use a new
`REBUILD_DIR` for each attempt. Cargo registry sources and the installed
Gazebo toolchain must already be available locally. Rebuilt executable hashes
can differ from the original run because the sealed checkout has a new commit
and filesystem path; the rebuild receipt binds the actual source snapshot,
toolchain, and resulting binaries.

The bridge always sends telemetry to QGC's configured UDP peer. It can also
send a fixed copy to the local flight-check observer at `127.0.0.1:14574`.
This observer is configured at bridge startup; a valid frame from an arbitrary
local UDP source cannot redirect or subscribe itself to QGC telemetry.

The bridge audits MAVLink frames to JetStream, then requests control through
`furia.mavlink.control.<callsign>`. The run-owned local authority accepts only
bounded commands and signs fresh grants and setpoints for Antoine's existing
Gazebo path. It waits for ARM before issuing a flight grant; DISARM is accepted
only after return to the captured home position at low speed. Gazebo remains
the source of pose truth. This is a software-SITL path using a throwaway local
authority, not Core/Prime/Vigie authorization. Use Ctrl-C to stop the profile;
the launcher cleans up only processes it started.

Before launching the profile, `make qgc-gazebo` applies/verifies the versioned
local patch `patches/antoine-gazebo-battery-drain.patch`, runs its focused Rust
test, builds the Antoine SITL controller, local authority, and boundary service,
and writes a source/patch/binary SHA-256 receipt beside the Cargo target. The
patch makes the Gazebo plant honor
`--battery-drain-pct-per-s`; a configured zero drain therefore stays at 100%
during an interactive session. Run `make antoine-gz-battery-test` to repeat
that verification independently. `make doctor` checks that the selected
Antoine checkout contains the patch; on a changed checkout, review and apply
it with `make antoine-gz-battery-patch`.

Once the run-owned NATS/controller is ready, the interactive launcher writes
`research-provenance-pre.json` before opening the GUI clients. At shutdown it
writes `research-provenance.json` and
`research-provenance-comparison.json` in the run directory. These bind Git
revisions and dirty-state digests for Antoine, Gazebo, simulator assets, the
C2 bridge, QGC, and this research checkout; hashes for the QGC/Gazebo/Antoine/
bridge executables, the patch/build receipt, source and generated worlds,
applier, GUI and QGC settings, and normalized run configuration; and Rust,
Cargo, NATS, Gazebo, and uv versions. The comparison must be stable and the
patch must be applied for the run to count as provenance-qualified. The
launcher fails before starting SITL if the expected local QGC binary or
verified Antoine build artifacts are missing. Provenance identifies inspected
files; it does not attest which executable the OS loaded.

Each profile run also retains verified `antoine-source-snapshot/` and
`gazebo-source-snapshot/` bundles. The Antoine bundle includes its local Cargo
path dependencies, versioned lockfiles, and battery-drain patch receipt. The
Gazebo bundle includes the world, models, applier sources, build scripts, and
the C2 Gazebo-to-NATS bridge with its pinned Python requirements;
the profile launches against this run-owned Gazebo bundle. Their manifest
digests and receipt hashes are included in the profile receipt and pre/post
provenance. These bundles make the source inputs available for a clean rebuild;
the installed Gazebo toolchain remains a local prerequisite. The rebuild
command above proves that the archived Antoine inputs compile and pass headless
acceptance when combined with the archived Gazebo/C2 bundle.

Rebuild-from-receipt acceptance passed from committed profile
`dvd-antoine-committed-sealed-acceptance-20261022` into
`/private/tmp/dvd-antoine-committed-rebuild-retry-20261022`. The Antoine source
manifest remained `e94e5becf3d4aa0612b593500fbe7926950f88d62ceb2687fe6d0a87f2bfd303`;
the prebuilt-bundle reuse check verified all 142 files against the run capture.
The fresh headless flight report passed with stable provenance, 74 audited
frames, a 0.015 m MAVLink/Gazebo altitude delta, and final `armed=false`. The
rebuilt binary hashes differed from the earlier run, so this qualifies source,
toolchain, build-receipt, and behavior reproducibility rather than
byte-identical binaries. An initial attempt exposed a missing research-module
import in the prebuilt reuse branch; commit `cb3cb35` fixes it and adds mismatch
and tamper rejection tests.

Create a non-overwriting candidate from a stable run with
`make qgc-gazebo-pin-candidate PROFILE_RECEIPT=... PROFILE_COMPARISON=... PROFILE_PIN_CANDIDATE=...`.
The candidate command requires matching run IDs and a stable pre/post
comparison; headless candidates also require a passing flight report with a
disarmed final MAVLink state and recorded final Gazebo pose. It rehashes the
run-owned source snapshot files before recording their pins. It records complete
source, artifact, and toolchain values. If a
source checkout is unavailable, it can use the matching run-owned source
snapshot only when that snapshot has a verified manifest, receipt digest, and
file count; the verifier then compares those snapshot pins exactly. For a
headless profile, absent QGC binary and settings hashes are omitted because no
QGC process or settings file belongs to that run. Every candidate remains
`unreviewed`; the command cannot make a verified pin file.

Inspect the candidate's revisions, dirty-state hashes, artifacts, and toolchain
against the source and build review. Then explicitly set its `review` object to
`{"status":"reviewed","reviewer":"...","reviewedAtUtc":"...Z"}` and
save it as the reviewed pin file. Compare a captured interactive receipt using
`make qgc-gazebo-verify PROFILE_RECEIPT=... PROFILE_PINS=...`.
The pin file uses contract `damn-vulnerable-drone.interactive-profile-pins`
version `1.0.0`, and contains `profile`, `source`, `artifacts`, and `toolchain`
sections copied only after a reviewer accepts those values. It also carries
`sourceSnapshots` when the receipt has archived bundles. Dirty source pins must
include the exact worktree-status, tracked-diff, and dirty-file digests.
The verifier reports `verified`, `dirty-unreviewed`, `incompatible`, or
`missing`; it never creates or updates reviewed pins. Pinning dirty digests
identifies the reviewed checkout state but does not establish that its changes
are safe.

Run `make qgc-gazebo-check` to send a GCS-style heartbeat and stream request
through the UDP bridge and confirm JetStream audit. That probe is not a QGC GUI
action. Run `make mavlink-gazebo-flight-check` to exercise the live command path
for ARM/DISARM, takeoff, modes, position, mission upload/readback/execution,
RTL and landing; it requires the active profile's Gazebo pose recorder and
writes `/tmp/dvd-gazebo-mavlink-check.json`. The acceptance gate compares the
commands and MAVLink acknowledgements with independent Gazebo poses and leaves
the aircraft disarmed at its home position.

### Swarm telemetry fan-out

`ANTOINE_SWARM_MAVLINK=1 make swarm-flight` starts one bridge per callsign, each
with its own loopback UDP port pair and unique system ID. The runner then runs
`research/mavlink_swarm_probe.py`, which receives for a bounded window and
validates HEARTBEAT, GPS_RAW_INT, and GLOBAL_POSITION_INT framing, checksums,
per-callsign counts, and system-ID isolation. Evidence is written to
`research-mavlink-swarm.json`.

In the Gazebo profile this is telemetry fan-out only. The bridges subscribe to
`furia.telemetry.<callsign>`; their arm/disarm path publishes to
`furia.command.antoine.<callsign>`, which the Gazebo controller does not consume.
The probe sends no MAVLink packet, so it cannot arm, disarm, or steer the swarm.
Do not describe this path as GCS control of the Gazebo flight.

## Verification status

### Current configured environment (2026-10-22)

The configured Antoine-patched executable is absent at
`~/Work/antoine-qgroundcontrol/build/Debug/QGroundControl.app`; the source
checkout is modified and has no current build directory/CMake cache. A
previous Debug build attempt was interrupted before producing an executable.
The separate signed QGroundControl 5.1 app at `/Applications/QGroundControl.app`
has SHA-256 `a5a323a409d5b42446a15b1e3b75d8e05b1262d49f0656bcaa866711f2d49aa3`,
but its source revision and inclusion of the Antoine control patch are
unverified, so it is not counted as the configured patched app.

The default headless launcher captures, verifies, seals, builds, and launches
Antoine from a run-owned source snapshot. The latest QGC-free live acceptance at
`/private/tmp/antoine-gz-mavlink-20261022T001255Z-29145a78-6671-4413-94bf-6a37a6c7598c/gazebo-mavlink-acceptance.json`
passed with 68 audited MAVLink frames, valid `antoine-gz` telemetry, a 0.061 m
Gazebo/MAVLink altitude delta, and final `armed=false`. Its bounded direct-NATS
probe again showed that unsigned RTL requests, including stale-run,
unrelated-task, fake-expired-grant, and caller-target variants, are accepted by
the run-owned loopback test authority; caller targets were ignored in favor of
captured home. This is an expected local research finding, not a Core NATS or
production authorization claim. Pre/post provenance comparison reported stable
inputs and source trees. Post-stop status at
`/private/tmp/antoine-gz-mavlink-20261022T001255Z-29145a78-6671-4413-94bf-6a37a6c7598c/interactive-profile-status-after-stop.json`
confirmed the supervisor and bridge exited and the run-owned NATS and UDP
ports closed. The preceding QGC-free live acceptance at
`/private/tmp/antoine-gz-mavlink-20261022T000044Z-92f764a0-088e-450e-99b1-f1aa0789e6c0/gazebo-mavlink-acceptance.json`
passed after reusing binaries only when their sealed source snapshot matched the
fresh run snapshot. It recorded valid `antoine-gz` NATS telemetry, 122 audited
MAVLink frames, a 0.009 m Gazebo/MAVLink altitude delta, final `armed=false`,
and stable pre/post provenance with no changed inputs or source trees. Status
inspection after shutdown confirmed the run-owned NATS and UDP ports were
closed. A preceding isolated run at
`/private/tmp/antoine-gz-mavlink-20261021T235344Z-f157ef42-e9ff-404d-ac94-f1af29ad44ce/gazebo-mavlink-acceptance.json`
did not pass: its LAND path failed the settled-home gate, and cleanup did not
confirm disarm. That failure remains evidence, not a qualified run. The same
profile implementation passed two-cycle lifecycle acceptance at
`/tmp/dvd-antoine-headless-lifecycle-20261021T224449Z.json`: both runs reported
fresh telemetry and disarmed state; stale identity metadata could not signal
the live second bridge; both stops confirmed all run-owned processes and ports
closed. A fresh two-cycle run at
`/tmp/dvd-antoine-headless-lifecycle-20261022T001017Z.json` passed the same
checks using isolated ports: both runs reported fresh `antoine-gz` telemetry,
Gazebo/NATS simulation clocks aligned within 0.13 s, and `armed=false`; the
stale second-run receipt returned `identity-mismatch-not-signalled` while the
live profile remained recorded; both identity-checked stops confirmed NATS and
bridge ports closed. QGC was not started. These test-client/headless results do
not establish QGC GUI behavior.

The configured patched QGC executable remains absent. The separate signed app
at `/Applications/QGroundControl.app` still has SHA-256
`a5a323a409d5b42446a15b1e3b75d8e05b1262d49f0656bcaa866711f2d49aa3`, but its
source revision and Antoine patch state remain unverified. No QGC GUI-originated
mode, arm, position, or mission action has been verified, and the current
configured QGC `Not Ready` branch remains unobserved. The remaining paragraphs
in this section document earlier build and flight evidence rather than the
current configured GUI state.

### Historical QGC build and flight evidence

The local QGC source compiled successfully with the Antoine-specific control,
including the Fly view mode, takeoff, goto, mission-start, RTL, Land, and
Antoine-labeled arm/disarm integration. Its Debug app completed the QGC
simple-boot smoke test. `qmllint` returned success for the changed toolbar QML
with existing unqualified-access warnings.
The versioned bridge passes seven unit tests and three loopback integration
tests, including bidirectional QGC-peer routing and NATS-backed control. The
research Python suite passes 41 tests. `make validate` passes Ruff, formatting,
ShellCheck, shell syntax, scenario-catalog validation (23 scenarios), and legacy
crosswalk validation (42 guides). These checks are independent of a live flight.

The live acceptance run on callsign `drone-1` passed ARM, takeoff, HOLD/GUIDED,
a local position target, mission upload/readback/execution, RTL, a second
takeoff, LAND, and DISARM. It checked MAVLink acknowledgements and durable
JetStream audit against independent Gazebo poses. The latest run began at
`(0.113, -0.028, 0.090) m`, reached `0.848 m` on takeoff, executed the mission,
and ended at Gazebo truth `(0.152, 0.111, 0.160) m`; MAVLink reported disarmed
in STABILIZE at `0.173 m`. The altitude difference was `0.013 m`. The report
records pre-arm readiness before flight (`true`) and after disarm (`false`, as
the bridge's pre-arm gate is only advertised while the model is safely on the
ground under `safe-hold`). The machine-readable report is
`/tmp/dvd-gazebo-mavlink-check.json` and is specific to the latest run.

QGC receives telemetry on its configured loopback peer. Its `Not Ready` label
currently reflects QGC's own setup/readiness evaluation; this software SITL
does not provide a parameter-backed vehicle setup. The flight acceptance
records the bridge's model pre-arm gate as healthy before arming and false
after disarming, as expected from the gate's safe-hold conditions. QGC's UI
readiness is independent of the successful control and telemetry acceptance.

`make qgc-gazebo-check` confirms that a GCS heartbeat and stream request reach
the bridge and JetStream audit. The flight acceptance was driven by a MAVLink
test client; a mode change initiated by clicking QGC and then matched to
Antoine's NATS state remains a separate GUI acceptance check. macOS currently
denies Accessibility automation for QGC, so no synthetic UI click was sent.
This profile uses the local throwaway Gazebo authority, not Core/Prime/Vigie
authority, and is not for a real aircraft.
