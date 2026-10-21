#!/usr/bin/env bash
#
# Gracefully stop the Damn Vulnerable Drone lab

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/research/owned_process.sh"
export COMPOSE_PROJECT_NAME="damn-vulnerable-drone"
RUNTIME_STATE_DIR="${SCRIPT_DIR}/.antoine-runtime"
ACTIVE_RUN_DIR="${RUNTIME_STATE_DIR}/active"
ACTIVE_IDENTITY=""

# Stop only the checkout-owned native research console.
if command -v uv >/dev/null 2>&1; then
  "${SCRIPT_DIR}/research/console.sh" stop
  CONSOLE_STOP_NOTE="The research console is stopped."
else
  CONSOLE_STOP_NOTE="uv is unavailable; research-console state was not checked."
  echo "${CONSOLE_STOP_NOTE}" >&2
fi

# Stop only the Antoine wrapper recorded by this checkout. Never signal a
# process selected by name, and never touch another checkout's active marker.
if [[ -L "${RUNTIME_STATE_DIR}" ]]; then
  echo "Refusing to inspect Antoine state through a symlinked directory." >&2
  exit 1
fi
if [[ -L "${ACTIVE_RUN_DIR}" ]]; then
  echo "Refusing to inspect a symlinked Antoine active-run marker." >&2
  exit 1
fi
if [[ -d "${ACTIVE_RUN_DIR}" ]]; then
  if [[ -L "${ACTIVE_RUN_DIR}/pid" || (-e "${ACTIVE_RUN_DIR}/pid" && ! -f "${ACTIVE_RUN_DIR}/pid") || -L "${ACTIVE_RUN_DIR}/supervisor-identity" || (-e "${ACTIVE_RUN_DIR}/supervisor-identity" && ! -f "${ACTIVE_RUN_DIR}/supervisor-identity") ]]; then
    echo "Refusing to inspect an unsafe Antoine PID marker." >&2
    exit 1
  fi
  ACTIVE_PID="$(cat "${ACTIVE_RUN_DIR}/pid" 2>/dev/null || true)"
  ACTIVE_IDENTITY="$(cat "${ACTIVE_RUN_DIR}/supervisor-identity" 2>/dev/null || true)"
  if [[ ! "${ACTIVE_PID}" =~ ^[0-9]+$ ]] || ! kill -0 "${ACTIVE_PID}" 2>/dev/null; then
    echo "Removing stale Antoine run marker; no live recorded PID was found."
    rm -rf "${ACTIVE_RUN_DIR}"
  else
    echo "Stopping this checkout's Antoine run (PID ${ACTIVE_PID})..."
    # Failure is handled below by the explicit refusal message and exit.
    # shellcheck disable=SC2310
    if ! dvd_signal_owned_child "${SCRIPT_DIR}" "${ACTIVE_PID}" "${ACTIVE_IDENTITY}" TERM; then
      echo "Refusing to stop Antoine: its native supervisor identity is missing or no longer valid." >&2
      exit 1
    fi
    STOPPED=0
    for _ in {1..40}; do
      PROCESS_STATE="$(ps -p "${ACTIVE_PID}" -o stat= 2>/dev/null || true)"
      if [[ -z "${PROCESS_STATE}" || "${PROCESS_STATE}" == Z* ]]; then
        STOPPED=1
        break
      fi
      sleep 0.25
    done
    if [[ "${STOPPED}" -ne 1 ]]; then
      echo "Antoine launcher has not exited yet; check its terminal and run logs." >&2
      exit 1
    fi
    echo "Antoine run stopped."
  fi
fi

# Native Antoine runs and the research console need no elevated privileges.
# On macOS (or an unprivileged Linux shell), finish successfully when there is
# no legacy Docker lab left to stop; only the old Wi-Fi/container cleanup needs
# root.
if [[ ${EUID} -ne 0 ]]; then
  LEGACY_RUNNING=0
  if command -v docker >/dev/null 2>&1; then
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -Eq '^(simulator|simulator-lite|companion-computer|companion-computer-lite|ground-control-station|ground-control-station-lite)$'; then
      LEGACY_RUNNING=1
    fi
  fi
  if [[ "${LEGACY_RUNNING}" -eq 0 ]]; then
    echo "Native Antoine flight profile is stopped. No privileged legacy lab is running. ${CONSOLE_STOP_NOTE}"
    exit 0
  fi
fi

# ─────────────────────────  Pretty logging helpers  ────────────────────────────
NC='\033[0m'
CYAN='\033[0;36m'
RED='\033[0;31m'
GRN='\033[0;32m'
log() { echo -e "${CYAN}[+]${NC} $*"; }
warn() { echo -e "${RED}[!]${NC} $*"; }
ok() { echo -e "${GRN}[✓]${NC} $*"; }

# ───────────────────────────  Root check  ──────────────────────────────────────
if [[ ${EUID} -ne 0 ]]; then
  warn "This script must be run with sudo/root."
  warn "Try: sudo ./stop.sh"
  exit 1
fi

STOP_TIMESTAMP="$(date)"
log "Stopping Damn Vulnerable Drone Lab – ${STOP_TIMESTAMP}"

# ─────────────────────  Helper: is a WLAN interface virtual?  ──────────────────
is_hwsim_iface() {
  local iface="$1"
  local phy
  phy=$(readlink -f "/sys/class/net/${iface}/device/ieee80211" 2>/dev/null || true)
  [[ -n "${phy}" && "${phy}" =~ mac80211_hwsim ]]
}

# ───────────────────── 1. Drain Docker Compose  ───────────────────────────────
log "Stopping Docker Compose services..."
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  docker compose -f "${SCRIPT_DIR}/docker-compose.yaml" down --remove-orphans
  docker compose -f "${SCRIPT_DIR}/docker-compose-lite.yaml" down --remove-orphans
  ok "Docker stacks stopped"
else
  warn "Docker is unavailable; no legacy Docker stack was stopped"
fi

# ───────────────────── 2. Quiet networking daemons  ───────────────────────────
log "Pausing NetworkManager / wpa_supplicant"
if command -v systemctl &>/dev/null; then
  systemctl stop NetworkManager 2>/dev/null || true
  systemctl stop wpa_supplicant 2>/dev/null || true
else
  service NetworkManager stop 2>/dev/null || true
  pkill -TERM wpa_supplicant 2>/dev/null || true
fi

# ───────────────────── 3. Delete virtual WLAN interfaces  ─────────────────────
log "Removing mac80211_hwsim interfaces..."
mapfile -t ifaces < <(iw dev | awk '$1=="Interface"{print $2}' | tac || true)
for iface in "${ifaces[@]}"; do
  # A false result means this interface is not owned by mac80211_hwsim.
  # shellcheck disable=SC2310
  if is_hwsim_iface "${iface}"; then
    ip link set "${iface}" down 2>/dev/null || true
    log "  • ${iface}"
    iw dev "${iface}" del 2>/dev/null || true
  fi
done
ok "Virtual WLAN cleanup complete"

# ───────────────────── 4. Unload hwsim kernel module  ─────────────────────────
if lsmod | grep -q '^mac80211_hwsim'; then
  log "Attempting to unload mac80211_hwsim..."
  if modprobe -r mac80211_hwsim 2>/dev/null; then
    ok "mac80211_hwsim unloaded"
  else
    warn "Module still in use – leaving it loaded"
  fi
fi

# ───────────────────── 5. Resume networking services  ─────────────────────────-
log "Restarting networking"
if command -v systemctl &>/dev/null; then
  systemctl start NetworkManager 2>/dev/null || true
else
  service NetworkManager start 2>/dev/null || true
fi
service networking restart 2>/dev/null || true

ok "System ready. Lab is fully stopped."
exit 0
