use std::{
    net::{SocketAddr, TcpListener, UdpSocket as StdUdpSocket},
    process::{Child, Command, Stdio},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use anyhow::{Context, Result};
use async_nats::jetstream::{self, stream};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine};
use futures_util::StreamExt;
use mavlink::MavHeader;
use tokio::net::UdpSocket;
use tokio::time::timeout;

const STREAM: &str = "ANTOINE_MAVLINK";
static NATS_INTEGRATION_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

#[test]
fn gazebo_mode_requires_run_identity() {
    let output = Command::new(env!("CARGO_BIN_EXE_antoine-mavlink-jetstream-bridge"))
        .args(["--mode", "gazebo-nats"])
        .output()
        .expect("run bridge configuration check");
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("--run-id is required"));
}

struct ChildGuard(Child);

impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn free_udp_addr() -> Result<SocketAddr> {
    Ok(StdUdpSocket::bind("127.0.0.1:0")?.local_addr()?)
}

async fn wait_for_udp_bind(address: SocketAddr) -> Result<()> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    loop {
        match StdUdpSocket::bind(address) {
            Ok(socket) => drop(socket),
            Err(error) if error.kind() == std::io::ErrorKind::AddrInUse => return Ok(()),
            Err(error) => return Err(error.into()),
        }
        if tokio::time::Instant::now() >= deadline {
            anyhow::bail!("bridge did not bind QGC UDP {address}");
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

fn crc_x25(bytes: &[u8]) -> u16 {
    bytes.iter().fold(0xffff_u16, |crc, byte| {
        let mut tmp = *byte ^ (crc as u8);
        tmp ^= tmp.wrapping_shl(4);
        (crc >> 8) ^ (u16::from(tmp) << 8) ^ (u16::from(tmp) << 3) ^ (u16::from(tmp) >> 4)
    })
}

fn v1_frame(message_id: u8, sequence: u8, system_id: u8, payload: &[u8], crc_extra: u8) -> Vec<u8> {
    let mut body = vec![payload.len() as u8, sequence, system_id, 1, message_id];
    body.extend_from_slice(payload);
    let crc = crc_x25(&[body.as_slice(), &[crc_extra]].concat());
    let mut frame = vec![0xfe];
    frame.extend_from_slice(&body);
    frame.extend_from_slice(&crc.to_le_bytes());
    frame
}

fn v2_frame(
    message_id: u32,
    sequence: u8,
    system_id: u8,
    payload: &[u8],
    crc_extra: u8,
) -> Vec<u8> {
    v2_frame_with_component(message_id, sequence, system_id, 1, payload, crc_extra)
}

fn v2_frame_with_component(
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
    let crc = crc_x25(&[body.as_slice(), &[crc_extra]].concat());
    let mut frame = vec![0xfd];
    frame.extend_from_slice(&body);
    frame.extend_from_slice(&crc.to_le_bytes());
    frame
}

#[test]
fn bridge_rejects_non_loopback_bind_and_nats_endpoints() -> Result<()> {
    let binary = env!("CARGO_BIN_EXE_antoine-mavlink-jetstream-bridge");
    let non_loopback_bind = Command::new(binary)
        .args(["--vehicle-bind", "0.0.0.0:14560"])
        .output()?;
    assert!(!non_loopback_bind.status.success());
    assert!(
        String::from_utf8_lossy(&non_loopback_bind.stderr).contains("must use a loopback address")
    );

    let remote_nats = Command::new(binary)
        .args(["--nats-url", "nats://example.com:4222"])
        .output()?;
    assert!(!remote_nats.status.success());
    assert!(String::from_utf8_lossy(&remote_nats.stderr)
        .contains("must use localhost or a loopback IP"));
    Ok(())
}

async fn wait_for_stream_and_consumer(
    js: &jetstream::Context,
) -> Result<jetstream::stream::Stream> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    loop {
        if let Ok(mut stream) = js.get_stream(STREAM).await {
            if let Ok(info) = stream.info().await {
                if info.state.consumer_count > 0 {
                    return Ok(stream);
                }
            }
        }
        if tokio::time::Instant::now() >= deadline {
            anyhow::bail!("bridge did not create its JetStream stream and consumer");
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

async fn wait_for_jetstream(js: &jetstream::Context) -> Result<()> {
    js.create_stream(stream::Config {
        name: "dvd_jetstream_ready".into(),
        subjects: vec!["dvd_jetstream_ready".into()],
        ..Default::default()
    })
    .await?;
    Ok(())
}

async fn wait_for_raw_subject(
    stream: &jetstream::stream::Stream,
    subject: &str,
) -> Result<jetstream::message::StreamMessage> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
    loop {
        if let Ok(message) = stream.get_last_raw_message_by_subject(subject).await {
            return Ok(message);
        }
        if tokio::time::Instant::now() >= deadline {
            anyhow::bail!("no JetStream record appeared for {subject}");
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

#[tokio::test]
async fn local_bridge_routes_qgc_commands_bidirectionally_and_gates_nats_egress() -> Result<()> {
    let _serial = NATS_INTEGRATION_LOCK.lock().await;
    let nats_listener = TcpListener::bind("127.0.0.1:0")?;
    let nats_port = nats_listener.local_addr()?.port();
    drop(nats_listener);
    let nats_url = format!("nats://127.0.0.1:{nats_port}");
    let store = tempfile::tempdir()?;
    let nats_child = Command::new("nats-server")
        .args([
            "-js",
            "-a",
            "127.0.0.1",
            "-p",
            &nats_port.to_string(),
            "-sd",
        ])
        .arg(store.path())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("nats-server must be installed to run the loopback integration test")?;
    let mut _nats_guard = ChildGuard(nats_child);

    let client = timeout(Duration::from_secs(5), async {
        loop {
            if let Ok(client) = async_nats::connect(&nats_url).await {
                break client;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    })
    .await
    .context("local JetStream server did not start")?;
    let js = jetstream::new(client.clone());

    let vehicle = UdpSocket::bind("127.0.0.1:0").await?;
    let vehicle_peer = vehicle.local_addr()?;
    let gcs = UdpSocket::bind("127.0.0.1:0").await?;
    let qgc_peer = gcs.local_addr()?;
    let vehicle_bind = free_udp_addr()?;
    let qgc_bind = free_udp_addr()?;
    let callsign = "integration-sitl";

    let bridge_child = Command::new(env!("CARGO_BIN_EXE_antoine-mavlink-jetstream-bridge"))
        .args([
            "--run-id",
            "bridge-run-1",
            "--nats-url",
            &nats_url,
            "--callsign",
            callsign,
            "--vehicle-bind",
            &vehicle_bind.to_string(),
            "--vehicle-peer",
            &vehicle_peer.to_string(),
            "--qgc-bind",
            &qgc_bind.to_string(),
            "--qgc-peer",
            &qgc_peer.to_string(),
            "--system-id",
            "1",
        ])
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()?;
    let _bridge_guard = ChildGuard(bridge_child);
    let stream = wait_for_stream_and_consumer(&js).await?;

    let telemetry = v1_frame(0, 1, 1, &[0, 0, 0, 0, 2, 3, 0, 3, 3], 50);
    vehicle.send_to(&telemetry, vehicle_bind).await?;
    let mut buf = [0_u8; 512];
    let (len, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buf)).await??;
    assert_eq!(&buf[..len], telemetry.as_slice());
    let stored =
        wait_for_raw_subject(&stream, &format!("furia.mavlink.telemetry.{callsign}")).await?;
    let telemetry_event: serde_json::Value = serde_json::from_slice(&stored.payload)?;
    assert_eq!(telemetry_event["direction"], "vehicle_to_gcs");
    assert_eq!(telemetry_event["message_id"], 0);

    let mut mission_item_payload = vec![0_u8; 37];
    mission_item_payload[32] = 1;
    mission_item_payload[33] = 1;
    mission_item_payload[34] = 0;
    mission_item_payload[30] = 16; // MAV_FRAME_GLOBAL_RELATIVE_ALT
    let proposal = v1_frame(39, 2, 255, &mission_item_payload, 254);
    assert!(
        mavlink::read_v1_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&proposal))
        )
        .is_ok(),
        "test mission item must be valid MAVLink"
    );
    gcs.send_to(&proposal, qgc_bind).await?;
    let (len, _) = timeout(Duration::from_secs(2), vehicle.recv_from(&mut buf))
        .await
        .context("QGC mission item did not reach SITL")??;
    assert_eq!(&buf[..len], proposal.as_slice());
    let stored =
        wait_for_raw_subject(&stream, &format!("furia.mavlink.command.{callsign}")).await?;
    let proposal_event: serde_json::Value = serde_json::from_slice(&stored.payload)?;
    assert_eq!(proposal_event["direction"], "gcs_to_vehicle");
    assert_eq!(
        proposal_event["raw_base64"],
        BASE64.encode(proposal.as_slice())
    );

    let request_data_stream = v1_frame(66, 3, 255, &[1, 1, 0, 5, 0, 1], 148);
    let ack = js
        .publish(
            format!("furia.mavlink.request.{callsign}"),
            serde_json::json!({
                "runId": "stale-run",
                "raw_base64": BASE64.encode(request_data_stream.as_slice())
            })
            .to_string()
            .into(),
        )
        .await?;
    ack.await?;
    assert!(
        timeout(Duration::from_millis(200), vehicle.recv_from(&mut buf))
            .await
            .is_err(),
        "JetStream requests from another run must not reach the vehicle"
    );
    let ack = js
        .publish(
            format!("furia.mavlink.request.{callsign}"),
            serde_json::json!({
                "runId": "bridge-run-1",
                "raw_base64": BASE64.encode(request_data_stream.as_slice())
            })
            .to_string()
            .into(),
        )
        .await?;
    ack.await?;
    let (len, _) = timeout(Duration::from_secs(2), vehicle.recv_from(&mut buf))
        .await
        .context("NATS telemetry request did not reach SITL")??;
    assert_eq!(&buf[..len], request_data_stream.as_slice());

    let mut arm_payload = vec![0_u8; 33];
    arm_payload[28..30].copy_from_slice(&400_u16.to_le_bytes());
    arm_payload[30] = 1;
    let arm_command = v1_frame(76, 4, 255, &arm_payload, 152);
    let ack = js
        .publish(
            format!("furia.mavlink.request.{callsign}"),
            serde_json::json!({
                "runId": "bridge-run-1",
                "raw_base64": BASE64.encode(arm_command.as_slice())
            })
            .to_string()
            .into(),
        )
        .await?;
    ack.await?;
    assert!(
        timeout(Duration::from_millis(200), vehicle.recv_from(&mut buf))
            .await
            .is_err()
    );

    let arm_from_qgc = v1_frame(76, 5, 255, &arm_payload, 152);
    gcs.send_to(&arm_from_qgc, qgc_bind).await?;
    let (len, _) = timeout(Duration::from_secs(2), vehicle.recv_from(&mut buf))
        .await
        .context("QGC arm command did not reach SITL")??;
    assert_eq!(&buf[..len], arm_from_qgc.as_slice());

    let setpoint = v1_frame(84, 6, 255, &[0; 53], 143);
    gcs.send_to(&setpoint, qgc_bind).await?;
    let (len, _) = timeout(Duration::from_secs(2), vehicle.recv_from(&mut buf))
        .await
        .context("QGC setpoint did not reach SITL")??;
    assert_eq!(&buf[..len], setpoint.as_slice());

    let vehicle_spoof = v1_frame(0, 7, 1, &[0, 0, 0, 0, 2, 3, 0, 3, 3], 50);
    gcs.send_to(&vehicle_spoof, qgc_bind).await?;
    assert!(
        timeout(Duration::from_millis(150), vehicle.recv_from(&mut buf))
            .await
            .is_err()
    );

    let mut invalid_qgc = arm_from_qgc.clone();
    *invalid_qgc
        .last_mut()
        .context("arm frame has checksum bytes")? ^= 0x80;
    gcs.send_to(&invalid_qgc, qgc_bind).await?;
    assert!(
        timeout(Duration::from_millis(150), vehicle.recv_from(&mut buf))
            .await
            .is_err()
    );

    let command_ack = v1_frame(77, 7, 1, &[144, 1, 0], 143);
    assert!(
        mavlink::read_v1_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&command_ack))
        )
        .is_ok(),
        "test command ACK must be valid MAVLink"
    );
    vehicle.send_to(&command_ack, vehicle_bind).await?;
    let (len, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buf))
        .await
        .context("SITL command ACK did not return to QGC")??;
    assert_eq!(&buf[..len], command_ack.as_slice());

    let mut corrupted = telemetry.clone();
    *corrupted
        .last_mut()
        .context("heartbeat has checksum bytes")? ^= 0x80;
    vehicle.send_to(&corrupted, vehicle_bind).await?;
    assert!(timeout(Duration::from_millis(150), gcs.recv_from(&mut buf))
        .await
        .is_err());

    let mut stream = js.get_stream(STREAM).await?;
    let info = stream.info().await?;
    assert_eq!(
        info.state.messages, 9,
        "invalid MAVLink CRC must not be persisted"
    );
    assert_eq!(info.state.subjects_count, 4);
    let heartbeat_subject = "furia.mavlink.heartbeat.integration-sitl";
    let heartbeat = stream
        .get_last_raw_message_by_subject(heartbeat_subject)
        .await?;
    let heartbeat: serde_json::Value = serde_json::from_slice(&heartbeat.payload)?;
    assert_eq!(heartbeat["direction"], "vehicle_to_gcs");
    assert_eq!(heartbeat["message_id"], 0);

    let second_vehicle = UdpSocket::bind("127.0.0.1:0").await?;
    let second_vehicle_peer = second_vehicle.local_addr()?;
    let second_gcs = UdpSocket::bind("127.0.0.1:0").await?;
    let second_vehicle_bind = free_udp_addr()?;
    let second_qgc_bind = free_udp_addr()?;
    let second_callsign = "integration-sitl-2";
    let second_bridge = Command::new(env!("CARGO_BIN_EXE_antoine-mavlink-jetstream-bridge"))
        .args([
            "--run-id",
            "bridge-run-2",
            "--nats-url",
            &nats_url,
            "--callsign",
            second_callsign,
            "--vehicle-bind",
            &second_vehicle_bind.to_string(),
            "--vehicle-peer",
            &second_vehicle_peer.to_string(),
            "--qgc-bind",
            &second_qgc_bind.to_string(),
            "--qgc-peer",
            &second_gcs.local_addr()?.to_string(),
            "--system-id",
            "2",
        ])
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()?;
    let _second_bridge_guard = ChildGuard(second_bridge);
    timeout(Duration::from_secs(5), async {
        loop {
            if stream.info().await?.state.consumer_count >= 2 {
                return Ok::<(), anyhow::Error>(());
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    })
    .await
    .context("second vehicle bridge did not create its callsign-scoped consumer")??;
    let second_request = v1_frame(66, 1, 255, &[2, 1, 0, 5, 0, 1], 148);
    let ack = js
        .publish(
            format!("furia.mavlink.request.{second_callsign}"),
            serde_json::json!({
                "runId": "bridge-run-2",
                "raw_base64": BASE64.encode(second_request.as_slice())
            })
            .to_string()
            .into(),
        )
        .await?;
    ack.await?;
    let (len, _) = timeout(Duration::from_secs(2), second_vehicle.recv_from(&mut buf))
        .await
        .context("second vehicle request did not reach its own SITL")??;
    assert_eq!(&buf[..len], second_request.as_slice());
    assert!(
        timeout(Duration::from_millis(150), vehicle.recv_from(&mut buf))
            .await
            .is_err(),
        "second vehicle request must not reach the first SITL"
    );
    Ok(())
}

#[tokio::test]
async fn antoine_nats_mode_routes_arm_commands_and_telemetry_to_qgc() -> Result<()> {
    let _serial = NATS_INTEGRATION_LOCK.lock().await;
    let nats_listener = TcpListener::bind("127.0.0.1:0")?;
    let nats_port = nats_listener.local_addr()?.port();
    drop(nats_listener);
    let nats_url = format!("nats://127.0.0.1:{nats_port}");
    let store = tempfile::tempdir()?;
    let nats_child = Command::new("nats-server")
        .args([
            "-js",
            "-a",
            "127.0.0.1",
            "-p",
            &nats_port.to_string(),
            "-sd",
        ])
        .arg(store.path())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("nats-server must be installed to run the Antoine adapter test")?;
    let mut _nats_guard = ChildGuard(nats_child);
    let client = timeout(Duration::from_secs(5), async {
        loop {
            if let Ok(client) = async_nats::connect(&nats_url).await {
                break client;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    })
    .await
    .context("local JetStream server did not start")?;
    let js = jetstream::new(client.clone());
    wait_for_jetstream(&js).await?;
    // Simulate a deployment created before the QGC command-audit subject was
    // added. Bridge startup must extend this stream instead of leaving the
    // command subject unroutable.
    js.create_stream(stream::Config {
        name: "ANTOINE_MAVLINK".into(),
        subjects: vec![
            "furia.mavlink.telemetry.>".into(),
            "furia.mavlink.request.>".into(),
            "furia.mavlink.command.>".into(),
        ],
        storage: stream::StorageType::File,
        retention: stream::RetentionPolicy::Limits,
        max_age: Duration::from_secs(24 * 60 * 60),
        max_messages: 100_000,
        ..Default::default()
    })
    .await?;
    let gcs = UdpSocket::bind("127.0.0.1:0").await?;
    let qgc_peer = gcs.local_addr()?;
    let probe = UdpSocket::bind("127.0.0.1:0").await?;
    let control_probe = UdpSocket::bind("127.0.0.1:0").await?;
    let control_probe_addr = control_probe.local_addr()?.to_string();
    let qgc_bind = free_udp_addr()?;
    let callsign = "antoine-nats-test";
    let mut audit_subscription = client
        .subscribe(format!("furia.mavlink.command.{callsign}"))
        .await?;
    let bridge = Command::new(env!("CARGO_BIN_EXE_antoine-mavlink-jetstream-bridge"))
        .args([
            "--mode",
            "antoine-nats",
            "--nats-url",
            &nats_url,
            "--callsign",
            callsign,
            "--qgc-bind",
            &qgc_bind.to_string(),
            "--qgc-peer",
            &qgc_peer.to_string(),
            "--qgc-control-peer",
            &control_probe_addr,
            "--qgc-telemetry-peer",
            &control_probe_addr,
            "--system-id",
            "1",
        ])
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()?;
    let mut bridge_guard = ChildGuard(bridge);
    let controls = client
        .subscribe(format!("furia.mavlink.control.{callsign}"))
        .await?;
    client.flush().await?;
    let (reply_tx, mut reply_rx) = tokio::sync::mpsc::unbounded_channel();
    let reply_client = client.clone();
    let mission_upload_count = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let upload_count = mission_upload_count.clone();
    tokio::spawn(async move {
        let mut controls = controls;
        let mut uploaded_waypoints: Option<serde_json::Value> = None;
        while let Some(message) = controls.next().await {
            if let Some(reply) = message.reply {
                let value: serde_json::Value = serde_json::from_slice(&message.payload).unwrap();
                let _ = reply_tx.send(value.clone());
                let request_id = value["request_id"].clone();
                let command_type = value["command"]["type"].as_str().unwrap_or_default();
                let mission_query =
                    command_type == "mission_query" || value["type"] == "mission_query";
                let reject_mission_query = request_id
                    .as_str()
                    .is_some_and(|id| id.contains("-mission-query-14-"));
                let omit_mission_state = request_id
                    .as_str()
                    .is_some_and(|id| id.contains("-mission-query-15-"));
                let empty_uploaded_mission = request_id
                    .as_str()
                    .is_some_and(|id| id.contains("-mission-query-16-"));
                let contradictory_mission_state = request_id
                    .as_str()
                    .is_some_and(|id| id.contains("-mission-query-17-"));
                let reject_mission_upload = command_type == "mission_upload"
                    && upload_count.fetch_add(1, std::sync::atomic::Ordering::Relaxed) > 0;
                if command_type == "mission_clear" {
                    uploaded_waypoints = None;
                } else if command_type == "mission_upload" && !reject_mission_upload {
                    uploaded_waypoints = Some(value["command"]["payload"]["waypoints"].clone());
                }
                let response = if reject_mission_query {
                    serde_json::json!({"requestId":request_id,"accepted":false,"armed":true,"mode":"GUIDED"})
                } else if empty_uploaded_mission {
                    serde_json::json!({"requestId":request_id,"accepted":true,"armed":true,"mode":"GUIDED","missionUploaded":true,"waypoints":[]})
                } else if contradictory_mission_state {
                    serde_json::json!({"requestId":request_id,"accepted":true,"armed":true,"mode":"GUIDED","missionUploaded":false,"waypoints":[[0.0,0.0,-1.0]]})
                } else if omit_mission_state {
                    serde_json::json!({"requestId":request_id,"accepted":true,"armed":true,"mode":"GUIDED"})
                } else if reject_mission_upload {
                    serde_json::json!({"requestId":request_id,"accepted":false,"armed":true,"mode":"GUIDED"})
                } else if mission_query {
                    match &uploaded_waypoints {
                        Some(waypoints) => {
                            serde_json::json!({"requestId":request_id,"accepted":true,"armed":true,"mode":"GUIDED","missionUploaded":true,"waypoints":waypoints})
                        }
                        None => {
                            serde_json::json!({"requestId":request_id,"accepted":true,"armed":true,"mode":"GUIDED","missionUploaded":false,"waypoints":[]})
                        }
                    }
                } else {
                    serde_json::json!({"requestId":request_id,"accepted":true,"armed":true,"mode":"GUIDED","missionUploaded":false,"waypoints":[]})
                };
                let mut response = response;
                response["schema"] = serde_json::json!("furia.mavlink.control.result");
                response["version"] = serde_json::json!("1.0.0");
                let _ = reply_client
                    .publish(reply, response.to_string().into())
                    .await;
            }
        }
    });
    let (audit_tx, mut audit_rx) = tokio::sync::mpsc::unbounded_channel();
    tokio::spawn(async move {
        while let Some(message) = audit_subscription.next().await {
            let _ = audit_tx.send(message.payload.to_vec());
        }
    });
    client.flush().await?;
    wait_for_udp_bind(qgc_bind).await?;
    if let Some(status) = bridge_guard.0.try_wait()? {
        anyhow::bail!("Antoine adapter exited before receiving QGC traffic: {status}");
    }

    for (sequence, north, east) in [(8, 2.2_f32, 2.2_f32), (9, 3.1, 0.0)] {
        let mut out_of_range_setpoint_payload = vec![0_u8; 53];
        out_of_range_setpoint_payload[4..8].copy_from_slice(&north.to_le_bytes());
        out_of_range_setpoint_payload[8..12].copy_from_slice(&east.to_le_bytes());
        out_of_range_setpoint_payload[48..50].copy_from_slice(&0x0df8_u16.to_le_bytes());
        out_of_range_setpoint_payload[50] = 1;
        out_of_range_setpoint_payload[51] = 1;
        out_of_range_setpoint_payload[52] = 1;
        let out_of_range_setpoint =
            v2_frame(84, sequence, 255, &out_of_range_setpoint_payload, 143);
        gcs.send_to(&out_of_range_setpoint, qgc_bind).await?;
        assert!(
            timeout(Duration::from_millis(150), reply_rx.recv())
                .await
                .is_err(),
            "out-of-range position targets must not reach Antoine"
        );
        let setpoint_audit: serde_json::Value = serde_json::from_slice(
            &timeout(Duration::from_secs(2), audit_rx.recv())
                .await?
                .context("missing rejected position-target audit")?,
        )?;
        assert_eq!(setpoint_audit["direction"], "gcs_to_antoine");
        assert_eq!(setpoint_audit["message_id"], 84);
    }

    let mut arm_payload = vec![0_u8; 33];
    arm_payload[0..4].copy_from_slice(&1.0_f32.to_le_bytes());
    arm_payload[28..30].copy_from_slice(&400_u16.to_le_bytes());
    arm_payload[30] = 1;
    let arm = v2_frame(76, 10, 255, &arm_payload[..32], 152);
    assert!(
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&arm))
        )
        .is_ok(),
        "test COMMAND_LONG must be valid MAVLink"
    );
    gcs.send_to(&arm, qgc_bind).await?;
    let command = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("arm NATS request timeout")?
        .context("Antoine command subscription ended")?;
    assert_eq!(command["command"]["type"], "arm");
    let mut buffer = [0_u8; 512];
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("ARM ACK timeout")??;
    assert_eq!(buffer[0], 0xfd);
    assert_eq!(buffer[7], 77, "QGC should receive COMMAND_ACK");
    // MAVLink 2 trims trailing zero payload bytes (including an accepted
    // COMMAND_ACK result byte), so the valid encoded frame can be 14 bytes.
    assert!(length >= 14);
    let audit: serde_json::Value = serde_json::from_slice(
        &timeout(Duration::from_secs(2), audit_rx.recv())
            .await?
            .context("missing arm audit")?,
    )?;
    assert_eq!(audit["direction"], "gcs_to_antoine");
    assert_eq!(audit["message_id"], 76);

    // RTL has a vehicle-bound, targetless native representation. It cannot
    // carry caller-supplied parameters that could be mistaken for a return
    // target override.
    let mut rtl_payload = [0_u8; 33];
    rtl_payload[28..30].copy_from_slice(&20_u16.to_le_bytes());
    rtl_payload[30] = 1;
    rtl_payload[31] = 1;
    let rtl = v2_frame(76, 12, 255, &rtl_payload[..32], 152);
    gcs.send_to(&rtl, qgc_bind).await?;
    let command = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("RTL NATS request timeout")?
        .context("Antoine command subscription ended")?;
    assert_eq!(command["command"], serde_json::json!({"type":"rtl"}));
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("RTL ACK timeout")??;
    assert_eq!(buffer[7], 77, "QGC should receive RTL COMMAND_ACK");
    assert!(length >= 12, "truncated but valid MAVLink ACK frame");

    rtl_payload[0..4].copy_from_slice(&2.0_f32.to_le_bytes());
    let target_override = v2_frame(76, 13, 255, &rtl_payload[..32], 152);
    gcs.send_to(&target_override, qgc_bind).await?;
    assert!(
        timeout(Duration::from_millis(150), reply_rx.recv())
            .await
            .is_err(),
        "nonzero RTL parameters must not reach the NATS control subject"
    );
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("rejected RTL COMMAND_ACK timeout")??;
    let (_, response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::COMMAND_ACK(ack) = response else {
        anyhow::bail!("expected COMMAND_ACK for rejected RTL parameters");
    };
    assert_eq!(
        ack.command,
        mavlink::dialects::common::MavCmd::MAV_CMD_NAV_RETURN_TO_LAUNCH
    );
    assert_eq!(
        ack.result,
        mavlink::dialects::common::MavResult::MAV_RESULT_DENIED
    );

    rtl_payload[0..4].copy_from_slice(&0.0_f32.to_le_bytes());
    rtl_payload[30] = 2;
    let wrong_vehicle_rtl = v2_frame(76, 14, 255, &rtl_payload[..32], 152);
    gcs.send_to(&wrong_vehicle_rtl, qgc_bind).await?;
    assert!(
        timeout(Duration::from_millis(150), reply_rx.recv())
            .await
            .is_err(),
        "wrong-vehicle RTL must not reach the NATS control subject"
    );
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("wrong-vehicle RTL COMMAND_ACK timeout")??;
    let (_, response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::COMMAND_ACK(ack) = response else {
        anyhow::bail!("expected COMMAND_ACK for wrong-vehicle RTL");
    };
    assert_eq!(
        ack.command,
        mavlink::dialects::common::MavCmd::MAV_CMD_NAV_RETURN_TO_LAUNCH
    );
    assert_eq!(
        ack.result,
        mavlink::dialects::common::MavResult::MAV_RESULT_DENIED
    );

    // A different loopback client must not issue control commands or redirect telemetry.
    let probe_arm = v2_frame(76, 15, 255, &arm_payload[..32], 152);
    probe.send_to(&probe_arm, qgc_bind).await?;
    assert!(
        timeout(Duration::from_millis(150), reply_rx.recv())
            .await
            .is_err(),
        "an unconfigured loopback peer must not publish operator control"
    );
    assert!(
        timeout(Duration::from_millis(150), probe.recv_from(&mut buffer))
            .await
            .is_err(),
        "an unconfigured loopback peer must not receive a command ACK"
    );

    let probe_heartbeat = v2_frame(0, 11, 255, &[0, 0, 0, 0, 0, 2, 3], 50);
    probe.send_to(&probe_heartbeat, qgc_bind).await?;

    let native_telemetry = serde_json::json!({
        "schema":"furia.telemetry", "version":"1.0.0", "protocol":"antoine",
        "identity":callsign, "time_s":1.25, "lat_deg":48.0002, "lon_deg":0.3564,
        "alt_m":0.0, "vn_mps":0.1, "ve_mps":-0.1, "vd_mps":0.1,
        "actuator_thrust":0.0, "ground_contact":true, "gnss_degraded":false,
        "battery_pct":100, "battery_mv":12000
    });
    client
        .publish(
            format!("furia.telemetry.{callsign}"),
            native_telemetry.to_string().into(),
        )
        .await?;
    client.flush().await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("heartbeat timeout")??;
    assert_eq!(buffer[7], 0, "QGC should receive HEARTBEAT");
    let (observer_length, _) =
        timeout(Duration::from_secs(2), control_probe.recv_from(&mut buffer))
            .await
            .context("configured control peer did not receive observed HEARTBEAT")??;
    assert!(observer_length >= 21);
    assert_eq!(buffer[7], 0);
    assert!(
        timeout(Duration::from_millis(100), probe.recv_from(&mut buffer))
            .await
            .is_err(),
        "probe must not receive QGC telemetry"
    );

    let control_peer_heartbeat = v2_frame(0, 12, 255, &[0, 0, 0, 0, 0, 2, 3], 50);
    control_probe
        .send_to(&control_peer_heartbeat, qgc_bind)
        .await?;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    let mut control_peer_audited = false;
    while tokio::time::Instant::now() < deadline {
        let Ok(Some(raw)) = timeout(Duration::from_millis(100), audit_rx.recv()).await else {
            continue;
        };
        let record: serde_json::Value = serde_json::from_slice(&raw)?;
        if record["source"] == control_probe_addr {
            assert_eq!(record["direction"], "gcs_to_antoine");
            assert_eq!(record["message_id"], 0);
            control_peer_audited = true;
            break;
        }
    }
    assert!(
        control_peer_audited,
        "explicitly configured control peer frame must be audited"
    );
    let (_, heartbeat) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::HEARTBEAT(heartbeat) = heartbeat else {
        anyhow::bail!("expected HEARTBEAT message");
    };
    assert_eq!(
        heartbeat.autopilot,
        mavlink::dialects::common::MavAutopilot::MAV_AUTOPILOT_GENERIC,
        "Antoine must identify as a generic autopilot"
    );
    assert!(
        heartbeat
            .base_mode
            .contains(mavlink::dialects::common::MavModeFlag::MAV_MODE_FLAG_SAFETY_ARMED),
        "armed state should be reflected in HEARTBEAT"
    );
    assert_eq!(
        heartbeat.custom_mode, 1,
        "accepted mode should be reflected in HEARTBEAT"
    );
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("GLOBAL_POSITION_INT timeout")??;
    assert_eq!(buffer[7], 33, "QGC should receive GLOBAL_POSITION_INT");
    assert!(
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length]))
        )
        .is_ok()
    );

    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("GPS_RAW_INT timeout")??;
    assert_eq!(buffer[7], 24, "QGC should receive GPS_RAW_INT");
    assert!(
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length]))
        )
        .is_ok()
    );
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("HOME_POSITION timeout")??;
    assert_eq!(buffer[7], 242, "QGC should receive HOME_POSITION");
    assert!(
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length]))
        )
        .is_ok()
    );

    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("SYS_STATUS timeout")??;
    assert_eq!(buffer[7], 1, "QGC should receive SYS_STATUS readiness");
    let (_, status_message) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::SYS_STATUS(status) = status_message else {
        anyhow::bail!("expected SYS_STATUS message");
    };
    assert!(status
        .onboard_control_sensors_health
        .contains(mavlink::dialects::common::MavSysStatusSensor::MAV_SYS_STATUS_PREARM_CHECK));

    client
        .publish(
            format!("furia.telemetry.{callsign}"),
            native_telemetry.to_string().into(),
        )
        .await?;
    client.flush().await?;
    assert!(
        timeout(Duration::from_millis(250), gcs.recv_from(&mut buffer))
            .await
            .is_err(),
        "a duplicate native telemetry clock must not be projected as fresh MAVLink state"
    );

    let pending_upload_count = v2_frame(44, 25, 255, &[1, 0, 1, 1], 221);
    gcs.send_to(&pending_upload_count, qgc_bind).await?;
    let (_length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("pending upload's first mission-item request timeout")??;
    assert_eq!(buffer[7], 51, "bridge should start the pending upload");

    let mission_list_request = v2_frame(43, 11, 255, &[1, 1, 0], 132);
    gcs.send_to(&mission_list_request, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("pending upload rejection ACK timeout")??;
    assert_eq!(
        buffer[7], 47,
        "mission-list read should reject pending upload"
    );
    let (_, pending_upload_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(pending_upload_ack) =
        pending_upload_response
    else {
        anyhow::bail!("expected MISSION_ACK before mission-list response");
    };
    assert_eq!(
        pending_upload_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
    );

    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("MISSION_COUNT timeout")??;
    assert_eq!(buffer[7], 44, "mission list request should be handled");
    let (_, mission_response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::MISSION_COUNT(mission_count) = mission_response
    else {
        anyhow::bail!("expected MISSION_COUNT response");
    };
    assert_eq!(
        mission_count.count, 0,
        "mission count should initially be empty"
    );
    let _ = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("SITL mission query was not received")?;

    let unsupported_mission_list = v2_frame(43, 12, 255, &[1, 1, 1], 132);
    gcs.send_to(&unsupported_mission_list, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("unsupported mission-list ACK timeout")??;
    let (_, unsupported_list_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(unsupported_list_ack) =
        unsupported_list_response
    else {
        anyhow::bail!("expected MISSION_ACK for unsupported mission-list type");
    };
    assert_eq!(
        unsupported_list_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_UNSUPPORTED
    );
    assert!(
        timeout(Duration::from_millis(100), reply_rx.recv())
            .await
            .is_err(),
        "unsupported mission-list type must not query Antoine"
    );

    for (message_id, sequence, payload, crc_extra) in [
        (43, 18, vec![1], 132),
        (44, 19, vec![1, 0], 221),
        (51, 20, vec![0, 0], 196),
        (73, 21, vec![0; 30], 38),
    ] {
        let malformed = v2_frame(message_id, sequence, 255, &payload, crc_extra);
        gcs.send_to(&malformed, qgc_bind).await?;
        let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
            .await
            .with_context(|| {
                format!("truncated mission message {message_id} response timeout")
            })??;
        let (_, response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
        let mavlink::dialects::common::MavMessage::MISSION_ACK(ack) = response else {
            anyhow::bail!("expected MISSION_ACK for truncated mission message {message_id}");
        };
        assert_eq!(
            ack.mavtype,
            mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR,
            "truncated mission message {message_id} should fail explicitly"
        );
    }

    let unsupported_count_payload = [1, 0, 1, 1, 1];
    let unsupported_count = v2_frame(44, 13, 255, &unsupported_count_payload, 221);
    gcs.send_to(&unsupported_count, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("unsupported mission-count ACK timeout")??;
    let (_, unsupported_count_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(unsupported_count_ack) =
        unsupported_count_response
    else {
        anyhow::bail!("expected MISSION_ACK for unsupported mission-count type");
    };
    assert_eq!(
        unsupported_count_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_UNSUPPORTED
    );
    assert!(
        timeout(Duration::from_millis(100), reply_rx.recv())
            .await
            .is_err(),
        "unsupported mission-count type must not start an upload"
    );

    let unsupported_clear_all = v2_frame(45, 18, 255, &[1, 1, 1], 232);
    gcs.send_to(&unsupported_clear_all, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("unsupported mission-clear ACK timeout")??;
    let (_, unsupported_clear_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(unsupported_clear_ack) =
        unsupported_clear_response
    else {
        anyhow::bail!("expected MISSION_ACK for unsupported mission-clear type");
    };
    assert_eq!(
        unsupported_clear_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_UNSUPPORTED
    );
    assert!(
        timeout(Duration::from_millis(100), reply_rx.recv())
            .await
            .is_err(),
        "unsupported mission-clear type must not clear Antoine's mission"
    );

    let malformed_clear_all = v2_frame(45, 22, 255, &[1, 1, 0, 99], 232);
    gcs.send_to(&malformed_clear_all, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("malformed mission-clear ACK timeout")??;
    let (_, malformed_clear_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(malformed_clear_ack) =
        malformed_clear_response
    else {
        anyhow::bail!("expected MISSION_ACK for malformed mission-clear payload");
    };
    assert_eq!(
        malformed_clear_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
    );
    assert!(
        timeout(Duration::from_millis(100), reply_rx.recv())
            .await
            .is_err(),
        "malformed mission-clear payload must not reach Antoine"
    );

    let mut unsupported_item_payload = vec![0; 38];
    unsupported_item_payload[30] = 16;
    unsupported_item_payload[32] = 1;
    unsupported_item_payload[33] = 1;
    unsupported_item_payload[34] = 6;
    unsupported_item_payload[37] = 1;
    let unsupported_mission_item = v2_frame(73, 19, 255, &unsupported_item_payload, 38);
    gcs.send_to(&unsupported_mission_item, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("unsupported mission-item ACK timeout")??;
    let (_, unsupported_item_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(unsupported_item_ack) =
        unsupported_item_response
    else {
        anyhow::bail!("expected MISSION_ACK for unsupported mission-item type");
    };
    assert_eq!(
        unsupported_item_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_UNSUPPORTED
    );
    assert!(
        timeout(Duration::from_millis(100), reply_rx.recv())
            .await
            .is_err(),
        "unsupported mission item must not be published to Antoine"
    );

    for (sequence, payload_length) in [(20, 34), (21, 39)] {
        let mut malformed_item_payload = vec![0; payload_length];
        malformed_item_payload[30] = 16;
        malformed_item_payload[32] = 1;
        malformed_item_payload[33] = 1;
        if payload_length > 34 {
            malformed_item_payload[34] = 6;
        }
        let malformed_item = v2_frame(73, sequence, 255, &malformed_item_payload, 38);
        gcs.send_to(&malformed_item, qgc_bind).await?;
        let (_length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
            .await
            .context("malformed mission-item ACK timeout")??;
        assert_eq!(buffer[7], 47, "malformed mission item should be rejected");
        let (_, malformed_item_response) =
            mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
                &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
            )?;
        let mavlink::dialects::common::MavMessage::MISSION_ACK(malformed_item_ack) =
            malformed_item_response
        else {
            anyhow::bail!("expected MISSION_ACK for malformed mission item");
        };
        assert_eq!(
            malformed_item_ack.mavtype,
            mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
        );
    }
    assert!(
        timeout(Duration::from_millis(100), reply_rx.recv())
            .await
            .is_err(),
        "malformed mission items must not be published to Antoine"
    );

    let rejected_mission_list = v2_frame(43, 14, 255, &[1, 1, 0], 132);
    gcs.send_to(&rejected_mission_list, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("mission query failure response timeout")??;
    assert_eq!(
        buffer[7], 47,
        "failed query must not look like an empty mission"
    );
    let (_, failure_response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(failure_ack) = failure_response else {
        anyhow::bail!("expected MISSION_ACK for rejected mission query");
    };
    assert_eq!(
        failure_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
    );
    let _ = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("rejected SITL mission query was not received")?;
    let malformed_mission_list = v2_frame(43, 15, 255, &[1, 1, 0], 132);
    gcs.send_to(&malformed_mission_list, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("malformed mission query response timeout")??;
    assert_eq!(
        buffer[7], 47,
        "malformed query must not look like an empty mission"
    );
    let (_, malformed_response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(malformed_ack) = malformed_response
    else {
        anyhow::bail!("expected MISSION_ACK for malformed mission query response");
    };
    assert_eq!(
        malformed_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
    );
    let _ = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("malformed SITL mission query was not received")?;
    let contradictory_mission_list = v2_frame(43, 17, 255, &[1, 1, 0], 132);
    gcs.send_to(&contradictory_mission_list, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("contradictory mission query response timeout")??;
    let (_, contradictory_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(contradictory_ack) =
        contradictory_response
    else {
        anyhow::bail!("expected MISSION_ACK for contradictory mission state");
    };
    assert_eq!(
        contradictory_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
    );
    let _ = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("contradictory SITL mission query was not received")?;
    let empty_uploaded_mission_list = v2_frame(43, 16, 255, &[1, 1, 0], 132);
    gcs.send_to(&empty_uploaded_mission_list, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("empty uploaded mission response timeout")??;
    assert_eq!(buffer[7], 47, "empty uploaded mission must be rejected");
    let (_, empty_mission_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(empty_mission_ack) =
        empty_mission_response
    else {
        anyhow::bail!("expected MISSION_ACK for empty uploaded mission");
    };
    assert_eq!(
        empty_mission_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
    );
    let _ = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("empty uploaded mission query was not received")?;
    let mut mission_count_payload = vec![0; 9];
    mission_count_payload[0..2].copy_from_slice(&1_u16.to_le_bytes());
    mission_count_payload[2] = 1;
    mission_count_payload[3] = 1;
    let mission_count_request = v2_frame(44, 16, 255, &mission_count_payload, 221);
    gcs.send_to(&mission_count_request, qgc_bind).await?;
    let mut mission_request_seen = false;
    for _ in 0..4 {
        let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
            .await
            .context("mission request/list response timeout")??;
        if buffer[7] == 51 {
            mission_request_seen = true;
            break;
        }
        assert!(
            mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
                &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length]))
            )
            .is_ok()
        );
    }
    assert!(
        mission_request_seen,
        "bridge should request mission items in INT format"
    );
    let mut mission_item = Vec::new();
    mavlink::write_v2_msg(
        &mut mission_item,
        MavHeader {
            sequence: 15,
            system_id: 255,
            component_id: 190,
        },
        &mavlink::dialects::common::MavMessage::MISSION_ITEM_INT(
            mavlink::dialects::common::MISSION_ITEM_INT_DATA {
                target_system: 1,
                target_component: 1,
                seq: 0,
                frame: mavlink::dialects::common::MavFrame::MAV_FRAME_GLOBAL_RELATIVE_ALT,
                command: mavlink::dialects::common::MavCmd::MAV_CMD_NAV_WAYPOINT,
                current: 1,
                autocontinue: 1,
                param1: 0.0,
                param2: 0.0,
                param3: 0.0,
                param4: 0.0,
                x: 480_002_000,
                y: 3_564_000,
                z: 1.0,
            },
        ),
    )?;
    let matching_component_item =
        v2_frame(73, 15, 255, &mission_item[10..mission_item.len() - 2], 38);
    tokio::time::sleep(Duration::from_secs(10) + Duration::from_millis(100)).await;
    gcs.send_to(&mission_item, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("expired mission-upload ACK timeout")??;
    let (_, expired_upload_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(expired_upload_ack) =
        expired_upload_response
    else {
        anyhow::bail!("expected MISSION_ACK for expired mission upload");
    };
    assert_eq!(
        expired_upload_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
    );
    assert!(
        timeout(Duration::from_millis(100), reply_rx.recv())
            .await
            .is_err(),
        "expired mission upload must not reach Antoine"
    );

    let restarted_mission_count = v2_frame(44, 17, 255, &mission_count_payload, 221);
    gcs.send_to(&restarted_mission_count, qgc_bind).await?;
    let mut restarted_request_seen = false;
    for _ in 0..4 {
        let (_length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
            .await
            .context("restarted mission upload request timeout")??;
        if buffer[7] == 51 {
            restarted_request_seen = true;
            break;
        }
    }
    assert!(
        restarted_request_seen,
        "a fresh mission upload should start after expiry"
    );
    let mut wrong_component_payload = mission_item[10..mission_item.len() - 2].to_vec();
    wrong_component_payload.resize(38, 0);
    wrong_component_payload[37] = 1;
    let wrong_component_item =
        v2_frame_with_component(73, 15, 255, 190, &wrong_component_payload, 38);
    gcs.send_to(&wrong_component_item, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("wrong-component mission-item rejection timeout")??;
    assert_eq!(
        buffer[7], 47,
        "wrong GCS component must not complete upload"
    );
    let (_, wrong_component_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(wrong_component_ack) =
        wrong_component_response
    else {
        anyhow::bail!("expected MISSION_ACK for wrong GCS component");
    };
    assert_eq!(
        wrong_component_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_UNSUPPORTED
    );
    probe.send_to(&matching_component_item, qgc_bind).await?;
    assert!(
        timeout(Duration::from_millis(150), probe.recv_from(&mut buffer))
            .await
            .is_err(),
        "mission data from an unconfigured UDP source must be dropped without a response"
    );
    gcs.send_to(&matching_component_item, qgc_bind).await?;
    let mut mission_ack_seen = false;
    for _ in 0..4 {
        let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
            .await
            .context("mission ACK timeout")??;
        if buffer[7] == 47 {
            let (_, response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
                &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
            )?;
            let mavlink::dialects::common::MavMessage::MISSION_ACK(response) = response else {
                anyhow::bail!("expected MISSION_ACK");
            };
            assert_eq!(
                response.mavtype,
                mavlink::dialects::common::MavMissionResult::MAV_MISSION_ACCEPTED,
                "mission upload rejected by bridge"
            );
            mission_ack_seen = true;
            break;
        }
        assert!(
            mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
                &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length]))
            )
            .is_ok()
        );
    }
    assert!(
        mission_ack_seen,
        "mission upload should finish with MISSION_ACK"
    );
    let unsupported_item_request = v2_frame(51, 17, 255, &[0, 0, 1, 1, 1], 196);
    gcs.send_to(&unsupported_item_request, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("unsupported mission-item request response timeout")??;
    assert_eq!(buffer[7], 47, "unsupported mission type should be rejected");
    let (_, unsupported_item_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(unsupported_item_ack) =
        unsupported_item_response
    else {
        anyhow::bail!("expected MISSION_ACK for unsupported mission-item type");
    };
    assert_eq!(
        unsupported_item_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_UNSUPPORTED
    );
    let invalid_item_request = v2_frame(51, 16, 255, &[1, 0, 1, 1, 0], 196);
    gcs.send_to(&invalid_item_request, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("invalid mission-item request response timeout")??;
    assert_eq!(
        buffer[7], 47,
        "out-of-range item request should be rejected"
    );
    let (_, invalid_item_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(invalid_item_ack) =
        invalid_item_response
    else {
        anyhow::bail!("expected MISSION_ACK for out-of-range item request");
    };
    assert_eq!(
        invalid_item_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_INVALID_SEQUENCE
    );
    gcs.send_to(&matching_component_item, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("unsolicited mission-item response timeout")??;
    assert_eq!(
        buffer[7], 47,
        "item without active upload should be rejected"
    );
    let (_, unsolicited_response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(unsolicited_ack) = unsolicited_response
    else {
        anyhow::bail!("expected MISSION_ACK for unsolicited mission item");
    };
    assert_eq!(
        unsolicited_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_INVALID_SEQUENCE
    );
    let mut uploaded = None;
    while let Ok(Some(command)) = timeout(Duration::from_secs(2), reply_rx.recv()).await {
        if command["command"]["type"] == "mission_upload" {
            uploaded = Some(command);
            break;
        }
    }
    let uploaded = uploaded.context("mission upload request was not published")?;
    assert_eq!(uploaded["command"]["type"], "mission_upload");
    assert_eq!(uploaded["command"]["payload"]["waypoints"][0][0], 0.0);
    assert_eq!(uploaded["command"]["payload"]["waypoints"][0][1], 0.0);
    assert_eq!(uploaded["command"]["payload"]["waypoints"][0][2], -1.0);

    let mission_readback = v2_frame(43, 39, 255, &[1, 1, 0], 132);
    gcs.send_to(&mission_readback, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("uploaded mission readback count timeout")??;
    let (_, mission_count_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_COUNT(readback_count) =
        mission_count_response
    else {
        anyhow::bail!("expected MISSION_COUNT for uploaded mission readback");
    };
    assert_eq!(readback_count.count, 1);
    let mission_query_command = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("uploaded mission query was not sent to Antoine")?
        .context("Antoine mission-query subscription ended")?;
    assert!(
        mission_query_command["type"] == "mission_query"
            || mission_query_command["command"]["type"] == "mission_query"
    );

    let readback_item_request = v2_frame(51, 40, 255, &[0, 0, 1, 1, 0], 196);
    gcs.send_to(&readback_item_request, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("uploaded mission item readback timeout")??;
    let (_, mission_item_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ITEM_INT(readback_item) =
        mission_item_response
    else {
        anyhow::bail!("expected MISSION_ITEM_INT for uploaded mission readback");
    };
    assert_eq!(readback_item.seq, 0);
    assert_eq!(
        readback_item.frame,
        mavlink::dialects::common::MavFrame::MAV_FRAME_GLOBAL_RELATIVE_ALT
    );
    assert_eq!(readback_item.x, 480_002_000);
    assert_eq!(readback_item.y, 3_564_000);
    assert_eq!(readback_item.z, 1.0);

    let mut invalid_upload_id_payload = mission_count_payload.clone();
    invalid_upload_id_payload[5] = 1;
    let invalid_upload_id = v2_frame(44, 41, 255, &invalid_upload_id_payload, 221);
    gcs.send_to(&invalid_upload_id, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("nonzero upload opaque_id ACK timeout")??;
    let (_, invalid_upload_id_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(invalid_upload_id_ack) =
        invalid_upload_id_response
    else {
        anyhow::bail!("expected MISSION_ACK for nonzero upload opaque_id");
    };
    assert_eq!(
        invalid_upload_id_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
    );
    let no_upload_deadline = tokio::time::Instant::now() + Duration::from_millis(150);
    while tokio::time::Instant::now() < no_upload_deadline {
        let remaining = no_upload_deadline - tokio::time::Instant::now();
        match timeout(remaining, gcs.recv_from(&mut buffer)).await {
            Ok(Ok((_length, _))) if buffer[7] == 51 => {
                anyhow::bail!("nonzero upload opaque_id must not start a mission transaction");
            }
            Ok(Ok(_)) => {}
            Ok(Err(error)) => return Err(error.into()),
            Err(_) => break,
        }
    }

    let unsupported_mission_count = v2_frame(44, 42, 255, &mission_count_payload, 221);
    gcs.send_to(&unsupported_mission_count, qgc_bind).await?;
    let (_length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("unsupported-field upload mission request timeout")??;
    assert_eq!(buffer[7], 51, "bridge should request the mission item");
    let mut unsupported_waypoint_payload = mission_item[10..mission_item.len() - 2].to_vec();
    unsupported_waypoint_payload[0..4].copy_from_slice(&1.0_f32.to_le_bytes());
    let unsupported_waypoint = v2_frame(73, 43, 255, &unsupported_waypoint_payload, 38);
    gcs.send_to(&unsupported_waypoint, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("unsupported mission parameter ACK timeout")??;
    assert_eq!(
        buffer[7], 47,
        "unsupported waypoint semantics must be rejected"
    );
    let (_, unsupported_response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(unsupported_ack) = unsupported_response
    else {
        anyhow::bail!("expected MISSION_ACK for unsupported waypoint semantics");
    };
    assert_eq!(
        unsupported_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
    );
    assert!(
        timeout(Duration::from_millis(100), reply_rx.recv())
            .await
            .is_err(),
        "waypoint fields unsupported by the NATS contract must not be published"
    );

    let mut partial_upload_count = mission_count_payload.clone();
    partial_upload_count[0..2].copy_from_slice(&2_u16.to_le_bytes());
    let partial_upload = v2_frame(44, 30, 255, &partial_upload_count, 221);
    gcs.send_to(&partial_upload, qgc_bind).await?;
    let (_length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("partial upload mission request timeout")??;
    assert_eq!(
        buffer[7], 51,
        "partial upload should request its first item"
    );
    let replacement_upload = v2_frame(44, 32, 255, &mission_count_payload, 221);
    gcs.send_to(&replacement_upload, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("superseded mission upload ACK timeout")??;
    assert_eq!(
        buffer[7], 47,
        "superseded upload should be acknowledged as failed"
    );
    let (_, superseded_response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(superseded_ack) = superseded_response
    else {
        anyhow::bail!("expected MISSION_ACK for superseded upload");
    };
    assert_eq!(
        superseded_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
    );
    let (_length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("replacement upload mission request timeout")??;
    assert_eq!(
        buffer[7], 51,
        "replacement upload should request its first item"
    );
    let clear_all = v2_frame(45, 31, 255, &[1, 1, 0], 232);
    gcs.send_to(&clear_all, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("pending upload rejection ACK timeout")??;
    assert_eq!(buffer[7], 47, "pending upload should be rejected first");
    let (_, pending_upload_response) =
        mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
            &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
        )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(pending_upload_ack) =
        pending_upload_response
    else {
        anyhow::bail!("expected MISSION_ACK rejecting pending upload before clear");
    };
    assert_eq!(
        pending_upload_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
    );
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("mission clear response timeout")??;
    assert_eq!(buffer[7], 47, "MISSION_CLEAR_ALL should be acknowledged");
    let (_, clear_response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(clear_ack) = clear_response else {
        anyhow::bail!("expected MISSION_ACK for MISSION_CLEAR_ALL");
    };
    assert_eq!(
        clear_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ACCEPTED
    );
    let _ = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("mission clear request was not sent to Antoine")?;
    gcs.send_to(&matching_component_item, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("late mission item rejection timeout")??;
    assert_eq!(buffer[7], 47, "late item after clear must be rejected");
    let (_, late_item_response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::MISSION_ACK(late_item_ack) = late_item_response
    else {
        anyhow::bail!("expected MISSION_ACK for item arriving after clear");
    };
    assert_eq!(
        late_item_ack.mavtype,
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_INVALID_SEQUENCE
    );

    let rejected_upload_count = v2_frame(44, 16, 255, &mission_count_payload, 221);
    gcs.send_to(&rejected_upload_count, qgc_bind).await?;
    let (_, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("rejected upload mission request timeout")??;
    assert_eq!(
        buffer[7], 51,
        "bridge should request the rejected upload item"
    );
    gcs.send_to(&matching_component_item, qgc_bind).await?;
    let mut rejected_upload_ack = None;
    for _ in 0..4 {
        let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
            .await
            .context("rejected mission upload ACK timeout")??;
        if buffer[7] == 47 {
            let (_, response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
                &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
            )?;
            let mavlink::dialects::common::MavMessage::MISSION_ACK(response) = response else {
                anyhow::bail!("expected MISSION_ACK for rejected upload");
            };
            rejected_upload_ack = Some(response.mavtype);
            break;
        }
    }
    assert_eq!(
        rejected_upload_ack,
        Some(mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR),
        "controller rejection should be reported as a generic mission error"
    );
    let _ = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("rejected mission upload was not received")?;

    let mut unsupported_payload = arm_payload.clone();
    unsupported_payload[..28].fill(0);
    unsupported_payload[28..30].copy_from_slice(&22_u16.to_le_bytes());
    unsupported_payload[30] = 1;
    unsupported_payload[31] = 1;
    unsupported_payload[24..28].copy_from_slice(&1.0_f32.to_le_bytes());
    unsupported_payload[0..4].copy_from_slice(&1.0_f32.to_le_bytes());
    let denied_takeoff = v2_frame(76, 12, 255, &unsupported_payload[..32], 152);
    gcs.send_to(&denied_takeoff, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("denied TAKEOFF COMMAND_ACK timeout")??;
    assert_eq!(buffer[7], 77, "invalid TAKEOFF should receive COMMAND_ACK");
    let (_, denied_response) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::COMMAND_ACK(denied_ack) = denied_response else {
        anyhow::bail!("expected COMMAND_ACK for invalid TAKEOFF");
    };
    assert_eq!(
        denied_ack.command,
        mavlink::dialects::common::MavCmd::MAV_CMD_NAV_TAKEOFF
    );
    assert_eq!(
        denied_ack.result,
        mavlink::dialects::common::MavResult::MAV_RESULT_DENIED
    );
    assert!(
        timeout(Duration::from_millis(100), reply_rx.recv())
            .await
            .is_err(),
        "invalid TAKEOFF must not be published to NATS"
    );

    unsupported_payload.fill(0);
    unsupported_payload[28..30].copy_from_slice(&9999_u16.to_le_bytes());
    unsupported_payload[30] = 1;
    unsupported_payload[31] = 1;
    let unsupported_command = v2_frame(76, 13, 255, &unsupported_payload[..32], 152);
    gcs.send_to(&unsupported_command, qgc_bind).await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("unsupported COMMAND_LONG ACK timeout")??;
    assert_eq!(buffer[0], 0xfd);
    assert_eq!(buffer[1], 3);
    assert_eq!(length, 15);
    assert_eq!(buffer[7], 77);
    assert_eq!(u16::from_le_bytes([buffer[10], buffer[11]]), 9999);
    assert_eq!(
        buffer[12],
        mavlink::dialects::common::MavResult::MAV_RESULT_UNSUPPORTED as u8
    );
    let expected_crc = crc_x25(&[&buffer[1..13], &[143]].concat());
    assert_eq!(u16::from_le_bytes([buffer[13], buffer[14]]), expected_crc);
    assert!(
        timeout(Duration::from_millis(100), reply_rx.recv())
            .await
            .is_err(),
        "unsupported COMMAND_LONG must not be published to NATS"
    );

    unsupported_payload.fill(0);
    unsupported_payload[28..30].copy_from_slice(&22_u16.to_le_bytes());
    unsupported_payload[30] = 1;
    unsupported_payload[31] = 1;
    unsupported_payload[24..28].copy_from_slice(&1.0_f32.to_le_bytes());
    unsupported_payload[0..4].fill(0);
    let takeoff = v2_frame(76, 14, 255, &unsupported_payload[..32], 152);
    gcs.send_to(&takeoff, qgc_bind).await?;
    let command = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("takeoff NATS reply timeout")?
        .context("takeoff request missing/timeout")?;
    assert_eq!(command["command"]["type"], "takeoff");
    let _ = timeout(Duration::from_millis(30), gcs.recv_from(&mut buffer)).await;

    let mut disarm_payload = arm_payload;
    disarm_payload[0..4].copy_from_slice(&0.0_f32.to_le_bytes());
    let disarm = v2_frame(76, 15, 255, &disarm_payload[..32], 152);
    gcs.send_to(&disarm, qgc_bind).await?;
    let command = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("disarm NATS reply timeout")?
        .context("Antoine disarm command was not published")?;
    assert_eq!(command["command"]["type"], "disarm");
    let mut audit = None;
    while let Ok(Some(raw)) = timeout(Duration::from_secs(2), audit_rx.recv()).await {
        let record: serde_json::Value = serde_json::from_slice(&raw)?;
        if record["message_id"] == 76 {
            audit = Some(record);
            break;
        }
    }
    let audit = audit.context("missing disarm audit")?;
    assert_eq!(audit["direction"], "gcs_to_antoine");
    assert_eq!(audit["message_id"], 76);
    Ok(())
}

#[tokio::test]
async fn gazebo_nats_mode_routes_gcs_arm_through_local_authority() -> Result<()> {
    let _serial = NATS_INTEGRATION_LOCK.lock().await;
    let nats_listener = TcpListener::bind("127.0.0.1:0")?;
    let nats_port = nats_listener.local_addr()?.port();
    drop(nats_listener);
    let nats_url = format!("nats://127.0.0.1:{nats_port}");
    let store = tempfile::tempdir()?;
    let nats_child = Command::new("nats-server")
        .args([
            "-js",
            "-a",
            "127.0.0.1",
            "-p",
            &nats_port.to_string(),
            "-sd",
        ])
        .arg(store.path())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("nats-server must be installed to run the Gazebo bridge test")?;
    let mut _nats_guard = ChildGuard(nats_child);
    let client = timeout(Duration::from_secs(5), async {
        loop {
            if let Ok(client) = async_nats::connect(&nats_url).await {
                break client;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    })
    .await
    .context("local NATS server did not start")?;
    let js = jetstream::new(client.clone());
    wait_for_jetstream(&js).await?;

    let callsign = "gazebo-authority-test";
    let mut controls = client
        .subscribe(format!("furia.mavlink.control.{callsign}"))
        .await?;
    client.flush().await?;
    let gcs = UdpSocket::bind("127.0.0.1:0").await?;
    let qgc_peer = gcs.local_addr()?;
    let qgc_bind = free_udp_addr()?;
    let bridge = Command::new(env!("CARGO_BIN_EXE_antoine-mavlink-jetstream-bridge"))
        .args([
            "--mode",
            "gazebo-nats",
            "--run-id",
            "gazebo-authority-test-run",
            "--nats-url",
            &nats_url,
            "--callsign",
            callsign,
            "--qgc-bind",
            &qgc_bind.to_string(),
            "--qgc-peer",
            &qgc_peer.to_string(),
            "--system-id",
            "1",
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    let _bridge_guard = ChildGuard(bridge);
    wait_for_udp_bind(qgc_bind).await?;

    let mut arm_payload = [0_u8; 33];
    arm_payload[0..4].copy_from_slice(&1.0_f32.to_le_bytes());
    arm_payload[28..30].copy_from_slice(&400_u16.to_le_bytes());
    arm_payload[30] = 1;
    arm_payload[31] = 1;
    let arm = v2_frame(76, 10, 10, &arm_payload[..32], 152);
    gcs.send_to(&arm, qgc_bind).await?;

    let request = timeout(Duration::from_secs(2), controls.next())
        .await
        .context("local authority did not receive the GCS arm request")?
        .context("local authority subscription ended")?;
    assert_eq!(
        request.subject.as_str(),
        format!("furia.mavlink.control.{callsign}")
    );
    let envelope: serde_json::Value = serde_json::from_slice(&request.payload)?;
    assert_eq!(envelope["schema"], "furia.mavlink.control");
    assert_eq!(envelope["identity"], callsign);
    assert_eq!(envelope["command"]["type"], "arm");
    let request_id = envelope["request_id"]
        .as_str()
        .context("Gazebo authority request omitted its correlation ID")?;
    let reply = request
        .reply
        .context("Gazebo authority request omitted its reply subject")?;
    client
        .publish(
            reply,
            serde_json::json!({
                "schema": "wrong.result",
                "version": "1.0.0",
                "requestId": request_id,
                "accepted": true,
                "armed": true,
                "mode": "GUIDED"
            })
            .to_string()
            .into(),
        )
        .await?;
    client.flush().await?;

    let mut buffer = [0_u8; 512];
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("GCS did not receive rejection for malformed authority result")??;
    let (_, message) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::COMMAND_ACK(ack) = message else {
        anyhow::bail!("expected MAVLink COMMAND_ACK for malformed authority result");
    };
    assert_eq!(
        ack.result,
        mavlink::dialects::common::MavResult::MAV_RESULT_DENIED
    );

    let arm_retry = v2_frame(76, 11, 10, &arm_payload[..32], 152);
    gcs.send_to(&arm_retry, qgc_bind).await?;
    let request = timeout(Duration::from_secs(2), controls.next())
        .await
        .context("local authority did not receive the retried arm request")?
        .context("local authority subscription ended")?;
    let envelope: serde_json::Value = serde_json::from_slice(&request.payload)?;
    let request_id = envelope["request_id"]
        .as_str()
        .context("retried Gazebo authority request omitted its correlation ID")?;
    let reply = request
        .reply
        .context("retried Gazebo authority request omitted its reply subject")?;
    client
        .publish(
            reply,
            serde_json::json!({
                "schema": "furia.mavlink.control.result",
                "version": "1.0.0",
                "requestId": request_id,
                "accepted": true,
                "armed": true,
                "mode": "GUIDED"
            })
            .to_string()
            .into(),
        )
        .await?;
    client.flush().await?;

    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("GCS did not receive the authority decision")??;
    let (_, message) = mavlink::read_v2_msg::<mavlink::dialects::common::MavMessage, _>(
        &mut mavlink::peek_reader::PeekReader::new(std::io::Cursor::new(&buffer[..length])),
    )?;
    let mavlink::dialects::common::MavMessage::COMMAND_ACK(ack) = message else {
        anyhow::bail!("expected MAVLink COMMAND_ACK from the Gazebo authority decision");
    };
    assert_eq!(
        ack.result,
        mavlink::dialects::common::MavResult::MAV_RESULT_ACCEPTED
    );

    let observed_at_unix_ms = SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis() as u64;
    let gazebo_telemetry = |run_id: &str| {
        serde_json::json!({
            "schema":"furia.telemetry", "version":"1.0.0", "protocol":"antoine-gz",
            "identity":callsign, "runId":run_id, "observedAtUnixMs":observed_at_unix_ms,
            "lat_deg":48.0002, "lon_deg":0.3564, "alt_m":0.2,
            "vn_mps":0.0, "ve_mps":0.0, "vd_mps":0.0,
            "battery_pct":95, "control_authority":"core-execution-grant"
        })
    };
    client
        .publish(
            format!("furia.telemetry.{callsign}"),
            gazebo_telemetry("other-run").to_string().into(),
        )
        .await?;
    client.flush().await?;
    let mut buffer = [0_u8; 512];
    assert!(
        timeout(Duration::from_millis(250), gcs.recv_from(&mut buffer))
            .await
            .is_err(),
        "telemetry from a different profile run must not cross into MAVLink"
    );

    let matching_telemetry = gazebo_telemetry("gazebo-authority-test-run");
    client
        .publish(
            format!("furia.telemetry.{callsign}"),
            matching_telemetry.to_string().into(),
        )
        .await?;
    client.flush().await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("run-matched Gazebo telemetry was not forwarded")??;
    assert_eq!(
        buffer[7], 0,
        "run-matched telemetry should produce HEARTBEAT"
    );
    assert!(length >= 21);
    for _ in 1..5 {
        timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
            .await
            .context("bridge telemetry projection was incomplete")??;
    }
    tokio::time::sleep(Duration::from_millis(150)).await;
    client
        .publish(
            format!("furia.telemetry.{callsign}"),
            matching_telemetry.to_string().into(),
        )
        .await?;
    client.flush().await?;
    assert!(
        timeout(Duration::from_millis(200), gcs.recv_from(&mut buffer))
            .await
            .is_err(),
        "duplicate Gazebo observation timestamps must not be projected twice"
    );

    let stream = js.get_stream(STREAM).await?;
    let audit = wait_for_raw_subject(&stream, &format!("furia.mavlink.command.{callsign}")).await?;
    let audit: serde_json::Value = serde_json::from_slice(&audit.payload)?;
    assert_eq!(audit["direction"], "gcs_to_antoine");
    assert_eq!(audit["message_id"], 76);
    assert_eq!(audit["runId"], "gazebo-authority-test-run");
    Ok(())
}
