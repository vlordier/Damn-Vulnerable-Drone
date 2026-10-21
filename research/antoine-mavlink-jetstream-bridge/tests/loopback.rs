use std::{
    net::{SocketAddr, TcpListener, UdpSocket as StdUdpSocket},
    process::{Child, Command, Stdio},
    time::Duration,
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
    let mut body = vec![payload.len() as u8, 0, 0, sequence, system_id, 1];
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
        info.state.messages, 8,
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
    let telemetry_observer = UdpSocket::bind("127.0.0.1:0").await?;
    let telemetry_observer_addr = telemetry_observer.local_addr()?.to_string();
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
            "--qgc-telemetry-peer",
            &telemetry_observer_addr,
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
        while let Some(message) = controls.next().await {
            if let Some(reply) = message.reply {
                let value: serde_json::Value = serde_json::from_slice(&message.payload).unwrap();
                let _ = reply_tx.send(value.clone());
                let request_id = value["request_id"].clone();
                let reject_mission_query = request_id
                    .as_str()
                    .is_some_and(|id| id.contains("-mission-query-12-"));
                let omit_mission_state = request_id
                    .as_str()
                    .is_some_and(|id| id.contains("-mission-query-13-"));
                let reject_mission_upload = value["command"]["type"] == "mission_upload"
                    && upload_count.fetch_add(1, std::sync::atomic::Ordering::Relaxed) > 0;
                let response = if reject_mission_query {
                    serde_json::json!({"requestId":request_id,"accepted":false,"armed":true,"mode":"GUIDED"})
                } else if omit_mission_state {
                    serde_json::json!({"requestId":request_id,"accepted":true,"armed":true,"mode":"GUIDED"})
                } else if reject_mission_upload {
                    serde_json::json!({"requestId":request_id,"accepted":false,"armed":true,"mode":"GUIDED"})
                } else {
                    serde_json::json!({"requestId":request_id,"accepted":true,"armed":true,"mode":"GUIDED","missionUploaded":false,"waypoints":[]})
                };
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
    let mut rtl_payload = vec![0_u8; 33];
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
    assert!(
        timeout(Duration::from_millis(150), gcs.recv_from(&mut buffer))
            .await
            .is_err(),
        "rejected RTL parameters must not receive a success ACK"
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

    client
        .publish(
            format!("furia.telemetry.{callsign}"),
            serde_json::json!({
                "schema":"furia.telemetry", "version":"1.0.0", "protocol":"antoine",
                "identity":callsign, "time_s":1.25, "lat_deg":48.0002, "lon_deg":0.3564,
                "alt_m":0.0, "vn_mps":0.1, "ve_mps":-0.1, "vd_mps":0.1,
                "actuator_thrust":0.0, "ground_contact":true, "gnss_degraded":false,
                "battery_pct":100, "battery_mv":12000
            })
            .to_string()
            .into(),
        )
        .await?;
    client.flush().await?;
    let (length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("heartbeat timeout")??;
    assert_eq!(buffer[7], 0, "QGC should receive HEARTBEAT");
    let (observer_length, _) = timeout(
        Duration::from_secs(2),
        telemetry_observer.recv_from(&mut buffer),
    )
    .await
    .context("configured telemetry observer did not receive HEARTBEAT")??;
    assert!(observer_length >= 21);
    assert_eq!(buffer[7], 0);
    assert!(
        timeout(Duration::from_millis(100), probe.recv_from(&mut buffer))
            .await
            .is_err(),
        "probe must not receive QGC telemetry"
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

    let mission_list_request = v2_frame(43, 11, 255, &[1, 1, 0], 132);
    gcs.send_to(&mission_list_request, qgc_bind).await?;
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
    let rejected_mission_list = v2_frame(43, 12, 255, &[1, 1, 0], 132);
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
    let malformed_mission_list = v2_frame(43, 13, 255, &[1, 1, 0], 132);
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
    let mut mission_count_payload = vec![0; 4];
    mission_count_payload[0..2].copy_from_slice(&1_u16.to_le_bytes());
    mission_count_payload[2] = 1;
    mission_count_payload[3] = 1;
    let mission_count_request = v2_frame(44, 14, 255, &mission_count_payload, 221);
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
    gcs.send_to(&mission_item, qgc_bind).await?;
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
        mavlink::dialects::common::MavMissionResult::MAV_MISSION_ERROR
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
    let (_length, _) = timeout(Duration::from_secs(2), gcs.recv_from(&mut buffer))
        .await
        .context("mission clear response timeout")??;
    assert_eq!(buffer[7], 47, "MISSION_CLEAR_ALL should be acknowledged");
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
    unsupported_payload[28..30].copy_from_slice(&22_u16.to_le_bytes());
    unsupported_payload[24..28].copy_from_slice(&1.0_f32.to_le_bytes());
    let takeoff = v2_frame(76, 12, 255, &unsupported_payload[..32], 152);
    gcs.send_to(&takeoff, qgc_bind).await?;
    let command = timeout(Duration::from_secs(2), reply_rx.recv())
        .await
        .context("takeoff NATS reply timeout")?
        .context("takeoff request missing/timeout")?;
    assert_eq!(command["command"]["type"], "takeoff");
    let _ = timeout(Duration::from_millis(30), gcs.recv_from(&mut buffer)).await;

    let mut disarm_payload = arm_payload;
    disarm_payload[0..4].copy_from_slice(&0.0_f32.to_le_bytes());
    let disarm = v2_frame(76, 13, 255, &disarm_payload[..32], 152);
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
