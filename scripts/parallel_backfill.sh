#!/usr/bin/env bash
# GovInfo CREC backfill: 2 workers over BALANCED disjoint halves of the remaining
# span so both stay busy until the end (wA finishing early would waste key capacity).
# Combined request rate kept under the key's ~36k/hr ceiling; patient retries ride
# out throttling. On-disk file check dedupes already-downloaded granules.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
INTERVAL="${INTERVAL:-0.25}"

RANGES=(
  "2020-08-01 2023-12-31 wA"
  "2024-01-01 2026-07-05 wB"
)

run_worker() {
  local start="$1" end="$2" label="$3"
  local manifest="data/manifest_${label}.jsonl"
  local log="logs/worker_${label}.log"
  local attempt=0
  while true; do
    attempt=$((attempt+1))
    echo "[$label] attempt $attempt: $start..$end $(date)" >> "$log"
    $PY fetch_crec.py --start "$start" --end "$end" \
        --min-interval "$INTERVAL" --manifest "$manifest" >> "$log" 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then echo "[$label] DONE rc=0 $(date)" >> "$log"; break; fi
    echo "[$label] exited rc=$rc; retry in 120s $(date)" >> "$log"
    sleep 120
  done
}

mkdir -p logs
for r in "${RANGES[@]}"; do
  set -- $r
  run_worker "$1" "$2" "$3" &
  echo "launched worker $3 ($1..$2) pid $!"
  sleep 2
done
wait
echo "ALL_WORKERS_DONE $(date)"
