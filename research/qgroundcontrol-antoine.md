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
- `SET_POSITION_TARGET_LOCAL_NED` position-only targets. N/E are limited to
  ±3 m and down to -3..0 m; velocity, acceleration, yaw, and yaw-rate fields
  must be masked as ignored. GUIDED position targets expire after one second
  without another setpoint.
- Mission upload and download using `MISSION_COUNT`,
  `MISSION_REQUEST_INT`, and `MISSION_ITEM_INT`. Missions contain 1–5
  `MAV_CMD_NAV_WAYPOINT` items in `MAV_FRAME_LOCAL_NED` and share the same
  position envelope. The bridge audits GCS mission frames to JetStream before
  applying them, and reads the live mission from Antoine for mission-list
  responses.

The bridge maps `MAV_CMD_NAV_RETURN_TO_LAUNCH` to a targetless `{"type":"rtl"}`
request. The frame must target this vehicle, and all seven command parameters
must be zero; the adapter rejects other targets or parameter values before
publishing. This proves the bridge cannot carry a caller-selected return point.
It does not prove that a direct NATS producer without run/task authority is
rejected by the Antoine controller; that boundary still needs an isolated
controller run and independent trajectory evidence.

The local scenario uses `pymavlink` through the `uv` `mavlink-check` group and
the Rust bridge's `mavlink` 0.18 common dialect. It starts a separate
`nats-server`, native SITL, and bridge on temporary loopback ports. It tests
ARM, takeoff altitude, explicit modes, north/east movement, mission upload and
readback, AUTO execution, RTL mode, landing, DISARM, and durable MAVLink frame
audit. Run it with:

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
status command requires matching identities for the run supervisor and MAVLink
bridge, and subscribes for at most
1.5 seconds to two messages on the run's loopback
`furia.telemetry.<callsign>` subject. It reports armed state only when both
messages have the expected schema, protocol, and callsign, and their finite
simulation timestamps advance. Missing, malformed, mismatched, or
non-advancing telemetry leaves armed state unknown and the profile partial.
`qgcPeerRouting` distinguishes the bridge's identity-pinned peer
configuration from an observed QGC heartbeat; status does not claim that a
configured peer has actually sent traffic. Neither a receipt nor a live PID is
a complete flight-health claim. Status validates the configured TCP/UDP port
values and requires all three configured listeners; an invalid value or failed
port probe leaves the profile partial.

QGC's model pre-arm indication comes from the bridge's `SYS_STATUS`
`PREARM_CHECK` bit. It reflects the local simulation gate (ground/safe-hold,
altitude, velocity, thrust where available, and model battery level). Antoine
does not provide parameter-backed vehicle setup or physical sensor health;
this bit must not be read as hardware readiness. The bridge accepts a Gazebo
observation only when its `observedAtUnixMs` is no more than 1,000 ms old and
no more than 250 ms in the future; older/future observations are dropped, so
they cannot produce a `SYS_STATUS` pre-arm-ready frame.
The exact GUI Not Ready label also remains unreproduced in a controlled fresh
desktop session.

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

Run `make qgc-antoine-check` to send a harmless GCS heartbeat and targeted
`REQUEST_DATA_STREAM` frame and verify both were written to the callsign's
JetStream command audit. This exercises the QGC-facing UDP ingress and audit;
it does not issue a flight command. The isolated `make mavlink-flight-check`
scenario separately verifies mode changes, position targets, missions,
takeoff, land, and disarm.

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

Create a non-overwriting candidate from a stable run with
`make qgc-gazebo-pin-candidate PROFILE_RECEIPT=... PROFILE_COMPARISON=... PROFILE_PIN_CANDIDATE=...`.
The candidate command requires matching run IDs, a stable pre/post comparison,
and complete source, artifact, and toolchain values. It writes the candidate
with review status `unreviewed`; it cannot make a verified pin file.

Inspect the candidate's revisions, dirty-state hashes, artifacts, and toolchain
against the source and build review. Then explicitly set its `review` object to
`{"status":"reviewed","reviewer":"...","reviewedAtUtc":"...Z"}` and
save it as the reviewed pin file. Compare a captured interactive receipt using
`make qgc-gazebo-verify PROFILE_RECEIPT=... PROFILE_PINS=...`.
The pin file uses contract `damn-vulnerable-drone.interactive-profile-pins`
version `1.0.0`, and contains `profile`, `source`, `artifacts`, and `toolchain`
sections copied only after a reviewer accepts those values. Dirty source pins
must include the exact worktree-status, tracked-diff, and dirty-file digests.
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
