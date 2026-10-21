use std::{
    net::SocketAddr,
    time::{SystemTime, UNIX_EPOCH},
};

use anyhow::{bail, Context, Result};
use async_nats::jetstream::{self, consumer, stream};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use futures_util::StreamExt;
use mavlink::{dialects::common::*, MavHeader};
use serde::{Deserialize, Serialize};

const STREAM: &str = "ANTOINE_MAVLINK";
const TELEMETRY_SUBJECT: &str = "furia.mavlink.telemetry";
const OUTGOING_HEARTBEAT_SUBJECT: &str = "furia.mavlink.heartbeat";
const GCS_COMMAND_SUBJECT: &str = "furia.mavlink.command";
const COMMAND_SUBJECT: &str = "furia.mavlink.request";
const DURABLE: &str = "antoine_mavlink_readonly_requests";
const MAX_GAZEBO_TELEMETRY_AGE_MS: u64 = 1_000;
const MAX_GAZEBO_TELEMETRY_FUTURE_MS: u64 = 250;

#[derive(Debug, Clone)]
struct Config {
    callsign: String,
    nats_url: String,
    vehicle_bind: SocketAddr,
    vehicle_peer: SocketAddr,
    qgc_bind: SocketAddr,
    qgc_peer: SocketAddr,
    qgc_telemetry_peer: Option<SocketAddr>,
    system_id: u8,
    home_alt_msl: f64,
    home_lat_deg: f64,
    home_lon_deg: f64,
    mode: String,
    control_subject: String,
}

#[derive(Serialize, Deserialize)]
struct FrameEnvelope {
    schema: &'static str,
    version: &'static str,
    callsign: String,
    direction: String,
    received_at_unix_ms: u128,
    source: String,
    system_id: u8,
    component_id: u8,
    message_id: u32,
    sequence: u8,
    raw_base64: String,
}

#[derive(Debug)]
struct FrameInfo {
    system_id: u8,
    component_id: u8,
    message_id: u32,
    sequence: u8,
    payload: Vec<u8>,
}

fn outgoing_heartbeat_audit_subject(
    direction: &str,
    frame: &FrameInfo,
    callsign: &str,
) -> Option<String> {
    (direction == "vehicle_to_gcs" && frame.message_id == 0)
        .then(|| format!("{OUTGOING_HEARTBEAT_SUBJECT}.{callsign}"))
}

fn validate_frame(raw: &[u8]) -> Result<FrameInfo> {
    use mavlink::{dialects::common::MavMessage, peek_reader::PeekReader, Message};
    use std::io::Cursor;

    if raw.len() < 8 {
        bail!("MAVLink datagram is too short");
    }
    let expected_len = match raw[0] {
        0xfe => usize::from(raw[1]) + 8,
        0xfd => {
            if raw.len() < 12 {
                bail!("MAVLink 2 datagram is too short");
            }
            usize::from(raw[1]) + 12 + usize::from(raw[2] & 1 != 0) * 13
        }
        _ => bail!("unsupported MAVLink magic byte"),
    };
    if raw.len() != expected_len {
        bail!("MAVLink UDP datagram has an invalid frame length");
    }
    let mut reader = PeekReader::new(Cursor::new(raw));
    let (header, message) = match raw[0] {
        0xfe => mavlink::read_v1_msg::<MavMessage, _>(&mut reader)?,
        0xfd if raw[2] & 1 != 0 => {
            bail!("signed MAVLink 2 frames are unsupported without a configured signing key")
        }
        0xfd => mavlink::read_v2_msg::<MavMessage, _>(&mut reader)?,
        _ => unreachable!(),
    };
    Ok(FrameInfo {
        system_id: header.system_id,
        component_id: header.component_id,
        message_id: message.message_id(),
        sequence: header.sequence,
        payload: match raw[0] {
            0xfe => raw[6..raw.len() - 2].to_vec(),
            0xfd => raw[10..raw.len() - 2 - usize::from(raw[2] & 1 != 0) * 13].to_vec(),
            _ => unreachable!(),
        },
    })
}

fn arg(name: &str, default: &str) -> String {
    std::env::args()
        .skip_while(|v| v != name)
        .nth(1)
        .unwrap_or_else(|| default.into())
}

fn config() -> Result<Config> {
    let mut config = Config {
        mode: arg("--mode", "udp"),
        control_subject: String::new(),
        home_alt_msl: arg("--home-alt-msl", "82").parse()?,
        home_lat_deg: arg("--home-lat-deg", "48.0002").parse()?,
        home_lon_deg: arg("--home-lon-deg", "0.3564").parse()?,
        callsign: arg("--callsign", "antoine-sitl"),
        nats_url: arg("--nats-url", "nats://127.0.0.1:4222"),
        vehicle_bind: arg("--vehicle-bind", "127.0.0.1:14560").parse()?,
        vehicle_peer: arg("--vehicle-peer", "127.0.0.1:14561").parse()?,
        qgc_bind: arg("--qgc-bind", "127.0.0.1:14562").parse()?,
        qgc_peer: arg("--qgc-peer", "127.0.0.1:14550").parse()?,
        qgc_telemetry_peer: {
            let peer = arg("--qgc-telemetry-peer", "");
            (!peer.is_empty()).then(|| peer.parse()).transpose()?
        },
        system_id: arg("--system-id", "1").parse()?,
    };
    config.control_subject = format!("furia.mavlink.control.{}", config.callsign);
    for (name, address) in [
        ("vehicle bind", config.vehicle_bind),
        ("vehicle peer", config.vehicle_peer),
        ("QGC bind", config.qgc_bind),
        ("QGC peer", config.qgc_peer),
    ] {
        if !address.ip().is_loopback() {
            bail!("{name} must use a loopback address (got {address})");
        }
    }
    if let Some(peer) = config.qgc_telemetry_peer {
        if !peer.ip().is_loopback() || peer == config.qgc_peer {
            bail!("--qgc-telemetry-peer must be a distinct loopback UDP address");
        }
    }
    if !config.home_alt_msl.is_finite() || !(-1000.0..=100_000.0).contains(&config.home_alt_msl) {
        bail!("--home-alt-msl must be finite and within [-1000, 100000]");
    }
    if !config.home_lat_deg.is_finite()
        || !(-90.0..=90.0).contains(&config.home_lat_deg)
        || !config.home_lon_deg.is_finite()
        || !(-180.0..=180.0).contains(&config.home_lon_deg)
    {
        bail!("home coordinates must be finite valid latitude/longitude values");
    }
    let nats = url::Url::parse(&config.nats_url).context("parse --nats-url")?;
    let host = nats.host_str().context("--nats-url must include a host")?;
    let local_host = host == "localhost"
        || host
            .parse::<std::net::IpAddr>()
            .is_ok_and(|address| address.is_loopback());
    if !local_host {
        bail!("--nats-url must use localhost or a loopback IP (got {host})");
    }
    if !matches!(config.mode.as_str(), "udp" | "antoine-nats" | "gazebo-nats") {
        bail!("--mode must be udp, antoine-nats, or gazebo-nats");
    }
    Ok(config)
}

fn mavlink_v2_frame(
    message_id: u32,
    sequence: u8,
    system_id: u8,
    component_id: u8,
    payload: &[u8],
    crc_extra: u8,
) -> Vec<u8> {
    let mut body = vec![payload.len() as u8, 0, 0, sequence, system_id, component_id];
    body.extend_from_slice(&message_id.to_le_bytes()[..3]);
    body.extend_from_slice(payload);
    let mut crc = 0xffff_u16;
    for byte in body.iter().copied().chain(std::iter::once(crc_extra)) {
        let mut tmp = byte ^ crc as u8;
        tmp ^= tmp.wrapping_shl(4);
        crc = (crc >> 8) ^ (u16::from(tmp) << 8) ^ (u16::from(tmp) << 3) ^ (u16::from(tmp) >> 4);
    }
    let mut frame = vec![0xfd];
    frame.extend_from_slice(&body);
    frame.extend_from_slice(&crc.to_le_bytes());
    frame
}

fn encode_message(sequence: u8, system_id: u8, component_id: u8, message: MavMessage) -> Vec<u8> {
    let mut output = Vec::with_capacity(280);
    mavlink::write_v2_msg(
        &mut output,
        MavHeader {
            sequence,
            system_id,
            component_id,
        },
        &message,
    )
    .expect("writing a MAVLink message to Vec cannot fail");
    output
}

fn finite_number(value: &serde_json::Value, key: &str) -> Result<f64> {
    let number = value
        .get(key)
        .and_then(serde_json::Value::as_f64)
        .with_context(|| format!("telemetry missing numeric {key}"))?;
    if !number.is_finite() {
        bail!("telemetry {key} must be finite");
    }
    Ok(number)
}

fn unix_millis(value: &serde_json::Value, key: &str) -> Result<u64> {
    let timestamp = value
        .get(key)
        .and_then(serde_json::Value::as_u64)
        .with_context(|| format!("telemetry {key} must be a nonnegative integer"))?;
    if timestamp > u64::MAX - MAX_GAZEBO_TELEMETRY_AGE_MS {
        bail!("telemetry {key} is outside the supported Unix millisecond range");
    }
    Ok(timestamp)
}

fn telemetry_frames(
    value: &serde_json::Value,
    system_id: u8,
    home_alt_msl: f64,
    home_lat_deg: f64,
    home_lon_deg: f64,
    sequence: u8,
    armed: bool,
    mode: u8,
) -> Result<Vec<Vec<u8>>> {
    let gazebo_plant = value["protocol"].as_str() == Some("antoine-gz");
    let observed_at_ms = if gazebo_plant {
        Some(unix_millis(value, "observedAtUnixMs")?)
    } else {
        None
    };
    if let Some(observed_at_ms) = observed_at_ms {
        let now_ms = SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis() as u64;
        if observed_at_ms.saturating_add(MAX_GAZEBO_TELEMETRY_AGE_MS) < now_ms {
            bail!("Gazebo telemetry is stale");
        }
        if observed_at_ms > now_ms.saturating_add(MAX_GAZEBO_TELEMETRY_FUTURE_MS) {
            bail!("Gazebo telemetry timestamp is in the future");
        }
    }
    let time_s = if let Some(observed_at_ms) = observed_at_ms {
        observed_at_ms as f64 / 1000.0
    } else {
        finite_number(value, "time_s")?
    };
    let lat = finite_number(value, "lat_deg")?;
    let lon = finite_number(value, "lon_deg")?;
    let alt = finite_number(value, "alt_m")?;
    let vn = finite_number(value, "vn_mps")?;
    let ve = finite_number(value, "ve_mps")?;
    let vd = finite_number(value, "vd_mps")?;
    // Gazebo telemetry does not publish actuator thrust. It is only needed for
    // the disarmed, on-ground pre-arm gate; airborne altitude and velocity
    // already clear that gate. Keep the projection explicitly model-only.
    let thrust = if gazebo_plant {
        0.0
    } else {
        finite_number(value, "actuator_thrust")?
    };
    if !(-90.0..=90.0).contains(&lat)
        || !(-180.0..=180.0).contains(&lon)
        || !(-1000.0..=100_000.0).contains(&alt)
        || !(0.0..=1.0).contains(&thrust)
    {
        bail!("telemetry position or thrust is outside supported bounds");
    }
    let boot_ms = observed_at_ms
        .map(|observed| observed as u32)
        .unwrap_or_else(|| (time_s.max(0.0) * 1000.0).min(u32::MAX as f64) as u32);
    let lat_e7 = (lat * 1e7) as i32;
    let lon_e7 = (lon * 1e7) as i32;
    // Antoine publishes local altitude above the takeoff point. MAVLink
    // GLOBAL_POSITION_INT/GPS_RAW_INT expect MSL altitude, while
    // GLOBAL_POSITION_INT.relative_alt is relative to home.
    let alt_msl = alt + home_alt_msl;
    let alt_mm = (alt_msl * 1000.0) as i32;
    let mut heartbeat = vec![0; 9];
    heartbeat[0..4].copy_from_slice(&(mode as u32).to_le_bytes());
    heartbeat[4] = 2; // MAV_TYPE_QUADROTOR
    heartbeat[5] = 0; // MAV_AUTOPILOT_GENERIC: Antoine implements its own control model.
    heartbeat[6] = if armed { 0x80 | 1 } else { 1 };
    heartbeat[7] = if armed { 4 } else { 3 }; // MAV_STATE_ACTIVE / STANDBY
    heartbeat[8] = 3;

    let mut position = vec![0; 28];
    position[0..4].copy_from_slice(&boot_ms.to_le_bytes());
    position[4..8].copy_from_slice(&lat_e7.to_le_bytes());
    position[8..12].copy_from_slice(&lon_e7.to_le_bytes());
    position[12..16].copy_from_slice(&alt_mm.to_le_bytes());
    let relative_alt_mm = (alt * 1000.0) as i32;
    position[16..20].copy_from_slice(&relative_alt_mm.to_le_bytes());
    for (offset, speed) in [(20, vn), (22, ve), (24, vd)] {
        let speed = (speed * 100.0).clamp(i16::MIN as f64, i16::MAX as f64) as i16;
        position[offset..offset + 2].copy_from_slice(&speed.to_le_bytes());
    }
    position[26..28].copy_from_slice(&u16::MAX.to_le_bytes()); // heading unavailable
    let speed = (vn.hypot(ve).hypot(vd) * 100.0).clamp(0.0, u16::MAX as f64) as u16;
    let mut gps = vec![0; 30];
    let time_usec = (time_s.max(0.0) * 1_000_000.0).min(u64::MAX as f64) as u64;
    gps[0..8].copy_from_slice(&time_usec.to_le_bytes());
    gps[8..12].copy_from_slice(&lat_e7.to_le_bytes());
    gps[12..16].copy_from_slice(&lon_e7.to_le_bytes());
    gps[16..20].copy_from_slice(&alt_mm.to_le_bytes());
    gps[20..22].copy_from_slice(&u16::MAX.to_le_bytes());
    gps[22..24].copy_from_slice(&u16::MAX.to_le_bytes());
    gps[24..26].copy_from_slice(&speed.to_le_bytes());
    gps[26..28].copy_from_slice(&u16::MAX.to_le_bytes());
    gps[28] = 3; // GPS_FIX_TYPE_3D
    gps[29] = 10; // Modelled receiver; not a hardware claim.
    let mut home = vec![0; 52];
    home[0..4].copy_from_slice(&((home_lat_deg * 1e7) as i32).to_le_bytes());
    home[4..8].copy_from_slice(&((home_lon_deg * 1e7) as i32).to_le_bytes());
    home[8..12].copy_from_slice(&((home_alt_msl * 1000.0) as i32).to_le_bytes());
    home[24..28].copy_from_slice(&1.0_f32.to_le_bytes()); // identity quaternion
                                                          // QGC otherwise keeps a model-only vehicle in "Not Ready" because this
                                                          // adapter has no parameter-backed setup or health report. Publish only the
                                                          // pre-arm check bit; this is a simulation gate derived from Antoine's
                                                          // reported state, not a claim about physical sensors.
    let prearm = MavSysStatusSensor::MAV_SYS_STATUS_PREARM_CHECK;
    let gazebo_ready_on_ground = gazebo_plant
        && alt.abs() <= 0.15
        && value["control_authority"].as_str() == Some("safe-hold");
    let ready_to_arm = (value["ground_contact"].as_bool() == Some(true) || gazebo_ready_on_ground)
        && (value["gnss_degraded"].as_bool() == Some(false) || gazebo_plant)
        && finite_number(value, "battery_pct").is_ok_and(|percent| percent >= 20.0)
        && alt.abs() <= 0.1
        && vn.abs() <= 0.15
        && ve.abs() <= 0.15
        && vd.abs() <= 0.15
        && thrust <= 0.05;
    let sys_status = encode_message(
        sequence.wrapping_add(4),
        system_id,
        1,
        MavMessage::SYS_STATUS(SYS_STATUS_DATA {
            onboard_control_sensors_present: prearm,
            onboard_control_sensors_enabled: prearm,
            onboard_control_sensors_health: if ready_to_arm {
                prearm
            } else {
                MavSysStatusSensor::empty()
            },
            load: 0,
            voltage_battery: finite_number(value, "battery_mv")
                .ok()
                .filter(|millivolts| (0.0..=u16::MAX as f64).contains(millivolts))
                .or_else(|| {
                    if !gazebo_plant {
                        return None;
                    }
                    finite_number(value, "battery_pct")
                        .ok()
                        .filter(|percent| (0.0..=100.0).contains(percent))
                        .map(|percent| 12_000.0 + percent * 80.0)
                })
                .map(|millivolts| millivolts as u16)
                .unwrap_or(u16::MAX),
            current_battery: -1,
            drop_rate_comm: 0,
            errors_comm: 0,
            errors_count1: 0,
            errors_count2: 0,
            errors_count3: 0,
            errors_count4: 0,
            battery_remaining: finite_number(value, "battery_pct")
                .ok()
                .filter(|percent| (0.0..=100.0).contains(percent))
                .map(|percent| percent.round() as i8)
                .unwrap_or(-1),
        }),
    );
    Ok(vec![
        mavlink_v2_frame(0, sequence, system_id, 1, &heartbeat, 50),
        mavlink_v2_frame(33, sequence.wrapping_add(1), system_id, 1, &position, 104),
        mavlink_v2_frame(24, sequence.wrapping_add(2), system_id, 1, &gps, 24),
        mavlink_v2_frame(242, sequence.wrapping_add(3), system_id, 1, &home, 104),
        sys_status,
    ])
}

fn command_id(frame: &FrameInfo) -> Option<u16> {
    match frame.message_id {
        76 if frame.payload.len() >= 30 => {
            Some(u16::from_le_bytes([frame.payload[28], frame.payload[29]]))
        }
        _ => None,
    }
}

fn command_ack(
    sequence: u8,
    system_id: u8,
    _target_system: u8,
    _target_component: u8,
    command: u16,
    accepted: bool,
) -> Vec<u8> {
    let mav_command = match command {
        22 => MavCmd::MAV_CMD_NAV_TAKEOFF,
        20 => MavCmd::MAV_CMD_NAV_RETURN_TO_LAUNCH,
        21 => MavCmd::MAV_CMD_NAV_LAND,
        176 => MavCmd::MAV_CMD_DO_SET_MODE,
        300 => MavCmd::MAV_CMD_MISSION_START,
        193 => MavCmd::MAV_CMD_DO_PAUSE_CONTINUE,
        _ => MavCmd::MAV_CMD_COMPONENT_ARM_DISARM,
    };
    encode_message(
        sequence,
        system_id,
        1,
        mavlink::dialects::common::MavMessage::COMMAND_ACK(COMMAND_ACK_DATA {
            command: mav_command,
            result: if accepted {
                MavResult::MAV_RESULT_ACCEPTED
            } else {
                MavResult::MAV_RESULT_DENIED
            },
        }),
    )
}

fn mission_query_envelope(config: &Config, request_id: String) -> serde_json::Value {
    let mut envelope = serde_json::json!({
        "schema":"furia.mavlink.control",
        "version":"1.0.0",
        "identity":config.callsign,
        "request_id":request_id
    });
    if config.mode == "gazebo-nats" {
        envelope["command"] = serde_json::json!({"type":"mission_query","payload":{}});
    } else {
        envelope["type"] = serde_json::json!("mission_query");
    }
    envelope
}

fn mission_ack(
    sequence: u8,
    system_id: u8,
    target_system: u8,
    target_component: u8,
    result: u8,
) -> Vec<u8> {
    encode_message(
        sequence,
        system_id,
        1,
        mavlink::dialects::common::MavMessage::MISSION_ACK(MISSION_ACK_DATA {
            target_system,
            target_component,
            mavtype: match result {
                0 => MavMissionResult::MAV_MISSION_ACCEPTED,
                2 => MavMissionResult::MAV_MISSION_UNSUPPORTED_FRAME,
                13 => MavMissionResult::MAV_MISSION_INVALID_SEQUENCE,
                _ => MavMissionResult::MAV_MISSION_ERROR,
            },
        }),
    )
}

fn mission_request_int(
    sequence: u8,
    system_id: u8,
    component_id: u8,
    target_system: u8,
    target_component: u8,
    item_seq: u16,
) -> Vec<u8> {
    encode_message(
        sequence,
        system_id,
        component_id,
        mavlink::dialects::common::MavMessage::MISSION_REQUEST_INT(MISSION_REQUEST_INT_DATA {
            seq: item_seq,
            target_system,
            target_component,
        }),
    )
}

async fn clear_mission(
    client: &async_nats::Client,
    subject: &str,
    callsign: &str,
    source_system: u8,
    sequence: u8,
) -> bool {
    let request_id = format!(
        "{source_system}-mission-clear-{sequence}-{}",
        unix_ms().unwrap_or_default()
    );
    let payload = serde_json::json!({
        "schema":"furia.mavlink.control","version":"1.0.0",
        "identity":callsign,"request_id":request_id,
        "command":{"type":"mission_clear"}
    });
    tokio::time::timeout(
        std::time::Duration::from_secs(2),
        client.request(subject.to_owned(), payload.to_string().into()),
    )
    .await
    .ok()
    .and_then(|response| response.ok())
    .and_then(|response| serde_json::from_slice::<serde_json::Value>(&response.payload).ok())
    .is_some_and(|value| value["requestId"] == request_id && value["accepted"] == true)
}

fn control_command(frame: &FrameInfo, vehicle_system_id: u8) -> Result<Option<serde_json::Value>> {
    if frame.message_id == 43 {
        return Ok(None);
    }
    if frame.message_id == 11 {
        if frame.payload.len() < 6
            || (frame.payload[4] != 0 && frame.payload[4] != vehicle_system_id)
        {
            bail!("SET_MODE target mismatch");
        }
        if frame.payload[5] & 1 == 0 {
            bail!("SET_MODE requires custom mode enabled");
        }
        let mode = match u32::from_le_bytes(frame.payload[0..4].try_into()?) {
            0 => "STABILIZE",
            1 => "GUIDED",
            2 => "AUTO",
            3 => "HOLD",
            4 => "RTL",
            5 => "LAND",
            _ => bail!("unsupported custom mode"),
        };
        return Ok(Some(
            serde_json::json!({"type":"set_mode","payload":{"mode":mode}}),
        ));
    }
    if frame.message_id == 84 {
        if frame.payload.len() < 53 {
            bail!("SET_POSITION_TARGET_LOCAL_NED truncated");
        }
        let mask = u16::from_le_bytes([frame.payload[48], frame.payload[49]]);
        let target = frame.payload[50];
        let component = frame.payload[51];
        let frame_id = frame.payload[52];
        if (target != 0 && target != vehicle_system_id) || (component != 0 && component != 1) {
            bail!("position target mismatch");
        }
        // Position-only LOCAL_NED: xyz active; velocity, acceleration, yaw and
        // yaw rate ignored; FORCE bit clear.
        if frame_id != 1 || mask != 0x0df8 {
            bail!("only position-only LOCAL_NED targets are supported");
        }
        let number = |offset| -> Result<f64> {
            let value = f32::from_le_bytes(frame.payload[offset..offset + 4].try_into()?) as f64;
            if !value.is_finite() {
                bail!("position target coordinates must be finite");
            }
            Ok(value)
        };
        return Ok(Some(
            serde_json::json!({"type":"set_position","payload":{"north_m":number(4)?,"east_m":number(8)?,"down_m":number(12)?}}),
        ));
    }
    if frame.message_id != 76 {
        return Ok(None);
    }
    if !(32..=33).contains(&frame.payload.len()) {
        bail!("COMMAND_LONG must carry its 32-byte MAVLink 2 minimum payload");
    }
    let params: [f64; 7] = std::array::from_fn(|index| {
        f32::from_le_bytes(frame.payload[index * 4..index * 4 + 4].try_into().unwrap()) as f64
    });
    if params.iter().any(|value| !value.is_finite()) {
        bail!("COMMAND_LONG parameters must be finite");
    }
    let p = |index: usize| params[index];
    let target = frame.payload[30];
    let component = frame.payload[31];
    if target != 0 && target != vehicle_system_id {
        bail!("COMMAND_LONG target mismatch");
    }
    if component != 0 && component != 1 {
        bail!("COMMAND_LONG component mismatch");
    }
    let command = u16::from_le_bytes([frame.payload[28], frame.payload[29]]);
    let value = match command {
        400 if p(0) == 1.0 && p(1) == 0.0 => serde_json::json!({"type":"arm"}),
        400 if p(0) == 0.0 && p(1) == 0.0 => serde_json::json!({"type":"disarm"}),
        22 => serde_json::json!({"type":"takeoff","payload":{"altitude_m":p(6)}}),
        20 if params.iter().all(|value| *value == 0.0) => {
            serde_json::json!({"type":"rtl"})
        }
        20 => bail!("NAV_RETURN_TO_LAUNCH does not accept caller-supplied parameters"),
        21 => serde_json::json!({"type":"land"}),
        176 => {
            if p(0) != 1.0 {
                bail!("MAV_CMD_DO_SET_MODE requires custom mode enabled");
            }
            if params[2..].iter().any(|value| *value != 0.0) {
                bail!("MAV_CMD_DO_SET_MODE submode and extra parameters are unsupported");
            }
            let custom_mode = p(1);
            if custom_mode.fract() != 0.0 || !(0.0..=5.0).contains(&custom_mode) {
                bail!("unsupported custom mode");
            }
            let mode = match custom_mode as u32 {
                0 => "STABILIZE",
                1 => "GUIDED",
                2 => "AUTO",
                3 => "HOLD",
                4 => "RTL",
                5 => "LAND",
                _ => bail!("unsupported custom mode"),
            };
            serde_json::json!({"type":"set_mode","payload":{"mode":mode}})
        }
        300 => serde_json::json!({"type":"mission_start"}),
        193 if p(0) == 0.0 => serde_json::json!({"type":"mission_start"}),
        193 if p(0) == 1.0 => serde_json::json!({"type":"mission_pause"}),
        _ => bail!("unsupported COMMAND_LONG id={command}"),
    };
    Ok(Some(value))
}

fn unix_ms() -> Result<u128> {
    Ok(SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis())
}

fn read_only_request(frame: &FrameInfo, target_system: u8) -> bool {
    // MAVLink HEARTBEAT and REQUEST_DATA_STREAM are read-only. COMMAND_LONG is
    // accepted only for MAV_CMD_REQUEST_MESSAGE (512), which requests telemetry.
    match frame.message_id {
        0 => true,
        66 => frame
            .payload
            .first()
            .is_some_and(|target| *target == 0 || *target == target_system),
        76 => {
            let command = frame
                .payload
                .get(28..30)
                .map(|bytes| u16::from_le_bytes([bytes[0], bytes[1]]));
            command == Some(512)
                && frame
                    .payload
                    .get(30)
                    .is_some_and(|target| *target == 0 || *target == target_system)
        }
        _ => false,
    }
}

async fn publish_frame(
    js: &jetstream::Context,
    callsign: &str,
    direction: &str,
    source: &str,
    raw: &[u8],
    frame: &FrameInfo,
) -> Result<()> {
    let subject = match direction {
        "vehicle_to_gcs" => format!("{TELEMETRY_SUBJECT}.{callsign}"),
        _ => format!("{GCS_COMMAND_SUBJECT}.{callsign}"),
    };
    let envelope = FrameEnvelope {
        schema: "furia.mavlink.frame",
        version: "1.0.0",
        callsign: callsign.to_owned(),
        direction: direction.to_owned(),
        received_at_unix_ms: unix_ms()?,
        source: source.to_owned(),
        system_id: frame.system_id,
        component_id: frame.component_id,
        message_id: frame.message_id,
        sequence: frame.sequence,
        raw_base64: BASE64.encode(raw),
    };
    let payload = serde_json::to_vec(&envelope)?;
    let ack = js.publish(subject, payload.clone().into()).await?;
    ack.await
        .context("JetStream did not acknowledge MAVLink frame")?;
    if let Some(heartbeat_subject) = outgoing_heartbeat_audit_subject(direction, frame, callsign) {
        let ack = js.publish(heartbeat_subject, payload.into()).await?;
        ack.await
            .context("JetStream did not acknowledge outgoing heartbeat")?;
    }
    Ok(())
}

async fn vehicle_to_gcs(
    config: &Config,
    js: jetstream::Context,
    vehicle: std::sync::Arc<tokio::net::UdpSocket>,
    gcs: std::sync::Arc<tokio::net::UdpSocket>,
) -> Result<()> {
    let mut buf = vec![0_u8; 4096];
    loop {
        let (len, src) = vehicle.recv_from(&mut buf).await?;
        if src != config.vehicle_peer {
            continue;
        }
        let raw = &buf[..len];
        let Ok(frame) = validate_frame(raw) else {
            continue;
        };
        if frame.system_id != config.system_id {
            continue;
        }
        publish_frame(
            &js,
            &config.callsign,
            "vehicle_to_gcs",
            &src.to_string(),
            raw,
            &frame,
        )
        .await?;
        gcs.send_to(raw, config.qgc_peer).await?;
    }
}

async fn gcs_to_vehicle(
    config: &Config,
    js: jetstream::Context,
    gcs: std::sync::Arc<tokio::net::UdpSocket>,
    vehicle: std::sync::Arc<tokio::net::UdpSocket>,
) -> Result<()> {
    let mut buf = vec![0_u8; 4096];
    loop {
        let (len, src) = gcs.recv_from(&mut buf).await?;
        if src != config.qgc_peer {
            continue;
        }
        let raw = &buf[..len];
        let Ok(frame) = validate_frame(raw) else {
            continue;
        };
        if frame.system_id == config.system_id || frame.system_id == 0 {
            continue;
        }
        publish_frame(
            &js,
            &config.callsign,
            "gcs_to_vehicle",
            &src.to_string(),
            raw,
            &frame,
        )
        .await?;
        vehicle.send_to(raw, config.vehicle_peer).await?;
    }
}

async fn antoine_nats_telemetry_to_gcs(
    config: Config,
    client: async_nats::Client,
    gcs: std::sync::Arc<tokio::net::UdpSocket>,
    armed_state: std::sync::Arc<tokio::sync::RwLock<bool>>,
    mode_state: std::sync::Arc<tokio::sync::RwLock<u8>>,
) -> Result<()> {
    let subject = format!("furia.telemetry.{}", config.callsign);
    let mut subscription = client.subscribe(subject).await?;
    let mut sequence = 0_u8;
    let mut last_telemetry = tokio::time::Instant::now() - std::time::Duration::from_secs(1);
    while let Some(message) = subscription.next().await {
        let value: serde_json::Value = match serde_json::from_slice(&message.payload) {
            Ok(value) => value,
            Err(error) => {
                tracing::warn!(%error, "invalid Antoine telemetry JSON");
                continue;
            }
        };
        if value.get("identity").and_then(serde_json::Value::as_str) != Some(&config.callsign) {
            continue;
        }
        let now = tokio::time::Instant::now();
        if now.duration_since(last_telemetry) < std::time::Duration::from_millis(100) {
            continue;
        }
        last_telemetry = now;
        let telemetry_armed = value["armed"]
            .as_bool()
            .unwrap_or(*armed_state.read().await);
        let telemetry_mode = value["mode_id"]
            .as_u64()
            .map(|id| id as u8)
            .unwrap_or(*mode_state.read().await);
        let frames = match telemetry_frames(
            &value,
            config.system_id,
            config.home_alt_msl,
            config.home_lat_deg,
            config.home_lon_deg,
            sequence,
            telemetry_armed,
            telemetry_mode,
        ) {
            Ok(frames) => frames,
            Err(error) => {
                tracing::warn!(%error, "invalid Antoine telemetry");
                continue;
            }
        };
        for raw in frames {
            // Model telemetry is live telemetry, not an event log. Publishing it
            // to Core NATS avoids writing a 90 msg/s GPS stream to JetStream.
            // Keep telemetry on the explicitly configured peer. A loopback
            // probe or another local UDP client must not be able to redirect
            // the live QGC telemetry stream by sending one valid MAVLink frame.
            gcs.send_to(&raw, config.qgc_peer).await?;
            if let Some(observer) = config.qgc_telemetry_peer {
                gcs.send_to(&raw, observer).await?;
            }
            sequence = sequence.wrapping_add(1);
        }
    }
    Ok(())
}

async fn qgc_to_antoine_nats(
    config: Config,
    js: jetstream::Context,
    client: async_nats::Client,
    gcs: std::sync::Arc<tokio::net::UdpSocket>,
    armed_state: std::sync::Arc<tokio::sync::RwLock<bool>>,
    mode_state: std::sync::Arc<tokio::sync::RwLock<u8>>,
) -> Result<()> {
    let command_subject = config.control_subject.clone();
    let mut buffer = vec![0_u8; 4096];
    let mut mission_upload: Option<(SocketAddr, u8, u8, Vec<Option<[f64; 3]>>)> = None;
    let mut uploaded_waypoints: Vec<[f64; 3]> = Vec::new();
    loop {
        let (length, source) = gcs.recv_from(&mut buffer).await?;
        if !source.ip().is_loopback() {
            tracing::warn!(%source, "QGC datagram rejected from non-loopback peer");
            continue;
        }
        if source != config.qgc_peer {
            tracing::warn!(%source, expected=%config.qgc_peer, "QGC control datagram rejected from an unconfigured loopback peer");
            continue;
        }
        let raw = &buffer[..length];
        let frame = match validate_frame(raw) {
            Ok(frame) => frame,
            Err(error) => {
                tracing::debug!(%source, %error, bytes=length, "invalid QGC MAVLink frame ignored");
                continue;
            }
        };
        if frame.system_id == 0 || frame.system_id == config.system_id {
            continue;
        }
        // Record every valid GCS-originated control-plane frame before acting
        // on it, including the mission microservice handshake.
        publish_frame(
            &js,
            &config.callsign,
            "gcs_to_antoine",
            &source.to_string(),
            raw,
            &frame,
        )
        .await?;
        tracing::debug!(
            source_system = frame.system_id,
            source_component = frame.component_id,
            message_id = frame.message_id,
            "QGC MAVLink frame received"
        );
        if frame.message_id == 44 {
            if frame.payload.len() < 4 || frame.payload.len() > 5 {
                continue;
            }
            let count = u16::from_le_bytes([frame.payload[0], frame.payload[1]]) as usize;
            let target = frame.payload[2];
            let component = frame.payload[3];
            let mission_type = frame.payload.get(4).copied().unwrap_or(0);
            if (target != 0 && target != config.system_id)
                || (component != 0 && component != 1)
                || mission_type != 0
                || count > 5
            {
                let ack = mission_ack(
                    frame.sequence.wrapping_add(1),
                    config.system_id,
                    frame.system_id,
                    frame.component_id,
                    1,
                );
                gcs.send_to(&ack, source).await?;
                continue;
            }
            if count == 0 {
                mission_upload = None;
                let accepted = clear_mission(
                    &client,
                    &command_subject,
                    &config.callsign,
                    frame.system_id,
                    frame.sequence,
                )
                .await;
                if accepted {
                    uploaded_waypoints.clear();
                }
                let ack = mission_ack(
                    frame.sequence.wrapping_add(1),
                    config.system_id,
                    frame.system_id,
                    frame.component_id,
                    if accepted { 0 } else { 1 },
                );
                gcs.send_to(&ack, source).await?;
                continue;
            }
            if let Some((old_source, old_system, old_component, _)) = mission_upload.take() {
                let ack = mission_ack(
                    frame.sequence.wrapping_add(1),
                    config.system_id,
                    old_system,
                    old_component,
                    1,
                );
                gcs.send_to(&ack, old_source).await?;
            }
            mission_upload = Some((
                source,
                frame.system_id,
                frame.component_id,
                vec![None; count],
            ));
            let request = mission_request_int(
                frame.sequence.wrapping_add(1),
                config.system_id,
                1,
                frame.system_id,
                frame.component_id,
                0,
            );
            gcs.send_to(&request, source).await?;
            continue;
        }
        if frame.message_id == 45 {
            if frame.payload.len() < 2
                || (frame.payload[0] != 0 && frame.payload[0] != config.system_id)
                || (frame.payload[1] != 0 && frame.payload[1] != 1)
                || frame.payload.get(2).copied().unwrap_or(0) != 0
            {
                let ack = mission_ack(
                    frame.sequence.wrapping_add(1),
                    config.system_id,
                    frame.system_id,
                    frame.component_id,
                    1,
                );
                gcs.send_to(&ack, source).await?;
                continue;
            }
            mission_upload = None;
            let accepted = clear_mission(
                &client,
                &command_subject,
                &config.callsign,
                frame.system_id,
                frame.sequence,
            )
            .await;
            if accepted {
                uploaded_waypoints.clear();
            }
            let ack = mission_ack(
                frame.sequence.wrapping_add(1),
                config.system_id,
                frame.system_id,
                frame.component_id,
                if accepted { 0 } else { 1 },
            );
            gcs.send_to(&ack, source).await?;
            continue;
        }
        if frame.message_id == 43 {
            tracing::debug!("mission list requested");
            if frame.payload.len() < 2
                || (frame.payload[0] != 0 && frame.payload[0] != config.system_id)
                || (frame.payload[1] != 0 && frame.payload[1] != 1)
                || frame.payload.get(2).copied().unwrap_or(0) != 0
            {
                continue;
            }
            let request_id = format!(
                "{}-mission-query-{}-{}",
                frame.system_id,
                frame.sequence,
                unix_ms()?
            );
            let query = mission_query_envelope(&config, request_id.clone());
            let query_response = tokio::time::timeout(
                std::time::Duration::from_secs(2),
                client.request(command_subject.clone(), query.to_string().into()),
            )
            .await;
            let live_waypoints = match query_response {
                Err(_) => {
                    tracing::warn!(
                        request_id,
                        "mission query timed out; returning a mission error"
                    );
                    None
                }
                Ok(Err(error)) => {
                    tracing::warn!(request_id, %error, "mission query failed; returning a mission error");
                    None
                }
                Ok(Ok(response)) => {
                    match serde_json::from_slice::<serde_json::Value>(&response.payload) {
                        Err(error) => {
                            tracing::warn!(request_id, %error, "mission query returned invalid JSON; returning a mission error");
                            None
                        }
                        Ok(value)
                            if value["requestId"] != request_id || value["accepted"] != true =>
                        {
                            tracing::warn!(
                                request_id,
                                returned_request_id = ?value["requestId"],
                                accepted = ?value["accepted"],
                                "mission query response did not match the request; returning a mission error"
                            );
                            None
                        }
                        Ok(value) if value["missionUploaded"] == false => {
                            tracing::debug!(request_id, "controller reports no uploaded mission");
                            Some(Vec::new())
                        }
                        Ok(value) if value["missionUploaded"] == true => {
                            match serde_json::from_value::<Vec<[f64; 3]>>(
                                value["waypoints"].clone(),
                            ) {
                                Err(error) => {
                                    tracing::warn!(request_id, %error, "mission query returned invalid waypoints; returning a mission error");
                                    None
                                }
                                Ok(points)
                                    if points.len() <= 5
                                        && points.iter().all(|point| {
                                            point.iter().all(|value| value.is_finite())
                                                && point[0].abs() <= 3.0
                                                && point[1].abs() <= 3.0
                                                && (-3.0..=0.0).contains(&point[2])
                                        }) =>
                                {
                                    tracing::debug!(
                                        request_id,
                                        waypoint_count = points.len(),
                                        "mission query succeeded"
                                    );
                                    Some(points)
                                }
                                Ok(_) => {
                                    tracing::warn!(
                                        request_id,
                                        "mission query waypoints exceed the local envelope; returning a mission error"
                                    );
                                    None
                                }
                            }
                        }
                        Ok(_) => {
                            tracing::warn!(
                                request_id,
                                "mission query omitted a valid missionUploaded flag; returning a mission error"
                            );
                            None
                        }
                    }
                }
            };
            let Some(live_waypoints) = live_waypoints else {
                let ack = mission_ack(
                    frame.sequence.wrapping_add(1),
                    config.system_id,
                    frame.system_id,
                    frame.component_id,
                    1,
                );
                gcs.send_to(&ack, source).await?;
                continue;
            };
            uploaded_waypoints = live_waypoints;
            let count = uploaded_waypoints.len() as u16;
            let response = encode_message(
                frame.sequence.wrapping_add(1),
                config.system_id,
                1,
                mavlink::dialects::common::MavMessage::MISSION_COUNT(MISSION_COUNT_DATA {
                    count,
                    target_system: frame.system_id,
                    target_component: frame.component_id,
                }),
            );
            gcs.send_to(&response, source).await?;
            continue;
        }
        if frame.message_id == 51 {
            if frame.payload.len() < 4
                || frame.payload[2] != config.system_id
                || frame.payload[3] != 1
                || frame.payload.get(4).copied().unwrap_or(0) != 0
            {
                continue;
            }
            let item_seq = u16::from_le_bytes([frame.payload[0], frame.payload[1]]) as usize;
            if let Some(point) = uploaded_waypoints.get(item_seq) {
                let cos_lat = config.home_lat_deg.to_radians().cos();
                let latitude = config.home_lat_deg + point[0] / 111_111.0;
                let longitude = config.home_lon_deg + point[1] / (111_111.0 * cos_lat);
                let response = encode_message(
                    frame.sequence.wrapping_add(1),
                    config.system_id,
                    1,
                    mavlink::dialects::common::MavMessage::MISSION_ITEM_INT(
                        MISSION_ITEM_INT_DATA {
                            param1: 0.0,
                            param2: 0.0,
                            param3: 0.0,
                            param4: 0.0,
                            x: (latitude * 1e7) as i32,
                            y: (longitude * 1e7) as i32,
                            z: (-point[2]) as f32,
                            seq: item_seq as u16,
                            command: MavCmd::MAV_CMD_NAV_WAYPOINT,
                            target_system: frame.system_id,
                            target_component: frame.component_id,
                            frame: MavFrame::MAV_FRAME_GLOBAL_RELATIVE_ALT,
                            current: u8::from(item_seq == 0),
                            autocontinue: 1,
                        },
                    ),
                );
                gcs.send_to(&response, source).await?;
            } else {
                let ack = mission_ack(
                    frame.sequence.wrapping_add(1),
                    config.system_id,
                    frame.system_id,
                    frame.component_id,
                    13,
                );
                gcs.send_to(&ack, source).await?;
            }
            continue;
        }
        if frame.message_id == 73 {
            if frame.payload.len() < 37 {
                continue;
            }
            let seq = u16::from_le_bytes([frame.payload[28], frame.payload[29]]) as usize;
            let command_id = u16::from_le_bytes([frame.payload[30], frame.payload[31]]);
            let target = frame.payload[32];
            let component = frame.payload[33];
            let coord_frame = frame.payload[34];
            if (target != 0 && target != config.system_id)
                || (component != 0 && component != 1)
                || !matches!(coord_frame, 1 | 3 | 6)
                || command_id != 16
                || frame.payload[35] > 1
                || frame.payload[36] > 1
            {
                mission_upload = None;
                let ack = mission_ack(
                    frame.sequence.wrapping_add(1),
                    config.system_id,
                    frame.system_id,
                    frame.component_id,
                    2,
                );
                gcs.send_to(&ack, source).await?;
                continue;
            }
            let wire_x = i32::from_le_bytes(frame.payload[16..20].try_into()?) as f64;
            let wire_y = i32::from_le_bytes(frame.payload[20..24].try_into()?) as f64;
            let wire_z = f32::from_le_bytes(frame.payload[24..28].try_into()?) as f64;
            let (x, y, z) = if coord_frame == 1 {
                (wire_x / 10_000.0, wire_y / 10_000.0, wire_z)
            } else {
                let north = (wire_x / 1e7 - config.home_lat_deg) * 111_111.0;
                let east = (wire_y / 1e7 - config.home_lon_deg)
                    * 111_111.0
                    * config.home_lat_deg.to_radians().cos();
                (north, east, -wire_z)
            };
            if let Some((upload_source, sender, component, items)) = mission_upload.as_mut() {
                if *upload_source != source
                    || *sender != frame.system_id
                    || *component != frame.component_id
                {
                    let ack = mission_ack(
                        frame.sequence.wrapping_add(1),
                        config.system_id,
                        frame.system_id,
                        frame.component_id,
                        1,
                    );
                    gcs.send_to(&ack, source).await?;
                    continue;
                }
                let expected = items.iter().position(Option::is_none);
                if seq >= items.len()
                    || expected != Some(seq)
                    || !x.is_finite()
                    || !y.is_finite()
                    || !z.is_finite()
                    || x.abs() > 3.0
                    || y.abs() > 3.0
                    || !(-3.0..=0.0).contains(&z)
                {
                    mission_upload = None;
                    let ack = mission_ack(
                        frame.sequence.wrapping_add(1),
                        config.system_id,
                        frame.system_id,
                        frame.component_id,
                        2,
                    );
                    gcs.send_to(&ack, source).await?;
                    continue;
                }
                items[seq] = Some([x, y, z]);
                if seq + 1 < items.len() {
                    let request = mission_request_int(
                        frame.sequence.wrapping_add(1),
                        config.system_id,
                        1,
                        frame.system_id,
                        frame.component_id,
                        (seq + 1) as u16,
                    );
                    gcs.send_to(&request, source).await?;
                } else if items.iter().all(Option::is_some) {
                    let waypoints: Vec<[f64; 3]> =
                        items.iter().filter_map(|point| *point).collect();
                    let request_id = format!(
                        "{}-mission-{}-{}",
                        frame.system_id,
                        frame.sequence,
                        unix_ms()?
                    );
                    let payload = serde_json::json!({"schema":"furia.mavlink.control","version":"1.0.0","identity":config.callsign,"request_id":request_id,"command":{"type":"mission_upload","payload":{"waypoints":waypoints}}});
                    let accepted = tokio::time::timeout(
                        std::time::Duration::from_secs(2),
                        client.request(command_subject.clone(), payload.to_string().into()),
                    )
                    .await
                    .ok()
                    .and_then(|r| r.ok())
                    .and_then(|r| serde_json::from_slice::<serde_json::Value>(&r.payload).ok())
                    .is_some_and(|v| v["accepted"] == true && v["requestId"] == request_id);
                    if accepted {
                        uploaded_waypoints = waypoints;
                    }
                    let ack = mission_ack(
                        frame.sequence.wrapping_add(1),
                        config.system_id,
                        frame.system_id,
                        frame.component_id,
                        if accepted { 0 } else { 1 },
                    );
                    gcs.send_to(&ack, source).await?;
                    mission_upload = None;
                }
            } else {
                let ack = mission_ack(
                    frame.sequence.wrapping_add(1),
                    config.system_id,
                    frame.system_id,
                    frame.component_id,
                    13,
                );
                gcs.send_to(&ack, source).await?;
            }
            continue;
        }
        let request_id = format!("{}-{}-{}", frame.system_id, frame.sequence, unix_ms()?);
        let command = match control_command(&frame, config.system_id) {
            Ok(Some(command)) => command,
            Ok(None) => continue,
            Err(error) => {
                tracing::debug!(%error, message_id=frame.message_id, "unsupported MAVLink command rejected");
                continue;
            }
        };
        // The frame was durably audited above, before publishing the
        // non-replayable control request to Core NATS.
        let payload = serde_json::json!({"schema":"furia.mavlink.control","version":"1.0.0","identity":config.callsign,"request_id":request_id,"command":command});
        tracing::debug!(request_id, request=%payload, "publishing Antoine control request");
        let response = tokio::time::timeout(
            std::time::Duration::from_secs(2),
            client.request(command_subject.clone(), payload.to_string().into()),
        )
        .await;
        let accepted = match response {
            Ok(Ok(message)) => {
                tracing::debug!(request_id, response=%String::from_utf8_lossy(&message.payload), "Antoine control response");
                serde_json::from_slice::<serde_json::Value>(&message.payload)
                .ok()
                .filter(|value| value["requestId"] == request_id)
                .map(|value| {
                    if value["accepted"] != true {
                        tracing::warn!(request_id, reason=?value["reason"], "Antoine rejected MAVLink control request");
                        return false;
                    }
                    if let Some(armed) = value["armed"].as_bool() {
                        let _ = armed_state.try_write().map(|mut current| *current = armed);
                    }
                    if let Some(mode) = value["mode"].as_str() {
                        let id = match mode {
                            "STABILIZE" => 0,
                            "GUIDED" => 1,
                            "AUTO" => 2,
                            "HOLD" => 3,
                            "RTL" => 4,
                            "LAND" => 5,
                            _ => 255,
                        };
                        if id != 255 {
                            let _ = mode_state.try_write().map(|mut current| *current = id);
                        }
                    }
                    true
                })
                .unwrap_or(false)
            }
            _ => false,
        };
        if let Some(id) = command_id(&frame) {
            let ack = command_ack(
                frame.sequence.wrapping_add(1),
                config.system_id,
                frame.system_id,
                frame.component_id,
                id,
                accepted,
            );
            gcs.send_to(&ack, source).await?;
        }
    }
}

async fn run_antoine_nats_mode(
    config: Config,
    js: jetstream::Context,
    client: async_nats::Client,
) -> Result<()> {
    let gcs = std::sync::Arc::new(
        tokio::net::UdpSocket::bind(config.qgc_bind)
            .await
            .with_context(|| format!("bind QGC UDP {}", config.qgc_bind))?,
    );
    let telemetry_config = config.clone();
    let command_config = config;
    let audit_client = client.clone();
    let command_js = js;
    let telemetry_client = client.clone();
    let telemetry_gcs = gcs.clone();
    let command_gcs = gcs;
    let command_audit_client = audit_client;
    let armed_state = std::sync::Arc::new(tokio::sync::RwLock::new(false));
    let mode_state = std::sync::Arc::new(tokio::sync::RwLock::new(0_u8));
    let command_armed = armed_state.clone();
    let command_mode = mode_state.clone();
    let telemetry_armed = armed_state;
    let telemetry_mode = mode_state;
    tracing::info!(callsign=%telemetry_config.callsign, "Antoine NATS model adapter ready; bounded simulation controls require the opt-in native SITL");
    let telemetry = tokio::spawn(async move {
        antoine_nats_telemetry_to_gcs(
            telemetry_config,
            telemetry_client,
            telemetry_gcs,
            telemetry_armed,
            telemetry_mode,
        )
        .await
    });
    let commands = tokio::spawn(async move {
        qgc_to_antoine_nats(
            command_config,
            command_js,
            command_audit_client,
            command_gcs,
            command_armed,
            command_mode,
        )
        .await
    });
    tokio::select! {
        result = telemetry => result??,
        result = commands => result??,
        _ = tokio::signal::ctrl_c() => {},
    }
    Ok(())
}

async fn readonly_jetstream_to_vehicle(
    config: &Config,
    js: jetstream::Context,
    vehicle: std::sync::Arc<tokio::net::UdpSocket>,
) -> Result<()> {
    let stream = js.get_stream(STREAM).await?;
    let consumer = stream
        .get_or_create_consumer(
            DURABLE,
            consumer::pull::Config {
                durable_name: Some(DURABLE.into()),
                filter_subject: format!("{COMMAND_SUBJECT}.{}", config.callsign),
                ack_policy: consumer::AckPolicy::Explicit,
                deliver_policy: consumer::DeliverPolicy::New,
                ..Default::default()
            },
        )
        .await?;
    let mut messages = consumer.messages().await?;
    while let Some(message) = messages.next().await {
        let message = message?;
        let result = async {
            let value: serde_json::Value = serde_json::from_slice(&message.payload)?;
            let encoded = value
                .get("raw_base64")
                .and_then(|v| v.as_str())
                .context("missing raw_base64")?;
            let raw = BASE64.decode(encoded)?;
            let frame = validate_frame(&raw)?;
            if !read_only_request(&frame, config.system_id) {
                bail!("MAVLink message is outside the read-only request allowlist");
            }
            vehicle.send_to(&raw, config.vehicle_peer).await?;
            Ok::<(), anyhow::Error>(())
        }
        .await;
        if let Err(error) = result {
            tracing::warn!(%error, "JetStream request rejected");
        }
        message
            .ack()
            .await
            .map_err(|error| anyhow::anyhow!("JetStream ack failed: {error}"))?;
    }
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::builder()
                .with_default_directive(tracing::Level::INFO.into())
                .from_env_lossy(),
        )
        .init();
    let config = config()?;
    let client = async_nats::connect(&config.nats_url).await?;
    let js = jetstream::new(client.clone());
    js.create_or_update_stream(stream::Config {
        name: STREAM.into(),
        subjects: vec![
            format!("{TELEMETRY_SUBJECT}.>"),
            format!("{OUTGOING_HEARTBEAT_SUBJECT}.>"),
            format!("{GCS_COMMAND_SUBJECT}.>"),
            format!("{COMMAND_SUBJECT}.>"),
        ],
        storage: stream::StorageType::File,
        retention: stream::RetentionPolicy::Limits,
        max_age: std::time::Duration::from_secs(24 * 60 * 60),
        max_messages: 100_000,
        ..Default::default()
    })
    .await?;
    if matches!(config.mode.as_str(), "antoine-nats" | "gazebo-nats") {
        return run_antoine_nats_mode(config, js, client).await;
    }
    let vehicle = std::sync::Arc::new(
        tokio::net::UdpSocket::bind(config.vehicle_bind)
            .await
            .with_context(|| format!("bind vehicle UDP {}", config.vehicle_bind))?,
    );
    let gcs = std::sync::Arc::new(
        tokio::net::UdpSocket::bind(config.qgc_bind)
            .await
            .with_context(|| format!("bind QGC UDP {}", config.qgc_bind))?,
    );
    tracing::info!(callsign=%config.callsign, vehicle_bind=%config.vehicle_bind, vehicle_peer=%config.vehicle_peer, qgc_bind=%config.qgc_bind, qgc_peer=%config.qgc_peer, "loopback MAVLink JetStream bridge ready; QGC commands are routed to the pinned SITL peer");

    let v2g_config = Config {
        mode: config.mode.clone(),
        home_alt_msl: config.home_alt_msl,
        home_lat_deg: config.home_lat_deg,
        home_lon_deg: config.home_lon_deg,
        callsign: config.callsign.clone(),
        nats_url: config.nats_url.clone(),
        vehicle_bind: config.vehicle_bind,
        vehicle_peer: config.vehicle_peer,
        qgc_bind: config.qgc_bind,
        qgc_peer: config.qgc_peer,
        qgc_telemetry_peer: config.qgc_telemetry_peer,
        system_id: config.system_id,
        control_subject: config.control_subject.clone(),
    };
    let g2j_config = Config {
        mode: config.mode.clone(),
        home_alt_msl: config.home_alt_msl,
        home_lat_deg: config.home_lat_deg,
        home_lon_deg: config.home_lon_deg,
        callsign: config.callsign.clone(),
        nats_url: config.nats_url.clone(),
        vehicle_bind: config.vehicle_bind,
        vehicle_peer: config.vehicle_peer,
        qgc_bind: config.qgc_bind,
        qgc_peer: config.qgc_peer,
        qgc_telemetry_peer: config.qgc_telemetry_peer,
        system_id: config.system_id,
        control_subject: config.control_subject.clone(),
    };
    let j2v_config = Config {
        mode: config.mode.clone(),
        home_alt_msl: config.home_alt_msl,
        home_lat_deg: config.home_lat_deg,
        home_lon_deg: config.home_lon_deg,
        callsign: config.callsign.clone(),
        nats_url: config.nats_url.clone(),
        vehicle_bind: config.vehicle_bind,
        vehicle_peer: config.vehicle_peer,
        qgc_bind: config.qgc_bind,
        qgc_peer: config.qgc_peer,
        qgc_telemetry_peer: config.qgc_telemetry_peer,
        system_id: config.system_id,
        control_subject: config.control_subject.clone(),
    };
    let v2g_vehicle = vehicle.clone();
    let v2g_gcs = gcs.clone();
    let g2v_gcs = gcs.clone();
    let g2v_vehicle = vehicle.clone();
    let v2g_js = js.clone();
    let g2v_js = js.clone();
    let v2g =
        tokio::spawn(
            async move { vehicle_to_gcs(&v2g_config, v2g_js, v2g_vehicle, v2g_gcs).await },
        );
    let g2v =
        tokio::spawn(
            async move { gcs_to_vehicle(&g2j_config, g2v_js, g2v_gcs, g2v_vehicle).await },
        );
    let j2v_vehicle = vehicle.clone();
    let j2v =
        tokio::spawn(
            async move { readonly_jetstream_to_vehicle(&j2v_config, js, j2v_vehicle).await },
        );
    tokio::select! {
        result = v2g => result??,
        result = g2v => result??,
        result = j2v => result??,
        _ = tokio::signal::ctrl_c() => {},
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_config(mode: &str) -> Config {
        Config {
            callsign: "drone-1".to_owned(),
            nats_url: "nats://127.0.0.1:4222".to_owned(),
            vehicle_bind: "127.0.0.1:14560".parse().unwrap(),
            vehicle_peer: "127.0.0.1:14561".parse().unwrap(),
            qgc_bind: "127.0.0.1:14562".parse().unwrap(),
            qgc_peer: "127.0.0.1:14550".parse().unwrap(),
            qgc_telemetry_peer: None,
            system_id: 1,
            home_alt_msl: 82.0,
            home_lat_deg: 48.0002,
            home_lon_deg: 0.3564,
            mode: mode.to_owned(),
            control_subject: "furia.mavlink.control.drone-1".to_owned(),
        }
    }

    #[test]
    fn mission_query_uses_the_selected_plant_request_envelope() {
        let native = mission_query_envelope(&test_config("antoine-nats"), "native-query".into());
        assert_eq!(native["type"], "mission_query");
        assert!(native.get("command").is_none());

        let gazebo = mission_query_envelope(&test_config("gazebo-nats"), "gazebo-query".into());
        assert_eq!(
            gazebo["command"],
            serde_json::json!({"type":"mission_query","payload":{}})
        );
        assert!(gazebo.get("type").is_none());
        assert_eq!(gazebo["request_id"], "gazebo-query");
    }

    #[test]
    fn only_vehicle_heartbeat_uses_dedicated_outgoing_audit_subject() {
        let heartbeat = FrameInfo {
            system_id: 1,
            component_id: 1,
            message_id: 0,
            sequence: 0,
            payload: vec![],
        };
        assert_eq!(
            outgoing_heartbeat_audit_subject("vehicle_to_gcs", &heartbeat, "drone-1"),
            Some("furia.mavlink.heartbeat.drone-1".to_owned())
        );
        assert_eq!(
            outgoing_heartbeat_audit_subject("gcs_to_antoine", &heartbeat, "drone-1"),
            None
        );
        let position = FrameInfo {
            message_id: 33,
            ..heartbeat
        };
        assert_eq!(
            outgoing_heartbeat_audit_subject("vehicle_to_gcs", &position, "drone-1"),
            None
        );
    }

    #[test]
    fn local_antoine_altitude_maps_to_msl_and_relative_mavlink_altitudes() {
        let value = serde_json::json!({
            "time_s":1.0,"lat_deg":48.0,"lon_deg":0.3,"alt_m":0.0,
            "vn_mps":0.0,"ve_mps":0.0,"vd_mps":0.0,"actuator_thrust":0.0,
            "ground_contact":true,"gnss_degraded":false,"battery_pct":100,"battery_mv":12000
        });
        let frames = telemetry_frames(&value, 1, 82.0, 48.0002, 0.3564, 0, false, 0).unwrap();
        let position = validate_frame(&frames[1]).unwrap();
        assert_eq!(position.message_id, 33);
        assert_eq!(
            i32::from_le_bytes(position.payload[12..16].try_into().unwrap()),
            82_000
        );
        assert_eq!(
            i32::from_le_bytes(position.payload[16..20].try_into().unwrap()),
            0
        );

        let gps = validate_frame(&frames[2]).unwrap();
        assert_eq!(gps.message_id, 24);
        assert_eq!(
            i32::from_le_bytes(gps.payload[16..20].try_into().unwrap()),
            82_000
        );
        let home = validate_frame(&frames[3]).unwrap();
        assert_eq!(home.message_id, 242);
        assert_eq!(
            i32::from_le_bytes(home.payload[8..12].try_into().unwrap()),
            82_000
        );

        let status = validate_frame(&frames[4]).unwrap();
        assert_eq!(status.message_id, 1);
        let prearm_bit = MavSysStatusSensor::MAV_SYS_STATUS_PREARM_CHECK.bits();
        assert_eq!(
            u32::from_le_bytes(status.payload[4..8].try_into().unwrap()),
            prearm_bit
        );
        assert_eq!(
            u32::from_le_bytes(status.payload[8..12].try_into().unwrap()),
            prearm_bit,
            "healthy ground model reports QGC ready-to-arm"
        );
        assert_eq!(
            u16::from_le_bytes(status.payload[14..16].try_into().unwrap()),
            12_000
        );
        assert_eq!(status.payload[30], 100);

        let airborne = serde_json::json!({
            "time_s":1.0,"lat_deg":48.0,"lon_deg":0.3,"alt_m":1.0,
            "vn_mps":0.0,"ve_mps":0.0,"vd_mps":0.0,"actuator_thrust":0.2,
            "ground_contact":false,"gnss_degraded":false,"battery_pct":100,"battery_mv":12000
        });
        let airborne_status =
            telemetry_frames(&airborne, 1, 82.0, 48.0002, 0.3564, 4, true, 1).unwrap();
        let airborne_status = validate_frame(&airborne_status[4]).unwrap();
        assert_eq!(
            u32::from_le_bytes(airborne_status.payload[8..12].try_into().unwrap()),
            0,
            "airborne model must not advertise pre-arm readiness"
        );
    }

    #[test]
    fn gazebo_antoine_telemetry_uses_observed_time_and_model_prearm_gate() {
        let observed_at_unix_ms = unix_ms().unwrap() as u64;
        let ground = serde_json::json!({
            "identity":"drone-1","protocol":"antoine-gz",
            "observedAtUnixMs":observed_at_unix_ms,
            "lat_deg":48.0002,"lon_deg":0.3564,"alt_m":0.09,
            "vn_mps":0.0,"ve_mps":0.0,"vd_mps":0.0,
            "battery_pct":87,"control_authority":"safe-hold"
        });
        let frames = telemetry_frames(&ground, 1, 82.0, 48.0002, 0.3564, 0, false, 0).unwrap();
        assert_eq!(frames.len(), 5);
        let position = validate_frame(&frames[1]).unwrap();
        assert_eq!(position.message_id, 33);
        assert_eq!(
            u32::from_le_bytes(position.payload[0..4].try_into().unwrap()),
            observed_at_unix_ms as u32
        );
        assert_eq!(
            i32::from_le_bytes(position.payload[12..16].try_into().unwrap()),
            82_090
        );
        let status = validate_frame(&frames[4]).unwrap();
        assert_eq!(status.message_id, 1);
        assert_ne!(
            u32::from_le_bytes(status.payload[8..12].try_into().unwrap()),
            0,
            "grounded Gazebo model with fresh local telemetry is armable"
        );
        assert_eq!(
            u16::from_le_bytes(status.payload[14..16].try_into().unwrap()),
            18_960,
            "Gazebo battery voltage is modelled from state of charge"
        );

        let airborne = serde_json::json!({
            "identity":"drone-1","protocol":"antoine-gz",
            "observedAtUnixMs":observed_at_unix_ms,
            "lat_deg":48.0002,"lon_deg":0.3564,"alt_m":1.0,
            "vn_mps":0.0,"ve_mps":0.0,"vd_mps":0.0,
            "battery_pct":87,"control_authority":"authorized"
        });
        let frames = telemetry_frames(&airborne, 1, 82.0, 48.0002, 0.3564, 5, true, 1).unwrap();
        let status = validate_frame(&frames[4]).unwrap();
        assert_eq!(
            u32::from_le_bytes(status.payload[8..12].try_into().unwrap()),
            0,
            "airborne Gazebo model never advertises pre-arm readiness"
        );
    }

    #[test]
    fn gazebo_model_prearm_gate_fails_closed_on_unsafe_inputs() {
        let observed_at_unix_ms = unix_ms().unwrap() as u64;
        let healthy_ground = serde_json::json!({
            "identity":"drone-1","protocol":"antoine-gz",
            "observedAtUnixMs":observed_at_unix_ms,
            "lat_deg":48.0002,"lon_deg":0.3564,"alt_m":0.09,
            "vn_mps":0.0,"ve_mps":0.0,"vd_mps":0.0,
            "battery_pct":20.0,"control_authority":"safe-hold"
        });
        let has_prearm_health = |value: &serde_json::Value| {
            let frames = telemetry_frames(value, 1, 82.0, 48.0002, 0.3564, 0, false, 0).unwrap();
            let status = validate_frame(&frames[4]).unwrap();
            u32::from_le_bytes(status.payload[8..12].try_into().unwrap())
                & MavSysStatusSensor::MAV_SYS_STATUS_PREARM_CHECK.bits()
                != 0
        };

        assert!(
            has_prearm_health(&healthy_ground),
            "20% battery is the inclusive floor"
        );

        let mut unsafe_cases = Vec::new();
        let mut value = healthy_ground.clone();
        value["control_authority"] = serde_json::json!("authorized");
        unsafe_cases.push(("authority is not safe-hold", value));

        let mut value = healthy_ground.clone();
        value["alt_m"] = serde_json::json!(0.101);
        unsafe_cases.push(("altitude exceeds the ground gate", value));

        let mut value = healthy_ground.clone();
        value["vn_mps"] = serde_json::json!(0.151);
        unsafe_cases.push(("horizontal speed exceeds the settled gate", value));

        let mut value = healthy_ground.clone();
        value["battery_pct"] = serde_json::json!(19.999);
        unsafe_cases.push(("battery is below the inclusive floor", value));

        for (description, value) in unsafe_cases {
            assert!(
                !has_prearm_health(&value),
                "model pre-arm health must be clear when {description}"
            );
        }
    }

    #[test]
    fn gazebo_antoine_telemetry_rejects_stale_and_far_future_observations() {
        let now_ms = unix_ms().unwrap() as u64;
        let make = |observed_at_unix_ms| {
            serde_json::json!({
                "identity":"drone-1","protocol":"antoine-gz",
                "observedAtUnixMs":observed_at_unix_ms,
                "lat_deg":48.0002,"lon_deg":0.3564,"alt_m":0.0,
                "vn_mps":0.0,"ve_mps":0.0,"vd_mps":0.0,
                "ground_contact":true,"gnss_degraded":false,"battery_pct":87,
                "control_authority":"safe-hold"
            })
        };
        assert!(
            telemetry_frames(&make(now_ms - 2_000), 1, 82.0, 48.0002, 0.3564, 0, false, 0)
                .unwrap_err()
                .to_string()
                .contains("stale")
        );
        assert!(telemetry_frames(
            &make(now_ms + MAX_GAZEBO_TELEMETRY_FUTURE_MS + 1_000),
            1,
            82.0,
            48.0002,
            0.3564,
            0,
            false,
            0
        )
        .unwrap_err()
        .to_string()
        .contains("future"));
    }

    #[test]
    fn gazebo_observation_timestamp_requires_bounded_integer_milliseconds() {
        let now_ms = unix_ms().unwrap() as u64;
        let make = |observed_at_unix_ms| {
            serde_json::json!({
                "identity":"drone-1","protocol":"antoine-gz",
                "observedAtUnixMs":observed_at_unix_ms,
                "lat_deg":48.0002,"lon_deg":0.3564,"alt_m":0.0,
                "vn_mps":0.0,"ve_mps":0.0,"vd_mps":0.0,
                "battery_pct":87,"control_authority":"safe-hold"
            })
        };
        for malformed in [
            serde_json::json!(now_ms as f64 + 0.5),
            serde_json::json!(-1),
            serde_json::json!(u64::MAX),
        ] {
            let error = telemetry_frames(&make(malformed), 1, 82.0, 48.0002, 0.3564, 0, false, 0)
                .unwrap_err()
                .to_string();
            assert!(
                error.contains("observedAtUnixMs"),
                "unexpected error: {error}"
            );
        }
    }

    #[test]
    fn translates_bounded_mavlink_command_and_local_position_frames() {
        let mut command = vec![0_u8; 32];
        command[..4].copy_from_slice(&1.0_f32.to_le_bytes());
        command[28..30].copy_from_slice(&400_u16.to_le_bytes());
        command[30] = 1;
        let arm = FrameInfo {
            system_id: 255,
            component_id: 190,
            message_id: 76,
            sequence: 1,
            payload: command,
        };
        assert_eq!(control_command(&arm, 1).unwrap().unwrap()["type"], "arm");

        let mut position = vec![0_u8; 53];
        position[50] = 1;
        position[51] = 1;
        position[52] = 1;
        position[48..50].copy_from_slice(&0x0df8_u16.to_le_bytes());
        position[4..8].copy_from_slice(&1.25_f32.to_le_bytes());
        position[8..12].copy_from_slice(&(-0.5_f32).to_le_bytes());
        position[12..16].copy_from_slice(&(-1.0_f32).to_le_bytes());
        let setpoint = FrameInfo {
            system_id: 255,
            component_id: 190,
            message_id: 84,
            sequence: 2,
            payload: position,
        };
        let value = control_command(&setpoint, 1).unwrap().unwrap();
        assert_eq!(value["type"], "set_position");
        assert_eq!(value["payload"]["north_m"], 1.25);
        assert_eq!(value["payload"]["east_m"], -0.5);
        assert_eq!(value["payload"]["down_m"], -1.0);

        let mut set_mode = vec![0_u8; 6];
        set_mode[0..4].copy_from_slice(&4_u32.to_le_bytes());
        set_mode[4] = 1;
        set_mode[5] = 1;
        let mode = FrameInfo {
            system_id: 255,
            component_id: 190,
            message_id: 11,
            sequence: 3,
            payload: set_mode,
        };
        assert_eq!(
            control_command(&mode, 1).unwrap().unwrap()["payload"]["mode"],
            "RTL"
        );
    }

    #[test]
    fn position_target_rejects_reserved_mask_bits_and_non_finite_coordinates() {
        let mut position = vec![0_u8; 53];
        position[50] = 1;
        position[51] = 1;
        position[52] = 1;
        position[48..50].copy_from_slice(&0x1df8_u16.to_le_bytes());
        let frame = |payload| FrameInfo {
            system_id: 255,
            component_id: 190,
            message_id: 84,
            sequence: 1,
            payload,
        };
        assert!(control_command(&frame(position.clone()), 1)
            .unwrap_err()
            .to_string()
            .contains("position-only LOCAL_NED"));

        position[48..50].copy_from_slice(&0x0df8_u16.to_le_bytes());
        position[4..8].copy_from_slice(&f32::NAN.to_le_bytes());
        assert!(control_command(&frame(position.clone()), 1)
            .unwrap_err()
            .to_string()
            .contains("must be finite"));

        position[4..8].copy_from_slice(&f32::INFINITY.to_le_bytes());
        assert!(control_command(&frame(position), 1)
            .unwrap_err()
            .to_string()
            .contains("must be finite"));
    }

    #[test]
    fn rtl_is_vehicle_bound_and_has_no_caller_supplied_parameters() {
        let mut payload = vec![0_u8; 32];
        payload[28..30].copy_from_slice(&20_u16.to_le_bytes());
        payload[30] = 1;
        payload[31] = 1;
        let frame = |payload| FrameInfo {
            system_id: 255,
            component_id: 190,
            message_id: 76,
            sequence: 1,
            payload,
        };

        let rtl = control_command(&frame(payload.clone()), 1)
            .unwrap()
            .unwrap();
        assert_eq!(rtl, serde_json::json!({"type":"rtl"}));

        payload[0..4].copy_from_slice(&2.0_f32.to_le_bytes());
        assert!(control_command(&frame(payload.clone()), 1)
            .unwrap_err()
            .to_string()
            .contains("caller-supplied parameters"));

        payload[0..4].copy_from_slice(&0.0_f32.to_le_bytes());
        payload[30] = 2;
        assert!(control_command(&frame(payload), 1)
            .unwrap_err()
            .to_string()
            .contains("target mismatch"));
    }

    #[test]
    fn do_set_mode_requires_supported_custom_mode_flags_and_parameters() {
        let mut payload = vec![0_u8; 32];
        payload[0..4].copy_from_slice(&1.0_f32.to_le_bytes());
        payload[4..8].copy_from_slice(&4.0_f32.to_le_bytes());
        payload[28..30].copy_from_slice(&176_u16.to_le_bytes());
        payload[30] = 1;
        payload[31] = 1;
        let frame = |payload| FrameInfo {
            system_id: 255,
            component_id: 190,
            message_id: 76,
            sequence: 1,
            payload,
        };
        assert_eq!(
            control_command(&frame(payload.clone()), 1)
                .unwrap()
                .unwrap(),
            serde_json::json!({"type":"set_mode","payload":{"mode":"RTL"}})
        );

        payload[0..4].copy_from_slice(&0.0_f32.to_le_bytes());
        assert!(control_command(&frame(payload.clone()), 1)
            .unwrap_err()
            .to_string()
            .contains("custom mode enabled"));

        payload[0..4].copy_from_slice(&1.0_f32.to_le_bytes());
        payload[4..8].copy_from_slice(&4.5_f32.to_le_bytes());
        assert!(control_command(&frame(payload.clone()), 1)
            .unwrap_err()
            .to_string()
            .contains("unsupported custom mode"));

        payload[4..8].copy_from_slice(&4.0_f32.to_le_bytes());
        payload[8..12].copy_from_slice(&1.0_f32.to_le_bytes());
        assert!(control_command(&frame(payload), 1)
            .unwrap_err()
            .to_string()
            .contains("submode and extra parameters"));
    }

    fn checksum(bytes: &[u8]) -> u16 {
        bytes.iter().fold(0xffff_u16, |crc, byte| {
            let mut tmp = *byte ^ (crc as u8);
            tmp ^= tmp.wrapping_shl(4);
            (crc >> 8) ^ (u16::from(tmp) << 8) ^ (u16::from(tmp) << 3) ^ (u16::from(tmp) >> 4)
        })
    }

    fn v1_frame(
        message_id: u8,
        sequence: u8,
        system_id: u8,
        payload: &[u8],
        crc_extra: u8,
    ) -> Vec<u8> {
        let mut body = vec![payload.len() as u8, sequence, system_id, 1, message_id];
        body.extend_from_slice(payload);
        let crc = checksum(&[body.as_slice(), &[crc_extra]].concat());
        let mut frame = vec![0xfe];
        frame.extend_from_slice(&body);
        frame.extend_from_slice(&crc.to_le_bytes());
        frame
    }

    fn parsed(frame: &[u8]) -> FrameInfo {
        validate_frame(frame).expect("test frame should parse")
    }

    fn message(message_id: u32, payload: Vec<u8>) -> FrameInfo {
        FrameInfo {
            sequence: 1,
            system_id: 255,
            component_id: 190,
            message_id,
            payload,
        }
    }

    #[test]
    fn request_allowlist_accepts_heartbeat_and_targeted_stream_requests() {
        let heartbeat = parsed(&v1_frame(0, 1, 255, &[0, 0, 0, 0, 2, 3, 0, 3, 3], 50));
        assert!(read_only_request(&heartbeat, 1));

        // REQUEST_DATA_STREAM target system/component, stream id, rate, start/stop.
        let stream_request = parsed(&v1_frame(66, 2, 255, &[1, 1, 0, 5, 0, 1], 148));
        assert!(read_only_request(&stream_request, 1));
        let other_vehicle_request = message(66, vec![2, 1, 0, 5, 0, 1]);
        assert!(!read_only_request(&other_vehicle_request, 1));
    }

    #[test]
    fn command_long_allowlist_accepts_only_request_message_for_this_vehicle() {
        let mut payload = vec![0_u8; 33];
        payload[28..30].copy_from_slice(&512_u16.to_le_bytes());
        payload[30] = 1;
        let allowed = parsed(&v1_frame(76, 3, 255, &payload, 152));
        assert!(read_only_request(&allowed, 1));

        payload[28..30].copy_from_slice(&400_u16.to_le_bytes());
        let arm_disarm = parsed(&v1_frame(76, 4, 255, &payload, 152));
        assert!(!read_only_request(&arm_disarm, 1));

        payload[28..30].copy_from_slice(&512_u16.to_le_bytes());
        payload[30] = 2;
        let wrong_target = parsed(&v1_frame(76, 5, 255, &payload, 152));
        assert!(!read_only_request(&wrong_target, 1));
    }

    #[test]
    fn mission_and_setpoint_messages_are_not_read_only_requests() {
        let mission_item = message(39, vec![0; 37]);
        assert!(!read_only_request(&mission_item, 1));

        let setpoint = message(84, vec![0; 53]);
        assert!(!read_only_request(&setpoint, 1));
    }

    #[test]
    fn checksum_corruption_is_rejected_before_forwarding() {
        let mut heartbeat = v1_frame(0, 8, 1, &[0, 0, 0, 0, 2, 3, 0, 3, 3], 50);
        let final_byte = heartbeat.len() - 1;
        heartbeat[final_byte] ^= 0x80;
        assert!(validate_frame(&heartbeat).is_err());
    }
}
