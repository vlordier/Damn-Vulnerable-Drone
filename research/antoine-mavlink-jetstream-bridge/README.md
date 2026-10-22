# Antoine MAVLink ↔ NATS bridge

This Rust bridge is versioned with the drone security lab. It adapts a bounded
MAVLink common-dialect surface to the native Antoine software SITL through
Core NATS and stores command-frame evidence in JetStream. It runs on loopback
only. It is not a hardware flight controller, and it does not bypass the
Gazebo profile's Core/Vigie authority boundary.
Callsigns used in NATS subjects are restricted to 1–64 ASCII letters, digits,
hyphens, or underscores so a vehicle identity cannot introduce subject
separators or wildcards.

The default `udp` mode relays MAVLink frames between local UDP peers and audits
accepted ingress. The `antoine-nats` mode converts native model telemetry into
MAVLink `HEARTBEAT`, `SYS_STATUS`, `GLOBAL_POSITION_INT`, `GPS_RAW_INT`, and
`HOME_POSITION` frames, and maps supported GCS controls to Antoine's NATS
control subject. `SYS_STATUS` advertises only the model pre-arm check; its
health bit is true only when ground contact, GNSS, battery, altitude, velocity,
and thrust meet the local readiness bounds.
Inbound controls are pinned to `--qgc-peer` by default. The optional
`--qgc-control-peer` explicitly allows a second loopback acceptance client;
its frames are audited with their source address. The optional
`--qgc-telemetry-peer` remains telemetry-only.

The opt-in `gazebo-nats` mode uses the same MAVLink codec and telemetry
projection, but sends operator requests to the run-owned Gazebo local-authority
process. That authority validates each request and signs the grant and setpoint
consumed by the normal Antoine Gazebo controller. The bridge cannot mint
authority envelopes or publish actuator commands. This is a loopback research
authority, not Core/Prime/Vigie authority. This mode requires `--run-id` and
copies it into every JetStream MAVLink frame envelope so status and acceptance
can reject cross-run audit records.
In `udp` mode, JetStream read-only vehicle requests also require an exact
`runId` match whenever `--run-id` is configured; requests without one remain
compatible only when the bridge itself has no selected run ID.

The supported control surface includes ARM/DISARM, takeoff, LAND, RTL, mode
changes (STABILIZE, GUIDED, AUTO, HOLD, RTL, LAND), bounded local-NED position
targets, and bounded local-NED waypoint missions (upload, readback, execution).
The bridge audits GCS frames before applying controls. Position and mission
targets are constrained to the software SITL envelope; GUIDED targets expire
when refreshed setpoints stop. See
[`../qgroundcontrol-antoine.md`](../qgroundcontrol-antoine.md) for commands and
the full acceptance run.

## Build and test

From the repository root:

```sh
make bridge-build
make bridge-test
```

These use the checked-in `Cargo.lock` and build artifacts under
`/tmp/dvd-antoine-bridge-target`. To run the bridge directly:

```sh
cargo run --locked --manifest-path research/antoine-mavlink-jetstream-bridge/Cargo.toml -- \
  --mode antoine-nats --callsign antoine-sitl \
  --nats-url nats://127.0.0.1:4222 \
  --qgc-bind 127.0.0.1:14651 --qgc-peer 127.0.0.1:14550 \
  --system-id 1 --home-alt-msl 82
```

The native Antoine SITL and NATS server remain separate local services. Start Antoine
with `--plant antoine --enable-mavlink-control`; the control opt-in is rejected
for the Gazebo plant. Keep the bridge and controller callsigns aligned and use a
loopback NATS URL. For the interactive Gazebo profile, run `make qgc-gazebo`;
it starts a run-owned broker, ground-start Gazebo world, Antoine controller,
local test authority and this bridge, then opens the local QGC build if needed.
The authority waits for ARM before issuing its first flight grant. Stop the
profile with Ctrl-C; it cleans up only its run-owned processes. For a live
isolated protocol check that starts its own NATS,
SITL, and bridge, run `make mavlink-flight-check`. For QGC ingress plus
JetStream audit on the interactive bench, run `make qgc-antoine-check`.

The protocol checks verify mode, takeoff, position, mission upload/readback and
execution, RTL, landing, disarm, acknowledgements, and durable audit records.
The QGC startup check proves QGC heartbeat/request ingress; an exact mode change
initiated by clicking the QGC UI is a separate GUI acceptance check. All flight
validation is software simulation only. Never connect this adapter to a real
aircraft or a non-loopback peer.
