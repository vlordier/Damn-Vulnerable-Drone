#!/bin/bash

# Define container names
CONTAINERS=("companion-computer" "ground-control-station" "flight-controller" "simulator")
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_STATE_DIR="${SCRIPT_DIR}/.antoine-runtime"
ACTIVE_RUN_DIR="${RUNTIME_STATE_DIR}/active"

if [[ -L "${RUNTIME_STATE_DIR}" ]]; then
  echo "Antoine runtime-state path is a symlink; skipping native PID status."
elif [[ -d "${ACTIVE_RUN_DIR}" ]]; then
  ACTIVE_PID="$(cat "${ACTIVE_RUN_DIR}/pid" 2>/dev/null || true)"
  if [[ "${ACTIVE_PID}" =~ ^[0-9]+$ ]] && kill -0 "${ACTIVE_PID}" 2>/dev/null; then
    PROCESS_COMMAND="$(ps -ww -p "${ACTIVE_PID}" -o command= 2>/dev/null || true)"
    if [[ "${PROCESS_COMMAND}" == *"${SCRIPT_DIR}/scripts/run-antoine.sh"* ]]; then
      echo "Antoine native flight profile is running. PID: ${ACTIVE_PID}"
    else
      echo "Antoine run marker PID does not match this checkout's launcher."
    fi
  else
    echo "Stale Antoine run marker found. Run ./stop.sh to clear it."
  fi
fi
"${SCRIPT_DIR}/research/console.sh" status || true

# Check if Docker is running
if ! command -v docker &>/dev/null; then
  echo "Docker is not installed; legacy container profiles are unavailable."
  exit 0
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is unavailable; legacy container status cannot be inspected."
  exit 0
fi

# Function to check the status of each container
check_container_status() {
  local container=$1
  local status
  status=$(docker ps -a --filter "name=${container}" --format "{{.Status}}" | head -n 1 || true)

  if [[ ${status} == *"Up"* ]]; then
    echo "${container} is running. Status: ${status}"
  elif [[ ${status} == *"Exited"* ]]; then
    echo "${container} is stopped. Status: ${status}"
  else
    echo "${container} not found."
  fi
}

echo "Checking the status of the Damn Vulnerable Drone environment..."

# Check the status of each container
for container in "${CONTAINERS[@]}"; do
  check_container_status "${container}"
done
