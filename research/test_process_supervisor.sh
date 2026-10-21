#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/dvd-process-supervisor-test.XXXXXX")"
SUPERVISOR="${TEMP_ROOT}/process-supervisor"

cleanup() {
  for identity in "${TEMP_ROOT}"/*.identity; do
    [[ -e "${identity}" ]] || continue
    "${SUPERVISOR}" stop "${identity}" >/dev/null 2>&1 || true
  done
  rm -rf "${TEMP_ROOT}"
}
trap cleanup EXIT

compiler="$(command -v clang || command -v cc || true)"
[[ -n "${compiler}" ]] || { echo "a C compiler is required" >&2; exit 1; }
"${compiler}" -std=c11 -Wall -Wextra -Werror -O2 \
  "${PROJECT_ROOT}/research/dvd_research/process_supervisor.c" -o "${SUPERVISOR}"

wait_for_file() {
  local path="$1"
  for _ in $(seq 1 100); do
    [[ -s "${path}" ]] && return 0
    sleep 0.02
  done
  return 1
}

identity_fifo() {
  sed -n 's/^fifo=//p' "$1"
}

identity_fifo_exists() {
  compgen -G "$1.control.*" >/dev/null
}

# A child exit racing a stop request must leave no stale run-control files.
identity="${TEMP_ROOT}/race.identity"
"${SUPERVISOR}" run "${identity}" -- sh -c 'exit 7' & supervisor_pid=$!
wait_for_file "${identity}"
"${SUPERVISOR}" stop "${identity}" || true
set +e
wait "${supervisor_pid}"
race_status=$?
set -e
[[ "${race_status}" == 7 || "${race_status}" == 143 ]]
[[ ! -e "${identity}" ]] && ! identity_fifo_exists "${identity}"

# A direct stop can arrive as soon as the run identity becomes visible. The
# supervisor must already own the signal and reap its child before removing it.
identity="${TEMP_ROOT}/early-signal.identity"
child_pid_file="${TEMP_ROOT}/early-signal-child.pid"
"${SUPERVISOR}" run "${identity}" -- sh -c \
  'echo $$ > "$1"; trap "exit 0" TERM INT; while :; do sleep 0.05; done' \
  _ "${child_pid_file}" & early_signal_pid=$!
wait_for_file "${identity}"
early_fifo="$(identity_fifo "${identity}")"
kill -TERM "${early_signal_pid}"
set +e
wait "${early_signal_pid}"
early_signal_status=$?
set -e
[[ "${early_signal_status}" == 0 || "${early_signal_status}" == 143 ]]
if [[ -s "${child_pid_file}" ]]; then
  early_child_pid="$(cat "${child_pid_file}")"
  for _ in $(seq 1 100); do
    kill -0 "${early_child_pid}" 2>/dev/null || break
    sleep 0.02
  done
  if kill -0 "${early_child_pid}" 2>/dev/null; then
    echo "child survived an early supervisor signal" >&2
    exit 1
  fi
fi
[[ ! -e "${identity}" && ! -e "${early_fifo}" ]]

# Replacing the identity's FIFO reference cannot stop another live process.
sh -c 'trap "exit 0" TERM INT; while :; do sleep 0.05; done' & unrelated_pid=$!
identity="${TEMP_ROOT}/owned.identity"
"${SUPERVISOR}" run "${identity}" -- sh -c \
  'trap "exit 0" TERM INT; while :; do sleep 0.05; done' & owned_pid=$!
wait_for_file "${identity}"
owned_fifo="$(identity_fifo "${identity}")"
sed "s|${owned_fifo}|${TEMP_ROOT}/foreign.control|" "${identity}" >"${TEMP_ROOT}/stale.identity"
if "${SUPERVISOR}" stop "${TEMP_ROOT}/stale.identity" >/dev/null 2>&1; then
  echo "supervisor accepted a replaced identity" >&2
  exit 1
fi
ln -s "${identity}" "${TEMP_ROOT}/linked.identity"
if "${SUPERVISOR}" stop "${TEMP_ROOT}/linked.identity" >/dev/null 2>&1; then
  echo "supervisor followed a symlinked identity record" >&2
  exit 1
fi
owned_child_pid="$(awk -F= '$1 == "child_pid" { print $2; exit }' "${identity}")"
kill -0 "${owned_child_pid}"
rm "${TEMP_ROOT}/linked.identity"
mv "${owned_fifo}" "${TEMP_ROOT}/owned.control"
mkfifo "${TEMP_ROOT}/foreign.control"
ln -s "${TEMP_ROOT}/foreign.control" "${owned_fifo}"
if "${SUPERVISOR}" stop "${identity}" >/dev/null 2>&1; then
  echo "supervisor accepted a symlinked control FIFO" >&2
  exit 1
fi
kill -0 "${owned_child_pid}"
rm "${owned_fifo}" "${TEMP_ROOT}/foreign.control"
mv "${TEMP_ROOT}/owned.control" "${owned_fifo}"
kill -0 "${unrelated_pid}"
"${SUPERVISOR}" stop "${identity}"
wait "${owned_pid}" || true
kill -TERM "${unrelated_pid}"
wait "${unrelated_pid}" || true

# Reusing an identity path after a clean stop must not let an old receipt stop
# the new run through the newly-created FIFO at that path.
identity="${TEMP_ROOT}/restart.identity"
"${SUPERVISOR}" run "${identity}" -- sh -c \
  'trap "exit 0" TERM INT; while :; do sleep 0.05; done' & first_run_pid=$!
wait_for_file "${identity}"
first_fifo="$(identity_fifo "${identity}")"
cp "${identity}" "${TEMP_ROOT}/previous-run.identity"
"${SUPERVISOR}" stop "${identity}"
wait "${first_run_pid}" || true
"${SUPERVISOR}" run "${identity}" -- sh -c \
  'trap "exit 0" TERM INT; while :; do sleep 0.05; done' & restarted_pid=$!
wait_for_file "${identity}"
restarted_fifo="$(identity_fifo "${identity}")"
[[ "${first_fifo}" != "${restarted_fifo}" ]]
if "${SUPERVISOR}" stop "${TEMP_ROOT}/previous-run.identity" >/dev/null 2>&1; then
  echo "supervisor accepted stale receipt after identity-path reuse" >&2
  exit 1
fi
restarted_child_pid="$(awk -F= '$1 == "child_pid" { print $2; exit }' "${identity}")"
kill -0 "${restarted_child_pid}"
"${SUPERVISOR}" stop "${identity}"
wait "${restarted_pid}" || true
[[ ! -e "${identity}" && ! -e "${restarted_fifo}" ]]

# Escalation must stop an owned child that deliberately ignores TERM.
identity="${TEMP_ROOT}/escalation.identity"
"${SUPERVISOR}" run "${identity}" -- sh -c \
  'trap "" TERM INT; echo $$ > "$1"; while :; do sleep 0.05; done' \
  _ "${TEMP_ROOT}/ignores-term.pid" & escalation_pid=$!
wait_for_file "${TEMP_ROOT}/ignores-term.pid"
escalation_fifo="$(identity_fifo "${identity}")"
ignored_pid="$(cat "${TEMP_ROOT}/ignores-term.pid")"
"${SUPERVISOR}" stop "${identity}"
set +e
wait "${escalation_pid}"
escalation_status=$?
set -e
[[ "${escalation_status}" == 137 ]]
if kill -0 "${ignored_pid}" 2>/dev/null; then
  echo "TERM-ignoring owned process survived escalation" >&2
  exit 1
fi
[[ ! -e "${identity}" && ! -e "${escalation_fifo}" ]]

# A run wrapper may need several seconds to stop its own children after TERM.
# The supervisor must allow that bounded cleanup before process-group escalation.
identity="${TEMP_ROOT}/graceful-cleanup.identity"
"${SUPERVISOR}" run "${identity}" -- sh -c \
  'trap '\''trap "" TERM; sleep 2; exit 0'\'' TERM; while :; do sleep 0.05; done' & graceful_pid=$!
wait_for_file "${identity}"
graceful_fifo="$(identity_fifo "${identity}")"
"${SUPERVISOR}" stop "${identity}"
set +e
wait "${graceful_pid}"
graceful_status=$?
set -e
[[ "${graceful_status}" == 0 ]]
[[ ! -e "${identity}" && ! -e "${graceful_fifo}" ]]

# Stopping an owned process group also removes a direct descendant.
identity="${TEMP_ROOT}/tree.identity"
"${SUPERVISOR}" run "${identity}" -- sh -c \
  'sh -c '\''trap "exit 0" TERM INT; while :; do sleep 0.05; done'\'' & echo $! > "$1"; trap "exit 0" TERM INT; while :; do sleep 0.05; done' \
  _ "${TEMP_ROOT}/descendant.pid" & tree_pid=$!
wait_for_file "${TEMP_ROOT}/descendant.pid"
tree_fifo="$(identity_fifo "${identity}")"
descendant_pid="$(cat "${TEMP_ROOT}/descendant.pid")"
"${SUPERVISOR}" stop "${identity}"
wait "${tree_pid}" || true
for _ in $(seq 1 100); do
  kill -0 "${descendant_pid}" 2>/dev/null || break
  sleep 0.02
done
if kill -0 "${descendant_pid}" 2>/dev/null; then
  echo "owned descendant survived process-group cleanup" >&2
  exit 1
fi
[[ ! -e "${identity}" && ! -e "${tree_fifo}" ]]
echo "process supervisor acceptance passed"
