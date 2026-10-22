# Antoine/Gazebo swarm threat model

Version: 1.1.0
Scope: the local two-to-five vehicle profile launched by
`make swarm-flight` / `research/run-antoine-two-drone-flight.sh`.

This document describes source-backed trust boundaries for the current local
SITL. It does not claim the swarm has dynamic formation control or collision
avoidance. The runner computes each vehicle's waypoint route before launch and
passes a fixed route to each local authority.

## Attacker and operating limits

The experiment assumes an attacker can run a client on the same development
host and reach the run-owned NATS listener on `127.0.0.1`. The profile does not
configure NATS user credentials or per-subject ACLs. A callsign-specific subject
therefore provides routing and separation of application state, not broker
authorization. The attack surface is limited to a disposable local SITL run;
the runner must never target shared Furia/Core NATS, a non-loopback broker, or a
real aircraft.

The threat model excludes host administrator/kernel compromise, theft of the
test authority's signing seed from the same user's process/files, Gazebo binary
compromise, radio links, and physical sensors. It does include malformed,
replayed, stale, wrong-callsign, wrong-system-ID, and cross-vehicle messages
sent through documented local interfaces. A case is unavailable when the
current profile exposes no such consumer; do not add an emulator and call that
an Antoine vulnerability.

## Runtime components and trust boundaries

| Interface | Producer → consumer | Existing binding/check | Security meaning and gap |
|---|---|---|---|
| Gazebo pose transport → `furia.gzsitl.state.<callsign>` | Per-vehicle C2/Gazebo bridge → Antoine controller and local authority | Subject and JSON identity are callsign-scoped; payload includes simulation time and pose | Loopback NATS has no per-callsign ACL. Callsign/identity are not cryptographic source authentication. A same-host broker client can attempt forged state publication; effect on control must be measured separately. |
| `furia.prime.execution-grant.<callsign>` | Local test authority → matching Antoine controller | Ed25519 grant verification binds vehicle, run, mission/task and expiry to the controller's configured public key | This is a software test authority whose private seed is created for the local run. It is not Core/Prime production authorization. Wrong vehicle/run or invalid signature should fail closed. |
| `furia.gzsitl.setpoint.<callsign>` | Local authority → matching Antoine controller | Setpoint parser checks schema, expiry, vehicle identity, increasing sequence, and grant/task binding where the typed grant is used | Topic naming is not authorization. The controller's signature/grant checks are the relevant gate. The runner must correlate any acceptance with controller decision and independent actuator/pose evidence. |
| `furia.prime.execution-revoke.<callsign>` | Local authority → matching controller | Signed revoke is checked against the active grant identity/sequence before cached authority is removed | Revocation response timing and cross-vehicle isolation need explicit measurement; a revoke publication by itself is not evidence of neutral actuators. |
| `furia.gzsitl.commands.<callsign>` | Controller → local boundary/applier path | Run-owned boundary process has callsign and durable per-vehicle actuator-sequence state; actuator request validation binds authority/grant context | NATS delivery/audit is not proof of actuation. Use the raw actuator audit and Gazebo pose as separate evidence. The local run does not provide hardware safety certification. |
| `furia.telemetry.<callsign>` | Antoine controller → observers | Callsign-scoped JSON telemetry | Plain local NATS visibility is expected for same-host observers. Treat it as telemetry disclosure inside the declared local trust boundary, not remote exposure. |
| MAVLink UDP bridge in swarm probe | Per-callsign bridge → read-only UDP probe | Runner allocates distinct loopback bind/peer ports and system IDs; probe checks frame CRC, callsign socket and observed system ID | System ID is an identifier, not authentication. The probe sends no MAVLink commands. In `antoine-nats` mode the bridge's control subject is not consumed by the Gazebo plant; this is telemetry fan-out only, not a swarm command route. |

The subject names above are constructed in Antoine's
`antoine_sitl_nats.rs` and `antoine_gz_local_authority.rs`. The multi-drone
launcher binds its JetStream-enabled NATS server to `127.0.0.1`, allocates one
controller, boundary service, and local authority per callsign, and stores
actuator sequence state under the unique run directory. These process and
subject boundaries are evidenced in `~/Work/antoine/scripts/gz_flight_proof.sh`
and `research/run-antoine-two-drone-flight.sh`.

Existing source-level gate evidence was re-run for this inventory: Antoine's
`mission_setpoint_parser_requires_fresh_bound_fields_and_vehicle` and
`sequential_typed_setpoint_must_match_the_exact_grant_task_binding` unit tests
both pass with the `nats` feature. These tests establish parser/task-binding
behavior; they do not qualify multi-vehicle isolation at runtime. The runtime
positive and negative swarm controls remain tracked by `dvd-fgt.16.2.3` and
`dvd-fgt.16.2.4`.

## Identity and isolation expectations

- The active roster is `drone-1` through `drone-N`, where N is 2–5. Callsigns
  and controller instances are unique within one run.
- Each local authority uses the matching callsign as `vehicleId` and the
  generated run ID as `runId`. The controller verifies grants against its own
  callsign and a configured public key.
- The runner assigns MAVLink system IDs 1..N and distinct UDP ports. Those IDs
  and ports are transport routing checks only; they do not establish command
  authorization.
- A wrong callsign, stale run/task, expired grant, or stale/non-increasing
  setpoint must not change any vehicle's actuator output or Gazebo trajectory.
  The current swarm MAVLink bridge has no Gazebo command consumer, so wrong
  system-ID command injection is unavailable on this profile; do not count its
  absent route as a tested rejection. Positive controls must move only the
  selected vehicle when a real authorized consumer exists.
- If the test cannot observe rejection/acceptance at the controller and
  independently observe every vehicle's pose and actuator state, classify the
  result inconclusive.

## Current behavior and limits

`research/swarm_behavior.py` creates fixed ENU waypoints for lanes, column,
vee, rendezvous, and split. The launcher writes that plan into the run evidence
then passes per-vehicle waypoints to the local authorities. Antoine follows
signed setpoints as each route progresses. There is no identified live
formation/consensus controller in this profile, and no S1/ORCA3D feedback loop
or runtime collision-prevention guarantee. Pair separation measured from
sampled trajectories is useful experimental evidence, but it does not prove a
preventive safety mechanism.

Safe-stop timing in flight-evidence version 1.2.0 uses a monotonic clock local
to each authority process. Stop and recovery events include that clock source
and elapsed milliseconds; each recovery names the exact hold start. The swarm
evaluator requires their monotonic duration to agree and remain at most 2000 ms.
Unix timestamps remain available to correlate independent applier records;
they do not supply the hold-duration measurement. Missing clock identity,
backwards monotonic events, mismatched hold starts, stale or reordered applier
rows, and intervening authorized commands invalidate transition qualification.
Older wall-time-only stop evidence cannot qualify this gate when re-evaluated.
The default launcher applies `research/patches/antoine-flight-lifecycle.patch`
only inside its run-owned Antoine snapshot. The receipt records the original
source digest, patch digest and application status, and resulting source file
digests. An already-patched checkout is accepted without applying the change
twice; an incompatible revision fails before launch. The shared checkout is
not edited by this step.

The automatic flight profile uses an explicit armed latch in the sole Gazebo
actuator applier. The local authority signs versioned
`furia.gzsitl.actuator-lifecycle` requests on the existing command subject.
Requests bind run, vehicle, frame, a separate increasing lifecycle sequence,
and a 250 ms validity window. The applier rejects actuation while disarmed.
Grant expiry, revocation, watchdog zeros, and transient stop/hold commands do
not change that latch. Disarm requires a signed request naming the latest
explicit-stop sequence, with no accepted actuator command after that stop.
The bounded authority handshake retries at most ten times over about two
seconds; absent acknowledgement prevents arm or leaves disarm unqualified.

`applier-neutral/actuator-lifecycle.jsonl` and each vehicle's atomic
`*.armed-state.json` record the authoritative transitions. Evaluation checks
their identity and ordering, verifies request signatures against the run's
authority public key, and correlates final disarm with independent motor and
drive stop rows. The acknowledgement must be at most five seconds old when
the authority captures it, and its referenced stop at most fifteen seconds
old. A later authorized command invalidates disarm evidence. This is a software
actuator latch in the local simulator, not a physical landing or motor-health
claim. Interactive operator-control profiles do not yet use this lifecycle.

`research/patches/gazebo-actuator-lifecycle.patch` is applied to run-owned
applier source and compiled before launch. The Gazebo snapshot archives that
source, the patch, the build receipt and resulting executable digest. Shared
Gazebo source and binaries are not overwritten by this build.

The local attitude cascade limits its natural frequency to 0.08 times the
configured control rate (2 rad/s at 25 Hz), retaining critical continuous-time
damping. This provides margin for sampled pose differentiation, the 0.5 rate
filter, transport and motor delays. A linearized pinned-inertia roll regression
exposes instability with the earlier 4 rad/s gains under 40–80 ms sensor and
command delays and checks convergence with the capped gains. That model is
limited to small-angle attitude response; full route qualification still
requires independent live Gazebo kinematic dwell and separation evidence.
Explicit torque/rate gain overrides are recorded in the public run profile.

For Antoine revisions with the native SITL build-receipt verifier, the launcher
builds and verifies through that upstream tool in clean, task-owned Git source
bundles. Snapshotting follows local Cargo path dependencies and retains shared
contract conformance fixtures. Original snapshots remain byte-verified; local
packaging commit IDs are recorded separately in `native-source-git.json`.
Archives copy only receipted source and metadata, excluding Git directories
and build caches. The native receipt is retained and its digest and actual
commands are bound to the lab's run-build receipt. Native receipt checks remain
enabled before launching the captured binaries.

Swarm report version 1.12 also requires successful flight proofs and at least
1 m pairwise separation before labeling a baseline qualified. A measured
kinematic dwell failure is a failed behavior, even if every positional waypoint
was reached. Missing or unusable pose measurements remain inconclusive.

The `state-stream-timing` mode observes message receive timing without
publishing. `state-stream-partition` inserts a run-local relay between the
original per-vehicle Gazebo state subject and a controller-only subject, then
drops a bounded interval for one callsign. The independent local authority stays
on the original source subject. The controller enters a zero-velocity watchdog
hold and recovers after relay delivery resumes; the hold is not actuator
neutralization. The older `controller-state-blackhole` name remains an alias.
Evidence is inconclusive without timestamped authority progress during the
fault, sequence and simulation-clock recovery, synchronized per-vehicle pose and
actuator windows, and a measured separation floor. This is an application-edge
simulation fault, not shared-network or broker packet loss.

## Evidence needed to advance the model

1. Establish a synchronized clean baseline with per-callsign controller/run
   identity, Gazebo poses, actuator state, sample freshness, and minimum
   pairwise separation over time.
2. Exercise one wrong-callsign/system-ID/run/task request at a time and show
   the expected reject at the relevant gate, no cross-vehicle actuation, and a
   safe disarmed finish.
3. Exercise one valid authorized command and show only its target vehicle
   responds; keep all other vehicles inside their declared baseline tolerance.
4. Suppress or delay one run-owned state stream for a hard-capped interval,
   retain an independent watchdog, and measure controller response, neighbor
   trajectories, neutralization, and recovery.
5. Preserve provenance for the Antoine, Gazebo, simulation assets, bridge,
   profile, generated routes, toolchain, and all executables used by each run.

On the current launcher, one fresh wrong-callsign and one validly shaped
wrong-task request have run-bound controller decisions plus independent
three-window pose and actuator evidence. A bounded `wrong-target` mutation is
also available: it shifts one ENU target component by 2 m, advances the
sequence for freshness, and requires controller rejection before the active
setpoint advances. The run-owned launcher now applies a typed mission-setpoint
v2.2 extension carrying `runId`; the controller requires it to match the
active signed execution grant. The bounded stale-run mutation is implemented
but still needs qualifying live evidence. The selected MAVLink bridge only
fans telemetry out; it has no command ingress where a wrong system ID could
be exercised.

These requirements map to `dvd-fgt.16.1.4`, `dvd-fgt.16.2.3`,
`dvd-fgt.16.2.4`, and `dvd-fgt.16.3.2`. The existing
`research/mavlink_swarm_probe.py` is telemetry-only evidence and does not
satisfy command-isolation acceptance.

## Live avoidance seam assessment

Assessment of the qualified source snapshot `3a310e394ad8c10f3eae7f3de1878e803df900d06ee815786a9fd881e440f352`: **unsupported in the current automatic swarm launcher**. This is an architecture result, not an avoidance flight pass.

The available state source is each vehicle's `furia.gzsitl.state.<callsign>`; independent Gazebo pose capture supplies experiment truth. `antoine_sitl_nats.rs` subscribes to its own state, mission setpoint, grant and admission. Its `furia.sitl.s1.demo-estimator.<callsign>` subscription is a demo-state input, not a launched neighbor-state avoidance service. The launcher does not start a neighbor-state aggregator or S1/ORCA avoidance authority. `research/swarm_behavior.py` generates fixed routes before launch.

The existing command interface is `furia.gzsitl.setpoint.<callsign>`, but a new position must agree with the signed grant's exact target, task/frame and admission. The automatic local authority owns the fixed route and signs its current waypoint. Publishing an avoidance position alone cannot authorize it. Its optional operator surface translates commands into signed envelopes, but the automatic multi-vehicle profile does not enable that interactive interface. Thus the specific missing seam is an authorized, run/callsign-bound online waypoint replacement in the automatic authority, coupled to fresh neighbor state. Direct actuator publication would bypass the boundary under investigation.

Timing constraints include the configured 25 Hz control loop, 75 Hz requested plant state stream, maximum 2 s grant window, 8 s admission window with 5 s rotation and 400 ms admission/setpoint lead. These are existing protocol limits, not a measured neighbor-response deadline. Transition qualification permits at most 2 s of precisely bound neutral hold; the successful baseline's much shorter holds do not establish avoidance response latency. Missing/stale/mismatched authority inputs fail closed to neutral actuator output. Neutralization is not hovering or controlled landing and can lose altitude; explicit signed disarm is a separate final lifecycle event.

The smallest upstream extension is a bounded automatic-authority input for a replacement target, with run/callsign/frame/task binding, monotonic sequence and expiry, allowed-region/acceleration limits, and a receipt tying the accepted target to the newly minted grant/setpoint/admission. A neighbor consumer must reject stale, reordered and cross-run samples and declare its response deadline and loss policy before flight. Then a separate experiment can predeclare two converging tracks, a minimum separation and response deadline, demonstrate a target change before conflict, measure both independent pose streams and every signed command, and repeat with neighbor loss. Do not run a deliberate conflict and label ordinary separation or emergency motor neutralization as avoidance while this seam is absent.
