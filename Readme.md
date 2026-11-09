# Damn Vulnerable Drone

An intentionally vulnerable drone hacking simulator for offensive security training.

The native flight profile runs [Antoine](https://github.com/antoine-flight-controller), a Rust flight controller, with Gazebo Harmonic for 3D simulation and [NATS](https://nats.io/) for messaging. Legacy Docker profiles provide the original [ArduPilot](https://ardupilot.org/)/MAVLink lab.

## Architecture

```text
┌─────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  Antoine (Rust) │────▶│     NATS         │────▶│  Gazebo Harmonic │
│  Flight Ctrl    │◀────│  Message Broker  │◀────│  3D Simulator    │
└─────────────────┘     └──────────────────┘     └──────────────────┘
         │                                                       │
         ▼                                                       ▼
┌─────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  MAVLink Bridge │     │ Research Console │     │  QGroundControl  │
│  (NATS ↔ MAV)   │     │  (Flask Web UI)  │     │  GCS Interface   │
└─────────────────┘     └──────────────────┘     └──────────────────┘

Legacy Docker paths (ArduPilot/MAVLink):
  flight-controller ── companion-computer ── ground-control-station
       │                       │                       │
    (MAVLink)             (ROS 2 / Wi-Fi)         (QGC / MAVProxy)
```

## Quick Start

### Prerequisites

```bash
make doctor    # check Antoine/Gazebo/NATS prerequisites
```

### Native Antoine Profile

```bash
# Build Rust components and sync Python deps
make antoine-gz-battery-patch
make antoine-gz-battery-test

uv sync --project research

# Run bounded flight proofs
make baseline           # single-drone Antoine baseline
make swarm-flight       # two-drone swarm with separation evidence
make swarm-live         # same flight with Gazebo 3D GUI
make attack-waypoint    # bounded waypoint-injection scenario

# Interactive GCS
make qgc-gazebo         # QGroundControl + Gazebo 3D
```

### Legacy Docker Profiles

```bash
# Full mode (3D Gazebo, GPU required)
docker compose -f docker-compose.yaml build
sudo ./start.sh --mode full

# Lite mode (2D simulation, no GPU)
docker compose -f docker-compose-lite.yaml build
sudo ./start.sh --mode lite

# Stop everything
sudo ./stop.sh
```

## Features

- **Antoine Rust Flight Controller** — Software-in-the-loop simulation with Gazebo Harmonic, NATS state streams, and bounded flight proofs with signed evidence
- **Swarm Simulation** — Scripted formation flights (lanes, V, rendezvous, split) with per-vehicle separation analysis and Gazebo pose evidence
- **MAVLink ↔ NATS Bridge** — Bidirectional bridge for telemetry fan-out, arm/disarm commands, and GCS-style control through a local JetStream topic
- **Research Console** — Flask web interface indexing attack scenarios, findings, and run evidence with scenario catalog validation
- **Legacy ArduPilot/MAVLab** — Docker-based simulation with companion computer (ROS 2 Humble/Foxy, Wi-Fi AP, mavlink-router) and Ground Control Station (QGroundControl)

## Project Structure

```diff
├── flight-controller/          # ArduPilot SITL container (legacy)
│   ├── Dockerfile              # Copter-4.4 SITL build
│   └── lite/Dockerfile         # Lightweight 2D variant
├── companion-computer/         # ROS 2 + Wi-Fi AP + mavlink-router
│   ├── Dockerfile              # Ubuntu 22.04 / ROS 2 Humble
│   └── interface/              # Flask web UI (flight control, WiFi, camera)
├── ground-control-station/     # QGroundControl + tools
│   └── Dockerfile              # Ubuntu 24.04
├── simulator/                  # Gazebo + gzweb + management
│   ├── Dockerfile              # Gazebo 11 + ROS 2 Foxy
│   ├── mgmt/                   # Research console (Flask)
│   └── gzweb/                  # Web-based Gazebo visualization
├── research/                   # Native Antoine profile
│   ├── antoine-mavlink-jetstream-bridge/  # Rust MAVLink↔NATS bridge
│   ├── mavlink-nats-console/              # Rust telemetry console
│   ├── dvd_research/                   # Python research package
│   └── console.sh                    # Evidence console launcher
├── scripts/                    # QGC+Antoine launcher helpers
├── .github/workflows/          # GitHub Actions CI
├── Makefile                    # Swarm, bridge, console, lint, validate
└── docker-compose*.yaml        # Legacy Docker profiles
```

## Lint & Validate

```bash
make lint              # Python, shell, data, Docker, JS, Rust, C++ checks
make validate          # Lint + scenario catalog validation
make lint-rust         # cargo fmt --check + cargo clippy (local Rust tools)
```

## Attack Scenarios

Native Antoine/NATS scenarios are listed in the research console at
`/attacks/research`. Legacy guides target MAVLink, Wi-Fi, ROS 2, and MAVProxy
components. See the Antoine scenario crosswalk for current coverage.

## Development

```bash
# Research Python environment
uv sync --project research

# Rust lint (requires Cargo)
cargo clippy --all-targets --all-features \
  --manifest-path research/antoine-mavlink-jetstream-bridge/Cargo.toml

cargo clippy --all-targets --all-features \
  --manifest-path research/mavlink-nats-console/Cargo.toml
```

## Disclaimer

This platform is provided solely for educational and research purposes. Users are expected to adhere to ethical hacking principles, respecting privacy and laws, and must not use skills or knowledge acquired from Damn Vulnerable Drone for malicious activities.

## License

MIT — see [LICENSE](LICENSE) for details.
