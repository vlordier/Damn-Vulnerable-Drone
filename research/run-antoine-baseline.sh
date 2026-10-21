#!/usr/bin/env bash
# Produce a clean authorized flight control for comparison with attack runs.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORT_ROOT="${ANTOINE_REPORT_DIR:-$PROJECT_ROOT/build_logs/research/antoine-scenarios}"
mkdir -p "$REPORT_ROOT"
RUN_ID="antoine-baseline-$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$REPORT_ROOT/$RUN_ID"
[[ ! -e "$RUN_DIR" ]] || { echo "Refusing to overwrite evidence: $RUN_DIR" >&2; exit 1; }

if [[ -z "${ANTOINE_GZ_FLIGHT_NATS_PORT:-}" ]]; then
  ANTOINE_GZ_FLIGHT_NATS_PORT="$(uv run --no-project python -c \
    'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi
export ANTOINE_GZ_FLIGHT_NATS_PORT

set +e
"$PROJECT_ROOT/scripts/run-antoine.sh" \
  --run-dir "$RUN_DIR" --flight-seconds 120 --settle-seconds 10 --keep \
  --waypoints '0,0,10;12,8,6;12,8,0.15;0,0,0.15' \
  >"$REPORT_ROOT/$RUN_ID-launcher.log" 2>&1
LAUNCHER_STATUS=$?
set -e

if [[ -f "$RUN_DIR/gz-flight-proof-report.json" ]]; then
  set +e
  if [[ ! -f "$RUN_DIR/research-provenance.json" ]]; then
    echo "Postflight provenance is missing; baseline qualification will fail closed." >&2
  fi
  uv run --no-project python "$PROJECT_ROOT/research/summarize_antoine_baseline.py" \
    "$RUN_DIR" --launcher-exit "$LAUNCHER_STATUS"
  SUMMARY_STATUS=$?
  set -e
else
  SUMMARY_STATUS=2
  echo "No flight proof report was produced; baseline is inconclusive." >&2
fi

printf 'baseline run: %s\n' "$RUN_DIR"
printf 'launcher exit: %s\n' "$LAUNCHER_STATUS"
cat "$REPORT_ROOT/$RUN_ID-launcher.log"
[[ "$LAUNCHER_STATUS" -eq 0 && "$SUMMARY_STATUS" -eq 0 ]]
