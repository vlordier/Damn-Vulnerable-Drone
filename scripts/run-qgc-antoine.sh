#!/usr/bin/env bash
set -euo pipefail

ANTOINE_QGC_DIR="${ANTOINE_QGC_DIR:-${HOME}/Work/antoine-qgroundcontrol}"
QGC_CONFIG="${QGC_CONFIG:-Debug}"
QGC_UDP_LISTEN_PORT="${QGC_UDP_LISTEN_PORT:-14550}"
QGC_BRIDGE_PORT="${QGC_BRIDGE_PORT:-14651}"
QGC_SETTINGS_DIR="${QGC_SETTINGS_DIR:-/tmp/dvd-qgc-antoine-config}"
QGC_APP="${ANTOINE_QGC_BINARY:-${ANTOINE_QGC_DIR}/build/${QGC_CONFIG}/QGroundControl.app/Contents/MacOS/QGroundControl}"
QGC_SETTINGS_FILE="${QGC_SETTINGS_FILE:-${QGC_SETTINGS_DIR}/qgroundcontrol.com/QGroundControl Daily.ini}"

[[ -x "${QGC_APP}" ]] || {
  echo "Missing executable local QGC build: ${QGC_APP}" >&2
  exit 1
}
if lsof -nP -iUDP:"${QGC_UDP_LISTEN_PORT}" | rg -q 'QGroundCo'; then
  echo "QGC already owns UDP ${QGC_UDP_LISTEN_PORT}; close that instance before launching this profile." >&2
  exit 1
fi
lsof -nP -iUDP:"${QGC_BRIDGE_PORT}" >/dev/null 2>&1 || {
  echo "No bridge is listening on UDP ${QGC_BRIDGE_PORT}. Start the Antoine bridge first." >&2
  exit 1
}

mkdir -p "$(dirname "${QGC_SETTINGS_FILE}")"
cat >"${QGC_SETTINGS_FILE}" <<EOF
[AutoConnect]
autoConnectUDP=true
udpListenPort=${QGC_UDP_LISTEN_PORT}
udpTargetHostIP=127.0.0.1
udpTargetHostPort=${QGC_BRIDGE_PORT}
EOF

mkdir -p /tmp/dvd-qgc-antoine
nohup env XDG_CONFIG_HOME="${QGC_SETTINGS_DIR}" "${QGC_APP}" --allow-multiple \
  > /tmp/dvd-qgc-antoine/qgc.log 2>&1 </dev/null &
pid=$!
printf '%s\n' "${pid}" >/tmp/dvd-qgc-antoine/qgc.pid

for ((attempt = 0; attempt < 40; attempt++)); do
  if lsof -nP -iUDP:"${QGC_UDP_LISTEN_PORT}" | rg -q 'QGroundCo'; then
    echo "Local QGC listening on UDP ${QGC_UDP_LISTEN_PORT}; bridge peer 127.0.0.1:${QGC_BRIDGE_PORT}"
    echo "QGC log: /tmp/dvd-qgc-antoine/qgc.log"
    exit 0
  fi
  if ! kill -0 "${pid}" 2>/dev/null; then
    cat /tmp/dvd-qgc-antoine/qgc.log >&2
    exit 1
  fi
  sleep 0.25
done

cat /tmp/dvd-qgc-antoine/qgc.log >&2
echo "QGC did not bind UDP ${QGC_UDP_LISTEN_PORT}" >&2
exit 1
