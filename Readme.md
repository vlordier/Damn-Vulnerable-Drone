# Damn Vulnerable Drone

The Damn Vulnerable Drone is an intentionally vulnerable drone hacking simulator. Its native flight profile runs Antoine, a Rust flight controller, with Gazebo Harmonic and NATS. The legacy Docker profiles still provide the original ArduPilot/MAVLink lab.

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/Damn-Vulnerable-Drone-Banner.png?raw=true" alt="Damn Vulnerable Drone Logo"/>
</p>

# Table of Contents

* [About the Damn Vulnerable Drone](#about-damn-vulnerable-drone)
  * [What is the Damn Vulnerable Drone?](#what-is-the-damn-vulnerable-drone?)
  * [Why was it built?](#why-was-it-built?)
  * [How does it work?](#how-does-it-work?)
  * [Features](#features)
* [Installation](#installation)
  * [System Requirements](#system-requirements)
  * [Getting Docker](#getting-docker-key-dependency)
  * [Wi-Fi Mode](#wi-fi-mode)
  * [Non-Wi-Fi Mode](#non-wi-fi-mode)
* [Architecture](#architecture)
* [Flight States](#flight-states)
* [Attack Scenarios](#attack-scenarios)
* [Screenshots](#screenshots)
* [Mentions](#mentions)
* [Community Support](#community-support)
* [Feedback & Contributions](#feedback-&-contributions)
* [Maintainers](#maintainers)
* [Credits](#Credits)
* [Disclaimer](#disclaimer)
* [License](#license)

# About the Damn Vulnerable Drone

<p align="center">
  <a href="https://www.youtube.com/watch?v=EHTQv6IfnwI"><img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/demo.png?raw=true" alt="Damn Vulnerable Drone Demo"/></a>
</p>

## What is the Damn Vulnerable Drone?

The Damn Vulnerable Drone is a virtual environment for safely studying drone security. Its native profile runs the Rust-based Antoine controller with Gazebo Harmonic and NATS. Separate legacy profiles retain the original [ArduPilot](https://ardupilot.org/) and [MAVLink](https://mavlink.io/en/) lab.

## Why was it built?

The Damn Vulnerable Drone aims to enhance offensive security skills within a controlled environment, making it an invaluable tool for intermediate-level security professionals, pentesters, and hacking enthusiasts.

Similar to how pilots utilize flight simulators for training, we can use the Damn Vulnerable Drone simulator to gain in-depth knowledge of real-world drone systems, understand their vulnerabilities, and learn effective methods to exploit them.

The Damn Vulnerable Drone platform is open-source and available at no cost and was specifically designed to address the substantial expenses often linked with drone hardware, hacking tools, and maintenance. Its cost-free nature allows users to immerse themselves in drone hacking without financial concerns. This accessibility makes the Damn Vulnerable Drone a crucial resource for those in the fields of information security and penetration testing, promoting the development of offensive cybersecurity skills in a safe environment.

## How does it work?

The native profile uses software-in-the-loop simulation. Antoine runs as a local controller, while Gazebo provides the simulated aircraft and environment and NATS carries local control and state messages. The legacy Docker profiles continue to provide the older ArduPilot/MAVLink architecture.

While the current Damn Vulnerable Drone setup doesn't mirror every drone architecture or configuration, the integrated tactics, techniques and scenarios are broadly applicable across various drone systems, models and communication protocols.

## Features

- **Multiple Local Profiles**: Native Antoine/Gazebo/NATS profile plus legacy Docker-based ArduPilot modes.
- **Simulated Wireless Networking**: Available in the legacy Docker profiles only; the native Antoine profile is loopback-only and has no Wi-Fi network.
- **Camera Streaming & Gimbal**: Legacy Docker simulation features; the native Antoine X500 declares a camera sensor, but its current headless flight profile does not expose a camera stream or gimbal.
- **Companion Computer Web Interface**: Available in the legacy Docker profiles only.
- **QGroundControl/MAVProxy Integration**: Available in the legacy Docker profiles; the native Antoine profile uses its own bounded flight proof.
- **MAVLink Router Integration**: Available in the legacy Companion Computer profile; it is not part of the Antoine/NATS path.
- **Flight Evidence**: Antoine native runs collect controller, actuator, and Gazebo pose evidence; legacy ArduPilot runs retain their existing logging.
- **Management Web Console**: The Docker management UI controls the legacy simulator; the native Antoine research console indexes findings and run evidence without starting or commanding flights.
- **Security Research Scenarios**: Legacy guides target their documented MAVLink, Wi-Fi, or ROS 2 stacks. Antoine/NATS scenarios are listed separately and only marked active when their local interfaces are launched.
- **Detailed Walkthroughs**: If you need help hacking against a particular scenario you can leverage the detailed walkthrough documentation as a spoiler.

# Installation

Please review the following instructions carefully to ensure a stable and well performing Damn Vulnerable Drone lab environment.

> [!NOTE]  
> Depending on your computer’s performance and internet speed, the full end-to-end installation process including container builds and image pulls, can take between *30-60 minutes*. Be patient and let each step complete before moving forward. 

## System Requirements

Damn Vulnerable Drone can be run in two modes depending on your available hardware:

### Lite Mode (No GPU Required)

- **Operating System:**
  - **Kali Linux** (recommended, but Lite mode may run on most Linux distros)
  - Works on **bare metal** or **VM** environments without GPU passthrough

- **Hardware (Minimum):**
  - **RAM:** 4–8 GB
  - **Processor Cores:** 2
  - **Disk Storage:** 100 GB
  - **Swap:** 4 GB

- **Graphics:**
  - No GPU required  
  - Runs a **2D lightweight simulator** using ArduPilot’s built-in flight dynamics model instead of Gazebo  

- **Software:**
  - **Docker**
  - **Docker Compose**

Lite Mode is recommended for users who cannot meet the GPU requirements of Full Mode or want a faster, more lightweight setup.

### Lite Mode Interface

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/Lite-Mode.png?raw=true" alt="Damn Vulnerable Drone Lite Mode"/>
</p>

---

### Full Mode (3D Environment with Gazebo)

- **Operating System:**
  - **Kali Linux** (no other OS is supported)
  - For best performance, use **bare metal**
  - If you must use a VM, ensure **GPU passthrough** is enabled  
    - See [this guide on Hyper-V GPU passthrough](https://techcommunity.microsoft.com/t5/virtualization/bg-p/Virtualization) for setup help

- **Hardware (Minimum):**
  - **RAM:** 8–16 GB
  - **Processor Cores:** 2–4
  - **Disk Storage:** 100 GB
  - **Swap:** 10–12 GB

- **Graphics:**
  - At least 2 GB VRAM (4 GB+ recommended)
  - OpenGL 3.0 (or higher)
  - Dedicated GPU strongly recommended
  - For VMs: Enable **GPU passthrough** and **3D acceleration**

- **Software:**
  - **Docker**
  - **Docker Compose**

### Full 3D Mode Interface

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/screenshot-1.png?raw=true" alt="Damn Vulnerable Drone Full Mode"/>
</p>

---

## Getting Docker (Key Dependency)

The following instructions are meant to be executed on the latest version of [Kali Linux](https://www.kali.org/), please also follow the official kali guide on how to install docker [here](https://www.kali.org/docs/containers/installing-docker-on-kali/)

## Clone the repository

`git clone https://github.com/nicholasaleks/Damn-Vulnerable-Drone.git && cd Damn-Vulnerable-Drone`

---

## Native Antoine Flight Profile

### Quick commands

From the repository root, run `make` to see the common lab commands. The
shortest swarm workflow is:

```bash
make doctor           # check external checkouts and required commands
make swarm-smoke       # wiring check; no controller flight
make swarm-flight      # two-drone flight and separation evidence
make swarm-live        # same flight with Gazebo's 3D GUI
make swarm-vee         # scripted V formation (also rendezvous/split)
make swarm-bridge      # one MAVLink telemetry bridge per drone
make test-swarm        # planner, evaluator, and MAVLink probe tests
make swarm-3           # three-drone scaling run
make swarm-5           # five-drone scaling run
make swarm-blackhole   # bounded, controller-local state suppression
make qgc-gazebo        # interactive QGC ↔ MAVLink ↔ NATS ↔ Antoine ↔ Gazebo
make qgc-gazebo-status PROFILE_RECEIPT=/path/to/receipt.json
make qgc-gazebo-stop PROFILE_RECEIPT=/path/to/receipt.json
make qgc-gazebo-check  # verify GCS-style frames reach the bridge audit
make mavlink-gazebo-flight-check # exercise controls and verify Gazebo pose truth
make attack-waypoint   # one bounded setpoint mutation
make attack-replay     # one bounded setpoint replay
make observe-nats      # read-only, capped NATS observation
make probe-gps         # signed-GPS tampering probe
```

The fault target defaults to `drone-2`, starts after 30 seconds, and lasts two
seconds. Change the bounded fault with `ANTOINE_SWARM_FAULT_CALLSIGN`,
`ANTOINE_SWARM_FAULT_AFTER_MS`, and `ANTOINE_SWARM_FAULT_SECONDS`; this is a
controller-consumer experiment, not NATS packet loss. Set
`ANTOINE_SWARM_SPACING_M` to change lane spacing, or run
`make swarm-flight ANTOINE_SWARM_SIZE=4` for a four-drone flight.

For a selected-vehicle positive-control run, set
`ANTOINE_SWARM_AUTH_POSITIVE_TARGET=drone-2` (and optionally
`ANTOINE_SWARM_AUTH_POSITIVE_DELTA_M=2`). The runner adds one separated waypoint
to that vehicle's fixed local-authority route and records the exact grant and
actuator target. This remains a planned mission command, not an interactive
QGroundControl or live NATS control path. The report separately checks target
route progress, target pose, and peer stability; unsuccessful or ambiguous pose
evidence does not qualify the positive-control bead. Qualified run
`antoine-swarm-flight-20261020T211214Z-24266` measured 1.852 m of progress from
the selected vehicle's prior waypoint, 0.227 m target error, and 0.082 m peer
drift, with signed final disarm states and an integrity-verified evidence
manifest. The authority's simulation clock differs from Gazebo's world clock;
the report uses Gazebo pose time for physical effect and binds it to the matching
run, callsign, signed grant sequence, and actuator records. See
`research/findings.md`.

`ANTOINE_SWARM_FAULT=state-stream-timing` remains a read-only receive-timing
observation. `make swarm-blackhole` runs a bounded controller-only state-stream
partition: a run-local relay drops one callsign's forwarded state messages while
the local authority continues to observe the original Gazebo state subject.
The controller watchdog enters and exits zero-velocity hold; the report binds
the sequence and simulation-time gap to per-vehicle Gazebo poses and actuator
windows, authority progress during the fault, and minimum separation. Run
`antoine-swarm-flight-20261020T213831Z-70888` dropped 111 messages over 2.011 s,
recovered at the next source sequence, retained 8.114 m minimum separation,
passed both flight proofs and the qualified baseline gates, and verified its
evidence manifest. This is an application delivery-edge fault in local
simulation; it does not model shared-network packet loss or claim hardware
safety or actuator neutralization during the watchdog hold.

Swarm behaviours are fixed per-vehicle waypoint plans chosen before launch with
`ANTOINE_SWARM_BEHAVIOR=lanes|column|vee|rendezvous|split`. The runner writes the
plan to `research-swarm-plan.json`, launches one independent local authority per
vehicle, and then measures the final Gazebo poses against that plan. Non-lane
behaviours fly a three-point path: climb to a per-vehicle transit altitude layer,
cross lanes at that layer, then descend at the separated final slot. The planner
records a synchronized path-clearance estimate and rejects plans below a 1 m lab
floor. That swarm profile is static scheduling, not dynamic collision
avoidance. The runner derives its
flight-time budget from the longest planned path. At the default 8 m spacing,
final slots are at least 5 m apart, matching S1's default minimum separation,
but this profile does not start S1 or enforce its ORCA3D avoidance.
`make swarm-vee`, `make swarm-rendezvous`, and `make swarm-split` are shortcuts.

Automatic flights explicitly arm and disarm a software latch in the sole
Gazebo actuator applier using signed, run-bound lifecycle requests. The final
report verifies the requests and independent state/stop audits; revoked grants
or zero motor outputs alone never count as disarm. Each run builds the patched
applier and Antoine from isolated source snapshots and archives their build
inputs. This lifecycle measures the simulator's actuator authority, not a
physical landing or hardware motor state.

Set `ANTOINE_SWARM_MAVLINK=1` (or run `make swarm-bridge`) to start one local
MAVLink bridge per callsign and verify per-vehicle telemetry fan-out, distinct
system IDs, and checksummed frames at `research-mavlink-swarm.json`. The bridge
is read-only in the swarm path: it observes MAVLink telemetry and does not give a
GCS control of the multi-drone flight. For interactive control of a single
Gazebo X500, run `make qgc-gazebo`; this starts a separate run-owned profile and
opens Gazebo with the Antoine flight scene and QGroundControl, then routes bounded MAVLink
commands through Antoine's local test authority. Set `ANTOINE_GZ_FLIGHT_GUI=0`
to disable the Gazebo window. The profile
supports arm, takeoff, modes, position targets, missions, RTL, land and disarm,
with Gazebo poses as independent flight evidence. This is a software simulation
using a local test authority, not Core-authorized or hardware flight. See
[`research/qgroundcontrol-antoine.md`](research/qgroundcontrol-antoine.md).

Use
`make console-start`, `make console-status`, and `make console-stop` for the
local evidence console. `make validate` runs the focused lint and
scenario-catalog checks; it does not start simulations.

`make swarm-live` opens Gazebo's 3D client on the same private run partition
as the two-drone server. Orbit or pan the scene while both X500s follow their
separate scripted paths. For three or five aircraft, run
`make swarm-live ANTOINE_SWARM_SIZE=3` or `=5`. The terminal prints the run's
evidence directory; keep the run open until the flight completes. Each run
also saves a top-down trajectory SVG and raw Gazebo pose samples.

To review completed flights, run `make console-start`, then open
<http://127.0.0.1:8001/attacks/research>. Swarm paths are shown on the page and
can be opened full size with **View swarm trajectory**. The SVG is a static
X/Y plot; use the live Gazebo view to see the 3D flight. Stop the evidence
console with `make console-stop` when done. On a headless host, run
`make swarm-flight` and review its saved trajectory in the console. To give a
slower visual run more time, use `make swarm-live SWARM_LIVE_SECONDS=90`.

On the configured development host, start the Antoine Gazebo/NATS flight proof with:

```bash
./start.sh --mode antoine
```

This also starts the loopback-only research console at
<http://127.0.0.1:8001/attacks/research>. The console stops when the native
flight launcher exits. To inspect saved evidence outside a flight, run
`./research/console.sh start`, then `./research/console.sh stop`;
`./status.sh` reports its state, and `./stop.sh` stops it safely.

This runs the host-installed Gazebo Harmonic build from `~/Work/gz-macOS` with
Antoine from `~/Work/antoine`, the `durandal-gz-swarm-sim` plant/applier, and the
Furia C2 Gazebo state bridge. It builds Antoine into this checkout's
`.cargo-target/antoine-gz` and writes run evidence under
`build_logs/antoine-gz/`. Press Ctrl-C to stop an active run; its launcher
cleans up only processes created for that run.

Override checkout locations with `ANTOINE_ROOT`, `AGR_SIM`, `AGR_C2_SCRIPTS`,
and `GZ_BIN`. The proof run uses Antoine's local test authority and a fixed
waypoint profile. It demonstrates local Gazebo motion and actuator
neutralization; it does not claim Core/Prime/Vigie authority or preserve the
old MAVLink GCS flight stages. `./status.sh` reports the active Antoine
launcher, and `./stop.sh` asks only this checkout's recorded launcher to stop.
Ctrl-C in the launch terminal remains supported.

To use the legacy ArduPilot/MAVLink lab, choose `--mode lite` or `--mode full`.
Those profiles remain separate from the native Antoine path.

The old attack guides target MAVLink, Wi-Fi, or ROS 2 components and do not
apply unchanged to Antoine. A scenario-by-scenario crosswalk and eight
local Antoine/NATS guides are under
[`research/antoine-scenario-adaptation.md`](research/antoine-scenario-adaptation.md)
and the local Attack Scenarios page. Antoine guides are labeled as runner-backed, partial coverage, or research plans in the attack index. The Antoine Research console is at
`/attacks/research`. Native attack guides are limited to the
loopback broker and Gazebo test profile; unsupported legacy components are
identified instead of being presented as active attack paths.

The initial vulnerability study results and evidence are in
[`research/findings.md`](research/findings.md). Bounded local runs are available
with `./research/run-antoine-baseline.sh`,
`./research/run-antoine-scenario.sh waypoint-injection`,
`./research/run-antoine-replay.sh`, and
`./research/run-antoine-observation.sh`. To check two separated X500 models,
their `drone-1` / `drone-2` NATS state streams against independent Gazebo world
poses, and neutral actuator startup, run `./research/run-antoine-two-drone-smoke.sh`.
The smoke retains bounded pose evidence and requires stable pre/post input
provenance. It does not start Antoine controllers or test coordinated flight.
To fly both vehicles under
separate Antoine controllers and capture per-drone flight evaluations, run
`./research/run-antoine-two-drone-flight.sh`. It defaults to two vehicles; use
`ANTOINE_SWARM_SIZE=3` or `5` for bounded scaling runs, and
`ANTOINE_SWARM_SPACING_M=6` to change parallel-lane spacing. Reports include
each vehicle's flight gates and all-pairs minimum separation. It uses local
demo authorities and does not test formation control or collision avoidance. The bounded
signed-GPS tampering probe is `./research/run-antoine-sensor-tampering.sh`; it uses a
public test key only inside its local run, verifies the source-bound forwarder receipt and reuses the
simulation checkout's canonical Cargo target directory when the receipt is
current,
and retains no signature value in the public report. The signed-GPS runner does
not load the camera renderer, because rendering reduces controller tick rate
during flight. Run the camera-only rehearsal separately with
`./research/run-antoine-camera-observation.sh`: it builds a run-local camera
world, starts no Antoine controller, uses an isolated Gazebo partition, and
captures at most five frame dimensions and payload sizes. The latest run
received five 640×360 frames on the local Gazebo Transport topic; pixel data is
discarded. This proves same-host topic readability in the camera rehearsal,
not remote, ROS 2, Wi-Fi, or GCS video exposure.
The legacy Docker management UI also indexes local evidence at
`/attacks/research`. For attack comparisons, pass a successful baseline proof report or its
`research-baseline-summary.json` to `ANTOINE_BASELINE_REPORT` when running the
injection scenario. Comparisons are marked eligible only when the physics
engine, authority scope, flight, pose, and actuator gates match and pass.
Completed runners produce redacted manifests with evidence digests; export
comparable measurements with `uv run --project research python research/export_research_csv.py`.
The `research/pyproject.toml` and `research/uv.lock` pin the host research
environment. Every runner captures source/input provenance before launch and
after the run, including the three Antoine executables and sensor-forwarder
receipt; missing or changed inputs keep classification inconclusive. The
manifest verifier reports evidence digests separately from its optional local
Ed25519 signature. Initialize a key with
`uv run --project research python research/sign_evidence_manifest.py init-key`,
then set `DVD_RESEARCH_SIGNING_KEY` before launching a runner to sign completed
manifests. Pin the generated `.pub` file with
`DVD_RESEARCH_TRUSTED_PUBLIC_KEY` for trusted verification in the CSV exporter
and management UI. Private keys stay outside run artifacts; unsigned runs are
labelled explicitly and are never described as attested.

## Pull or Build Legacy Docker Container Images

You can either pull prebuilt images from Docker Hub or build them locally.
Choose the appropriate docker-compose file depending on whether you want Full Mode or Lite Mode.

### Pull Images

**Full Mode** (with Gazebo 3D simulator):  
```
docker compose -f docker-compose.yaml pull
```

**Lite Mode** (no GPU, lightweight 2D simulator): 
```
docker compose -f docker-compose-lite.yaml pull
```

### Build Images from Source

If you’d rather build the images yourself (slower than pulling):

**Full Mode** (with Gazebo 3D simulator):  
```
docker compose -f docker-compose.yaml build
```

**Lite Mode** (no GPU, lightweight 2D simulator): 
```
docker compose -f docker-compose-lite.yaml build
```

## Operating Damn Vulnerable Drone

Damn Vulnerable Drone includes three useful bash scripts which will help you manage the state of your simulator.

#### Starting Damn Vulnerable Drone

The start script is used to start Damn Vulnerable Drone simulator. This script will automatically create a `dvd.log` log file in the project directory, which you can use to view the simulator logs. 

> [!TIP]  
> If you have not already built or pulled Damn Vulnerable Drone images the `start.sh` script will automatically pull and build them for you.

```
sudo ./start.sh -h

Usage: sudo ./start.sh [OPTION]
Start the Damn Vulnerable Drone simulator.

Options:
  --mode [full|lite]     Choose simulator mode:
                           - full: 3D environment (GPU + drivers required)
                           - lite: no GPU, minimal requirements
  --wifi  [wep|wpa2]    Start the simulation with a virtual drone Wi-Fi network.
  --no-wifi   Start the simulation with instant access to the drone network (default).
  -h, --help  Display this help and exit.

Example:
  sudo ./start.sh --wifi wpa2     # Starts with virtual Wi-Fi in WPA2 mode
  sudo ./start.sh --no-wifi   # Starts without virtual Wi-Fi
```

#### Stop

The stop script is used to perform a full cleanup of the Damn Vulnerable Drone simulator and all of its virtual interface artifacts. Stop logs are also appended to the `dvd.log` log file.

`sudo ./stop.sh`

#### Status

If you ever want to check the status of your simulator you can run the status script as shown below.

`sudo ./status.sh`

### Wi-Fi Mode 

The legacy Docker profiles' "Wi-Fi Mode" (`--wifi`) deploys a simulated wireless network between their Ground Station and Companion Computer. It exposes the `Drone_Wifi` SSID and `192.168.13.0/24` network with WEP or WPA2. It is not available in the native Antoine profile.

> [!WARNING]  
> The 10.13.0.0/24 network is used to run the simulator infrastructure, should you attack this network, especially the simulator container on `10.13.0.5` this could cause your instance of Damn Vulnerable Drone to crash, warranting a potentially lengthy rebuild. 

### Non-Wi-Fi Mode

"Non-Wi-Fi Mode" (`--no-wifi`) essentially only runs the Damn Vulnerable Drone docker containers.
Unlike "Wi-Fi Mode" you are not limited to only running "Non-Wi-Fi Mode" within a Kali Linux VM. You'll be able to practice attacking the Damn Vulnerable Drone using just the stood up containers via `docker compose up --build`

> [!IMPORTANT]  
> Not, "Non-Wi-Fi Mode" does not support wifi simulations and you will need to assume that you have an established initial access foothold on the drone data-link connection (via the 10.13.0.0/24 network)

## Architecture

The legacy ArduPilot architecture and its ground station, companion computer, and simulated Wi-Fi components run in Docker containers. The native Antoine profile instead runs Antoine, Gazebo, and NATS on the host; it does not include those legacy services. The diagrams and component table below describe the legacy Docker architecture.

Below is a high-level overview of the Damn Vulnerable Drone architecture:

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/Damn-Vulnerable-Drone-Architecture.png?raw=true" alt="Damn Vulnerable Drone Architecture"/>
</p>

| Component           | Description                                                                                                                                                              | Docker IP | Wireless IP    |
|---------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------|----------------|
| Flight Controller   | This is the brain of the drone, running the ArduPilot firmware to simulate a drone's flight controls. It interacts with the Gazebo simulator through a Gazebo driver, allowing it to process virtual sensor data and respond as if it were flying in the real world.                                                                                                        | 10.13.0.2 | -              |
| Companion Computer  | The Companion Computer (attached to the drone) handles higher-level processing tasks that are too complex for the flight controller. It manages wireless networking, telemetry logs, facilitates camera streaming for surveillance or reconnaissance, and interfaces with guidance systems for autonomous operations.                            | 10.13.0.3 | 192.168.13.1   |
| Ground Control Station | This component acts as the remote pilot's interface, providing mission planning capabilities, flight mapping, video streaming, and joystick control inputs. It communicates with the flight controller and companion computer over a simulated wireless MAVLink connection.                                                            | 10.13.0.4 | 192.168.13.14   |
| Simulator            | Gazebo provides a richly detailed 3D world where the physics of drone flight are accurately modeled. This allows for realistic simulations of how the drone would react to control inputs and environmental factors. It’s here that the rotors spin, and the virtual drone takes to the skies, all under the watchful control of the Simulator Management Web Console. Warning: Try not to target the simulator directly as this may break the Damn Vulnerable Drone | 10.13.0.5 | -              |


## Flight States

The legacy Docker management UI at `http://localhost:8000` offers flight states by asking its Ground Control Station to command the ArduPilot vehicle. These controls do not operate the native Antoine profile, whose current launcher runs a fixed bounded proof.

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/flight-states.png?raw=true" alt="Damn Vulnerable Drone Flight States"/>
</p>

**1. Initial Boot:**
This simulates the drone's startup sequence, where all systems are initialized. Clicking this will simulate you "pressing" the power on button on the drone flight controller, allowing for the companion computer to establish a connection to it. This phase is critical for security/safety checks, calibration and ensuring communication protocols are setup before flight.

**2. Arm & Takeoff**
The phase where the drone transitions from a stationary state to airborne, testing the responsiveness of flight controls and the integrity of take-off protocols. Note: This may take some time to complete as the drone requires GPS & EKF3 to be ready)

**3. Autopilot Flight**
Represents the drone's ability to navigate autonomously based on predefined waypoints or dynamic commands, a vital state for exploring vulnerabilities in navigation and control systems.

**4. Emergency / Return-To-Land**
Simulates the drone's emergency protocols, automatically returning to a home location upon triggering fail-safes or loss of control signals, which can be a target for exploitation in hacking scenarios.

**5. Post-Flight Data Processing**
This state involves the handling of all data collected during the flight, including telemetry and logs, making it an important phase for understanding data exfiltration and integrity attacks.

# Attack Scenarios

The existing attack guides target the legacy MAVLink, Wi-Fi, and ROS 2 components unless a guide is explicitly listed under the native Antoine/NATS category in the running management UI. See the Antoine scenario crosswalk for current native coverage.

The full list of Damn Vulnerable Drone attack scenarios and detailed walkthroughs can be found in the project's wiki here: [https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Attack-Scenarios](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Attack-Scenarios)

| Reconnaissance                                      | Protocol Tampering                                | Denial of Service                                | Injection                                              | Exfiltration                                        | Firmware Attacks                                |
|----------------------------------------------------|---------------------------------------------------|--------------------------------------------------|--------------------------------------------------------|---------------------------------------------------|-------------------------------------------------|
| [Wifi Analysis & Cracking](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Wifi-Analysis-&-Cracking) | [Attitude Spoofing](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Attitude-Spoofing) | [Wifi Deauth Attack](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Wifi-Deauth-Attack) | [Ground Control Station Spoofing](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Ground-Control-Station-Spoofing) | [FTP Eavesdropping](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/FTP-Eavesdropping) | [Firmware Modding](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Firmware-Modding) |
| [Drone Discovery](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Drone-Discovery)  | [Battery Spoofing](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Battery-Spoofing) | [Geofencing Attack](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Geofencing-Attack) | [Camera Gimbal Takeover](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Camera-Gimbal-Takeover) | [Parameter Extraction](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Parameter-Extraction) | [Firmware Decompile](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Firmware-Decompile) |
| [Companion Computer Detection](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Companion-Computer-Detection) | [GPS Spoofing](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/GPS-Spoofing)   | [GPS Offset Glitching](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/GPS-Offset-Glitching) | [GPS Data Injection](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/GPS-Data-Injection) | [Wifi Client Data Leak](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Wifi-Client-Data-Leak) | |
| [Ground Control Station Discovery](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Ground-Control-Station-Discovery) | [Critical Error Spoofing](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Critical-Error-Spoofing) | [Flight Termination](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Flight-Termination) | [Return to Home Point Override](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Return-to-Home-Point-Override) | [Flight Log Extraction](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Flight-Log-Extraction) | |
| [Packet Sniffing](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Packet-Sniffing)  | [Emergency Status Spoofing](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Emergency-Status-Spoofing) | [Camera Feed ROS Topic Flooding](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Camera-Feed-ROS-Topic-Flooding) | [Companion Computer Web UI Login Brute Force](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Companion-Computer-Web-UI-Login-Brute-Force) | [Camera Feed Eavesdropping](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Camera-Feed-Eavesdropping) | |
| [Protocol Fingerprinting](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Protocol-Fingerprinting) | [Satellite Spoofing](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Satellite-Spoofing) | [Denial-of-Takeoff](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Denial-of-Takeoff) | [Waypoint Injection](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Waypoint-Injection) | [Mission Extraction](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Mission-Extraction) | |
| [Drone GPS & Telemetry Detection](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Drone-GPS-&-Telemetry-Detection) | [VFR HUD Spoofing](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/VFR-HUD-Spoofing) | [Communication Link Flooding](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Communication-Link-Flooding) | [Companion Computer Takeover](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Companion-Computer-Takeover) | [ROS 2 Topic Replay](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/ROS-2-Topic-Replay) | |
| [ROS 2 DDS Graph Enumeration](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/ROS-2-DDS-Graph-Enumeration) | [System Status Spoofing](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/System-Status-Spoofing) | | [MAVLink Injection Attack](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/MAVLink-Injection-Attack) | | |
| | | | [Flight Mode Injection](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/Flight-Mode-Injection) | | |
| | | | [ROS 2 Rogue Publisher Injection](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/wiki/ROS-2-Rogue-Publisher-Injection) | | |

## Documentation & Walkthrough

Each of the attack scenarios have detailed documentation which outlines what the attack scenario is, as well as a **Spoiler** step-by-step walkthrough for users to follow in order to execute the attack. These walkthroughs are hidden behind a button, which when clicked, will reveal the instructions.

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/Walkthrough.png?raw=true" alt="Damn Vulnerable Drone Walkthrough"/>
</p>


# Screenshots

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/a43dbcd7-10b0-4f7a-b920-5925bac59642.gif?raw=true" alt="Damn Vulnerable Drone Demo"/>
</p>

<p>
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/Damn-Vulnerable-Drone-Interface.png?raw=true" alt="Damn Vulnerable Drone Interface"/>
</p>

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/Damn-Vulnerable-Drone.png?raw=true" alt="Damn Vulnerable Drone"/>
</p>

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/Ground-Control-Station.png?raw=true" alt="Ground Control Station"/>
</p>

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/Drone-Hacker-Van.png?raw=true" alt="Drone Hacker Van"/>
</p>

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/Drone-Hacking-Station.png?raw=true" alt="Drone Hacking Station"/>
</p>

<p align="center">
  <img src="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/blob/master/simulator/mgmt/static/images/kali-drone-hacker.png?raw=true" alt="Damn Vulnerable Drone Desktop Background"/>
</p>

# Mentions

- [OWASP Top 10 Drone Security Risks](https://github.com/OWASP/CheatSheetSeries/issues/1412)
- [Kitploit](https://kitploit.com/2024/09/damn-vulnerable-drone-intentionally.html/)
- [David Bombal - Drone Hacking Demo](https://youtu.be/c1ZCHCwqWls?t=5705)
- [Red Team Village at DEFCON 32](https://becomingahacker.org/def-con-32-red-team-village-activities-e4e20895df37)
- [Aerospace Hacking Village DEFCON 32](https://www.aerospacevillage.org/defcon-32-workshop-schedule)
- [Cyberattacks and defenses for Autonomous Navigation Systems: A systematic literature review](https://www.researchgate.net/publication/391637526_Cyberattacks_and_defenses_for_Autonomous_Navigation_Systems_A_systematic_literature_review)
- [Drone Software Meetup Group](https://www.meetup.com/drone-software-meetup-group/events/300478718/)
- [National Cyber Security Service's](https://www.facebook.com/ncybersec/posts/-damn-vulnerable-drone-the-damn-vulnerable-drone-is-an-intentionally-vulnerable-/1040824904739615/)
- [OWASP Tunisia Chapter, Drone Security - Helmi Rais](https://www.youtube.com/watch?v=30gvG5EByHw)
- [Red Wrench - How to hack drones part 1](https://youtu.be/KYHbFEJl0AU?si=3tKzdYCN47W_Gkoc)

# Community Support

The Damn Vulnerable Drone platform thrives on the active participation and collaboration of its user community. This community is a collective of like-minded individuals ranging from cybersecurity enthusiasts to professional ethical hackers, all focused on sharing knowledge and advancing skills in drone security. Whether you're encountering technical issues, seeking advice on tackling scenarios, or looking to discuss the latest trends in drone vulnerabilities, the Damn Vulnerable Drone community is a valuable resource.

The community [Slack Channel](https://join.slack.com/t/damnvulnerabledrone/shared_invite/zt-2g9tp202t-x5csb~uTyvHurgptki_XwQ) is available for users to connect, share experiences, and provide mutual support. Users are encouraged to participate actively, ask questions, and offer help to others. Experienced members of the community often mentor newcomers, fostering a culture of continuous learning and improvement. Remember, every question you ask and every answer you provide helps the entire community grow stronger.

# Feedback & Contributions

The platform maintains a [GitHub open source repository](https://github.com/nicholasaleks/Damn-Vulnerable-Drone) where users can submit their contributions. These contributions are reviewed by the maintainers and, if aligned with the project's goals, are integrated into the platform. By contributing, you help ensure that the DVD remains a cutting-edge tool for learning and practicing drone hacking techniques in a safe and ethical manner.

If you have developed a new attack scenario, discovered a way to improve the simulation, or created educational content that could benefit others, create a [GitHub Pull Request](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/pulls). Contributions can take various forms, from code patches and feature suggestions to writing documentation and creating tutorial videos.

Feedback is the cornerstone of growth for the Damn Vulnerable Drone platform. Users are encouraged to provide their insights by creating a [GitHub Issue](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/issues). Do your best to including any challenges faced and suggestions for enhancements. This feedback is invaluable for the ongoing development and refinement of the platform.

# Roadmap
- Support PX4 Firmware
- Support Kali docker container
- Support Fixed-Wing Drone models

# Credits

Thanks to all the amazing [community contributors for sending PRs](https://github.com/nicholasaleks/Damn-Vulnerable-Drone/graphs/contributors) and keeping this project updated. :heart:

If you have an idea or some kind of improvement, you are welcome to contribute and participate in the project, feel free to send your PR.

<p align="center">
  <a href="https://github.com/nicholasaleks/Damn-Vulnerable-Drone/graphs/contributors">
    <img src="https://contrib.rocks/image?repo=nicholasaleks/Damn-Vulnerable-Drone" />
  </a>
</p>

# Disclaimer

The Damn Vulnerable Drone (DVD) platform is provided solely for educational and research purposes. Users are expected to adhere to ethical hacking principles, respecting privacy and laws, and must not use skills or knowledge acquired from Damn Vulnerable Drone for malicious activities. The creators and maintainers of Damn Vulnerable Drone are not liable for any misuse of the platform. By using Damn Vulnerable Drone, you agree to use it responsibly and within legal boundaries. Damn Vulnerable Drone is highly insecure, and as such, should not be deployed on drone hardware or internet facing servers. It is intentionally flawed and vulnerable, as such, it comes with no warranties.

# License

It is distributed under the MIT License. See LICENSE for more information.

