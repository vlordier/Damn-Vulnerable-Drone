#!/usr/bin/env bash
# Revoke one active local test grant after a bounded delay.
set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORT_ROOT="${ANTOINE_REPORT_DIR:-$PROJECT_ROOT/build_logs/research/antoine-scenarios}"
mkdir -p "$REPORT_ROOT"
RUN_ID="antoine-early-revoke-$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$REPORT_ROOT/$RUN_ID"
[[ ! -e "$RUN_DIR" ]] || { echo "Refusing to overwrite evidence: $RUN_DIR" >&2; exit 1; }
if [[ -z "${ANTOINE_GZ_FLIGHT_NATS_PORT:-}" ]]; then
  ANTOINE_GZ_FLIGHT_NATS_PORT="$(uv run --no-project python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
fi
export ANTOINE_GZ_FLIGHT_NATS_PORT
set +e
"$PROJECT_ROOT/scripts/run-antoine.sh" --run-dir "$RUN_DIR" --flight-seconds 45 \
  --revoke-after-seconds 5 --settle-seconds 10 --keep \
  >"$REPORT_ROOT/$RUN_ID-launcher.log" 2>&1
LAUNCHER_STATUS=$?
set -e
if [[ -f "$RUN_DIR/gz-flight-proof-report.json" ]]; then
  if [[ ! -f "$RUN_DIR/research-provenance-comparison.json" ]]; then
    echo "Postflight provenance comparison is missing; revoke classification will fail closed." >&2
  fi
  uv run --no-project python "$PROJECT_ROOT/research/summarize_antoine_early_revoke.py" \
    "$RUN_DIR" --launcher-exit "$LAUNCHER_STATUS" || SUMMARY_STATUS=$?
else
  SUMMARY_STATUS=2
  echo "No flight report was produced; revoke evidence is inconclusive." >&2
fi
SUMMARY_STATUS="${SUMMARY_STATUS:-0}"
printf 'early revoke run: %s\n' "$RUN_DIR"
printf 'launcher exit: %s\n' "$LAUNCHER_STATUS"
cat "$REPORT_ROOT/$RUN_ID-launcher.log"
[[ "$SUMMARY_STATUS" -eq 0 ]]
