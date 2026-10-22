use std::{env, net::SocketAddr, sync::Arc, time::Duration};

use anyhow::{bail, Context, Result};
use futures_util::StreamExt;
use mavlink::{dialects::common::MavMessage, MavHeader};
use tokio::net::UdpSocket;

fn arg(name: &str, default: &str) -> String {
    env::args()
        .skip_while(|value| value != name)
        .nth(1)
        .unwrap_or_else(|| default.to_owned())
}

fn required_value(name: &str) -> Option<String> {
    let mut args = env::args().skip(1);
    while let Some(value) = args.next() {
        if value == name {
            return args.next();
        }
    }
    None
}

fn usage() {
    println!(
        "Usage:\n  mavlink-nats-console listen [--nats-url URL] [--callsign NAME] [--listen ADDR] [--bridge ADDR]\n  mavlink-nats-console monitor [--nats-url URL] [--callsign NAME]\n  mavlink-nats-console heartbeat|arm|disarm [same options]\n\nDefaults: NATS 127.0.0.1:14224, callsign qgc-live, listen 127.0.0.1:14570, bridge 127.0.0.1:14651"
    );
}

fn loopback_addr(name: &str, default: &str) -> Result<SocketAddr> {
    let address: SocketAddr = arg(name, default)
        .parse()
        .with_context(|| format!("invalid {name}"))?;
    if !address.ip().is_loopback() {
        bail!("{name} must use a loopback address; got {address}");
    }
    Ok(address)
}

fn nats_command_summary(payload: &[u8]) -> String {
    match std::str::from_utf8(payload) {
        Ok(text) => match serde_json::from_str::<serde_json::Value>(text) {
            Ok(value) => serde_json::to_string(&value).unwrap_or_else(|_| "valid JSON".into()),
            Err(_) => "non-JSON payload".into(),
        },
        Err(_) => format!("{} bytes", payload.len()),
    }
}

async fn watch_nats(nats_url: String, callsign: String) -> Result<()> {
    let client = async_nats::connect(&nats_url)
        .await
        .with_context(|| format!("connect to NATS at {nats_url}"))?;
    let mut telemetry = client
        .subscribe(format!("furia.telemetry.{callsign}"))
        .await?;
    let mut commands = client
        .subscribe(format!("furia.command.antoine.{callsign}"))
        .await?;
    let mut last_telemetry = tokio::time::Instant::now() - Duration::from_secs(1);

    loop {
        tokio::select! {
            _ = tokio::signal::ctrl_c() => break,
            message = telemetry.next() => {
                if let Some(message) = message {
                    let now = tokio::time::Instant::now();
                    if now.duration_since(last_telemetry) >= Duration::from_secs(1) {
                        println!("NATS telemetry {}", nats_command_summary(&message.payload));
                        last_telemetry = now;
                    }
                }
            }
            message = commands.next() => {
                if let Some(message) = message {
                    println!("NATS command {}", nats_command_summary(&message.payload));
                }
            }
        }
    }
    Ok(())
}

async fn send_arm_disarm(
    socket: &UdpSocket,
    acknowledgements: &mut tokio::sync::mpsc::UnboundedReceiver<bool>,
    bridge: SocketAddr,
    sysid: u8,
    component: u8,
    target_system: u8,
    arm: bool,
) -> Result<()> {
    let command = MavMessage::COMMAND_LONG(mavlink::dialects::common::COMMAND_LONG_DATA {
        param1: if arm { 1.0 } else { 0.0 },
        param2: 0.0,
        param3: 0.0,
        param4: 0.0,
        param5: 0.0,
        param6: 0.0,
        param7: 0.0,
        command: mavlink::dialects::common::MavCmd::MAV_CMD_COMPONENT_ARM_DISARM,
        target_system,
        target_component: 1,
        confirmation: 0,
    });
    let header = MavHeader {
        system_id: sysid,
        component_id: component,
        sequence: 0,
    };
    let mut frame = Vec::new();
    mavlink::write_v2_msg(&mut frame, header, &command)?;
    socket.send_to(&frame, bridge).await?;
    println!(
        "Sent {} request to system {target_system}; waiting up to 5 seconds for COMMAND_ACK",
        if arm { "ARM" } else { "DISARM" }
    );
    match tokio::time::timeout(Duration::from_secs(5), acknowledgements.recv()).await {
        Ok(Some(true)) => Ok(()),
        Ok(Some(false)) => bail!("vehicle rejected COMMAND_LONG arm/disarm"),
        Ok(None) => bail!("MAVLink receive task stopped before COMMAND_ACK"),
        Err(_) => bail!("timed out waiting for COMMAND_ACK"),
    }
}

fn decode_frame(frame: &[u8]) -> Result<(MavHeader, MavMessage)> {
    use mavlink::peek_reader::PeekReader;
    use std::io::Cursor;

    let mut reader = PeekReader::new(Cursor::new(frame));
    let decoded = match frame.first() {
        Some(0xfe) => mavlink::read_v1_msg::<MavMessage, _>(&mut reader),
        Some(0xfd) if frame.len() > 2 && frame[2] & 1 == 0 => {
            mavlink::read_v2_msg::<MavMessage, _>(&mut reader)
        }
        Some(0xfd) => bail!("signed MAVLink 2 frames are unsupported"),
        _ => bail!("unsupported MAVLink frame"),
    }?;
    Ok(decoded)
}

fn render_message(header: MavHeader, message: MavMessage) {
    match message {
        MavMessage::HEARTBEAT(data) => println!(
            "MAVLink HEARTBEAT sys={} comp={} custom_mode={} type={:?} autopilot={:?} base_mode={:?} status={:?}",
            header.system_id,
            header.component_id,
            data.custom_mode,
            data.mavtype,
            data.autopilot,
            data.base_mode,
            data.system_status
        ),
        MavMessage::COMMAND_ACK(data) => println!(
            "MAVLink COMMAND_ACK sys={} command={:?} result={:?}",
            header.system_id, data.command, data.result
        ),
        MavMessage::GLOBAL_POSITION_INT(data) => println!(
            "MAVLink GLOBAL_POSITION_INT lat={:.7} lon={:.7} alt_m={:.2} rel_alt_m={:.2}",
            data.lat as f64 / 1e7,
            data.lon as f64 / 1e7,
            data.alt as f64 / 1000.0,
            data.relative_alt as f64 / 1000.0
        ),
        MavMessage::GPS_RAW_INT(data) => println!(
            "MAVLink GPS_RAW_INT fix={:?} satellites={} lat={:.7} lon={:.7}",
            data.fix_type,
            data.satellites_visible,
            data.lat as f64 / 1e7,
            data.lon as f64 / 1e7
        ),
        other => println!("MAVLink sys={} {:?}", header.system_id, other),
    }
}

async fn run() -> Result<()> {
    let action = env::args().nth(1).unwrap_or_else(|| "listen".into());
    if action == "--help" || action == "-h" || action == "help" {
        usage();
        return Ok(());
    }
    if !["listen", "monitor", "heartbeat", "arm", "disarm"].contains(&action.as_str()) {
        usage();
        bail!("unknown action {action:?}");
    }

    let bind = loopback_addr("--listen", "127.0.0.1:14570")?;
    let bridge = loopback_addr("--bridge", "127.0.0.1:14651")?;
    let nats_url = arg("--nats-url", "nats://127.0.0.1:14224");
    let callsign = arg("--callsign", "qgc-live");
    let sysid: u8 = arg("--system-id", "255").parse()?;
    let component: u8 = arg("--component-id", "190").parse()?;
    let target_system: u8 = required_value("--target-system")
        .unwrap_or_else(|| "1".into())
        .parse()?;
    if action == "monitor" {
        println!("Read-only NATS monitor; no MAVLink UDP or control socket is opened.");
        return watch_nats(nats_url, callsign).await;
    }

    let socket = Arc::new(
        UdpSocket::bind(bind)
            .await
            .with_context(|| format!("bind MAVLink console at {bind}"))?,
    );
    println!(
        "MAVLink UDP console bind={bind} bridge={bridge}; NATS={nats_url} callsign={callsign}"
    );
    println!("Listening for MAVLink and NATS; loopback control UDP accepts arm, disarm, status.");

    if action == "listen" {
        let control_bind = loopback_addr("--control-listen", "127.0.0.1:14573")?;
        let control_socket = UdpSocket::bind(control_bind)
            .await
            .with_context(|| format!("bind local control socket at {control_bind}"))?;
        println!("Control datagram socket {control_bind} accepts only arm, disarm, status");
        let (armed_state_tx, armed_state_rx) =
            tokio::sync::watch::channel(None::<(bool, tokio::time::Instant)>);
        let armed_state_tx = Arc::new(armed_state_tx);
        let (acknowledgement_tx, mut acknowledgement_rx) =
            tokio::sync::mpsc::unbounded_channel::<bool>();
        let announce = arg("--announce-heartbeat", "true") == "true";
        if announce {
            let announce_socket = socket.clone();
            let announce_bridge = bridge;
            tokio::spawn(async move {
                let heartbeat = MavMessage::HEARTBEAT(mavlink::dialects::common::HEARTBEAT_DATA {
                    custom_mode: 0,
                    mavtype: mavlink::dialects::common::MavType::MAV_TYPE_GCS,
                    autopilot: mavlink::dialects::common::MavAutopilot::MAV_AUTOPILOT_INVALID,
                    base_mode: mavlink::dialects::common::MavModeFlag::empty(),
                    system_status: mavlink::dialects::common::MavState::MAV_STATE_ACTIVE,
                    mavlink_version: 3,
                });
                let mut sequence = 0u8;
                let mut interval = tokio::time::interval(Duration::from_secs(1));
                loop {
                    interval.tick().await;
                    let mut frame = Vec::new();
                    let header = MavHeader {
                        system_id: sysid,
                        component_id: component,
                        sequence,
                    };
                    if let Err(error) = mavlink::write_v2_msg(&mut frame, header, &heartbeat) {
                        eprintln!("Could not encode GCS heartbeat: {error}");
                        continue;
                    }
                    if let Err(error) = announce_socket.send_to(&frame, announce_bridge).await {
                        eprintln!("Could not send GCS heartbeat: {error}");
                    }
                    sequence = sequence.wrapping_add(1);
                }
            });
        }
        let watcher_nats_url = nats_url.clone();
        let watcher_callsign = callsign.clone();
        tokio::spawn(async move {
            if let Err(error) = watch_nats(watcher_nats_url, watcher_callsign).await {
                eprintln!("NATS watcher stopped: {error:#}");
            }
        });
        let telemetry_socket = socket.clone();
        let telemetry_armed_state = armed_state_tx.clone();
        let telemetry_system_id = target_system;
        tokio::spawn(async move {
            let mut buffer = [0u8; 2048];
            loop {
                let (length, source) = match telemetry_socket.recv_from(&mut buffer).await {
                    Ok(received) => received,
                    Err(error) => {
                        eprintln!("MAVLink receive failed: {error}");
                        continue;
                    }
                };
                if !source.ip().is_loopback() {
                    eprintln!("Ignored non-loopback MAVLink datagram from {source}");
                    continue;
                }
                let (header, message) = match decode_frame(&buffer[..length]) {
                    Ok(decoded) => decoded,
                    Err(error) => {
                        eprintln!("MAVLink receive from {source}: {error:#}");
                        continue;
                    }
                };
                if header.system_id == telemetry_system_id {
                    if let MavMessage::HEARTBEAT(data) = &message {
                        let armed = data.base_mode.bits()
                            & mavlink::dialects::common::MavModeFlag::MAV_MODE_FLAG_SAFETY_ARMED
                                .bits()
                            != 0;
                        let _ =
                            telemetry_armed_state.send(Some((armed, tokio::time::Instant::now())));
                    }
                }
                if let MavMessage::COMMAND_ACK(data) = &message {
                    if data.command
                        == mavlink::dialects::common::MavCmd::MAV_CMD_COMPONENT_ARM_DISARM
                    {
                        let accepted = data.result
                            == mavlink::dialects::common::MavResult::MAV_RESULT_ACCEPTED;
                        let _ = acknowledgement_tx.send(accepted);
                    }
                }
                render_message(header, message);
            }
        });
        let mut control_state = armed_state_rx;
        loop {
            tokio::select! {
                received = async {
                    let mut buffer = [0u8; 128];
                    let result = control_socket.recv_from(&mut buffer).await;
                    (buffer, result)
                } => {
                    let (buffer, result) = received;
                    let (length, source) = result?;
                    if !source.ip().is_loopback() {
                        eprintln!("Rejected non-loopback control request from {source}");
                        continue;
                    }
                    let request = std::str::from_utf8(&buffer[..length]).unwrap_or("").trim();
                    let (action, request_id) = request.split_once(' ').unwrap_or((request, ""));
                    let response = match action {
                        "arm" | "disarm" => {
                            let armed = action == "arm";
                            match send_arm_disarm(&socket, &mut acknowledgement_rx, bridge, sysid, component, target_system, armed).await {
                                Ok(()) => {
                                    let expected = action == "arm";
                                    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
                                    loop {
                                        let observed = *control_state.borrow();
                                        if let Some((actual, timestamp)) = observed {
                                            if actual == expected
                                                && timestamp.elapsed() <= Duration::from_secs(3)
                                            {
                                                break format!("ok {request_id} acked {action} heartbeat armed={actual}");
                                            }
                                        }
                                        if tokio::time::Instant::now() >= deadline {
                                            break format!("error {request_id} acked-{action}-but-heartbeat-state-not-confirmed");
                                        }
                                        if tokio::time::timeout_at(deadline, control_state.changed()).await.is_err() {
                                            break format!("error {request_id} acked-{action}-but-heartbeat-state-not-confirmed");
                                        }
                                    }
                                }
                                Err(error) => format!("error {request_id} {error}"),
                            }
                        }
                        "status" => {
                            match control_state.borrow().as_ref() {
                                Some((armed, timestamp)) if timestamp.elapsed() <= Duration::from_secs(3) =>
                                    format!("ok {request_id} heartbeat system={target_system} armed={armed} age_ms={}", timestamp.elapsed().as_millis()),
                                _ => format!("error {request_id} no-recent-heartbeat system={target_system}"),
                            }
                        }
                        _ => format!("error {request_id} unsupported-action"),
                    };
                    if let Err(error) = control_socket.send_to(response.as_bytes(), source).await {
                        eprintln!("Control response to {source} failed: {error}");
                    }
                }
            }
        }
    }

    if action == "heartbeat" {
        let heartbeat = MavMessage::HEARTBEAT(mavlink::dialects::common::HEARTBEAT_DATA {
            custom_mode: 0,
            mavtype: mavlink::dialects::common::MavType::MAV_TYPE_GCS,
            autopilot: mavlink::dialects::common::MavAutopilot::MAV_AUTOPILOT_INVALID,
            base_mode: mavlink::dialects::common::MavModeFlag::empty(),
            system_status: mavlink::dialects::common::MavState::MAV_STATE_ACTIVE,
            mavlink_version: 3,
        });
        let mut frame = Vec::new();
        mavlink::write_v2_msg(
            &mut frame,
            MavHeader {
                system_id: sysid,
                component_id: component,
                sequence: 0,
            },
            &heartbeat,
        )?;
        socket.send_to(&frame, bridge).await?;
        println!("Sent read-only GCS heartbeat to bridge; no vehicle control command sent.");
        return Ok(());
    }

    println!("Waiting up to 5 seconds for recent system {target_system} heartbeat before sending control...");
    let discovery_deadline = std::time::Instant::now() + Duration::from_secs(5);
    let mut discovered = false;
    while std::time::Instant::now() < discovery_deadline {
        let mut buffer = [0u8; 2048];
        match tokio::time::timeout(Duration::from_millis(250), socket.recv_from(&mut buffer)).await
        {
            Ok(Ok((length, source))) if source.ip().is_loopback() => {
                match decode_frame(&buffer[..length]) {
                    Ok((header, MavMessage::HEARTBEAT(data)))
                        if header.system_id == target_system =>
                    {
                        println!("Discovered system {} component {}: type={:?}, firmware={:?}, status={:?}", header.system_id, header.component_id, data.mavtype, data.autopilot, data.system_status);
                        discovered = true;
                        break;
                    }
                    Ok(_) => {}
                    Err(error) => eprintln!("Ignoring invalid discovery datagram: {error:#}"),
                }
            }
            Ok(Ok((_length, source))) => {
                eprintln!("Ignoring non-loopback MAVLink datagram from {source}")
            }
            Ok(Err(error)) => return Err(error.into()),
            Err(_) => {}
        }
    }
    if !discovered {
        bail!("no recent heartbeat from system {target_system}; command not sent");
    }

    let arm = action == "arm";
    let command = MavMessage::COMMAND_LONG(mavlink::dialects::common::COMMAND_LONG_DATA {
        param1: if arm { 1.0 } else { 0.0 },
        param2: 0.0,
        param3: 0.0,
        param4: 0.0,
        param5: 0.0,
        param6: 0.0,
        param7: 0.0,
        command: mavlink::dialects::common::MavCmd::MAV_CMD_COMPONENT_ARM_DISARM,
        target_system,
        target_component: 1,
        confirmation: 0,
    });
    let header = MavHeader {
        system_id: sysid,
        component_id: component,
        sequence: 0,
    };
    let mut frame = Vec::new();
    mavlink::write_v2_msg(&mut frame, header, &command)?;
    socket.send_to(&frame, bridge).await?;
    println!(
        "Sent {action} request to system {target_system}; waiting up to 5 seconds for COMMAND_ACK"
    );
    let deadline = std::time::Instant::now() + Duration::from_secs(5);
    while std::time::Instant::now() < deadline {
        let mut buffer = [0u8; 2048];
        match tokio::time::timeout(Duration::from_millis(250), socket.recv_from(&mut buffer)).await
        {
            Ok(Ok((length, source))) if source.ip().is_loopback() => {
                let (header, message) = match decode_frame(&buffer[..length]) {
                    Ok(decoded) => decoded,
                    Err(error) => {
                        eprintln!("Ignoring invalid MAVLink response: {error:#}");
                        continue;
                    }
                };
                if let MavMessage::COMMAND_ACK(_) = message {
                    let confirmed = if let MavMessage::COMMAND_ACK(ref data) = message {
                        data.command
                            == mavlink::dialects::common::MavCmd::MAV_CMD_COMPONENT_ARM_DISARM
                            && data.result
                                == mavlink::dialects::common::MavResult::MAV_RESULT_ACCEPTED
                    } else {
                        false
                    };
                    render_message(header, message);
                    if confirmed {
                        return Ok(());
                    }
                } else {
                    render_message(header, message);
                }
            }
            Ok(Ok((_length, source))) => eprintln!("Ignoring non-loopback response from {source}"),
            Ok(Err(error)) => return Err(error.into()),
            Err(_) => {}
        }
    }
    bail!("timed out waiting for COMMAND_ACK")
}

#[tokio::main]
async fn main() -> Result<()> {
    run().await
}
