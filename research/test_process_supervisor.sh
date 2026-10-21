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
[[ ! -e "${identity}" && ! -e "${identity}.control" ]]

# Replacing the identity's FIFO reference cannot stop another live process.
sh -c 'trap "exit 0" TERM INT; while :; do sleep 0.05; done' & unrelated_pid=$!
identity="${TEMP_ROOT}/owned.identity"
"${SUPERVISOR}" run "${identity}" -- sh -c \
  'trap "exit 0" TERM INT; while :; do sleep 0.05; done' & owned_pid=$!
wait_for_file "${identity}"
sed "s|${identity}\.control|${TEMP_ROOT}/foreign.control|" "${identity}" >"${TEMP_ROOT}/stale.identity"
if "${SUPERVISOR}" stop "${TEMP_ROOT}/stale.identity" >/dev/null 2>&1; then
  echo "supervisor accepted a replaced identity" >&2
  exit 1
fi
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
cp "${identity}" "${TEMP_ROOT}/previous-run.identity"
"${SUPERVISOR}" stop "${identity}"
wait "${first_run_pid}" || true
"${SUPERVISOR}" run "${identity}" -- sh -c \
  'trap "exit 0" TERM INT; while :; do sleep 0.05; done' & restarted_pid=$!
wait_for_file "${identity}"
if "${SUPERVISOR}" stop "${TEMP_ROOT}/previous-run.identity" >/dev/null 2>&1; then
  echo "supervisor accepted stale receipt after identity-path reuse" >&2
  exit 1
fi
restarted_child_pid="$(awk -F= '$1 == "child_pid" { print $2; exit }' "${identity}")"
kill -0 "${restarted_child_pid}"
"${SUPERVISOR}" stop "${identity}"
wait "${restarted_pid}" || true
[[ ! -e "${identity}" && ! -e "${identity}.control" ]]

# Escalation must stop an owned child that deliberately ignores TERM.
identity="${TEMP_ROOT}/escalation.identity"
"${SUPERVISOR}" run "${identity}" -- sh -c \
  'trap "" TERM INT; echo $$ > "$1"; while :; do sleep 0.05; done' \
  _ "${TEMP_ROOT}/ignores-term.pid" & escalation_pid=$!
wait_for_file "${TEMP_ROOT}/ignores-term.pid"
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
[[ ! -e "${identity}" && ! -e "${identity}.control" ]]

# Stopping an owned process group also removes a direct descendant.
identity="${TEMP_ROOT}/tree.identity"
"${SUPERVISOR}" run "${identity}" -- sh -c \
  'sh -c '\''trap "exit 0" TERM INT; while :; do sleep 0.05; done'\'' & echo $! > "$1"; trap "exit 0" TERM INT; while :; do sleep 0.05; done' \
  _ "${TEMP_ROOT}/descendant.pid" & tree_pid=$!
wait_for_file "${TEMP_ROOT}/descendant.pid"
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
[[ ! -e "${identity}" && ! -e "${identity}.control" ]]
echo "process supervisor acceptance passed"
