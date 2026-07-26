#!/usr/bin/env bash
# Waits for the parallel backfill to finish, then merges manifests and rebuilds
# the GovInfo turns + metrics + charts so coverage extends through 2026.
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python
LOG=logs/finalize.log
echo "[finalize] watcher started $(date)" > "$LOG"

for i in $(seq 1 240); do   # up to ~20h (5-min polls)
  if grep -q ALL_WORKERS_DONE logs/parallel_backfill.log 2>/dev/null; then
    echo "[finalize] workers done; merging manifests $(date)" >> "$LOG"
    $PY scripts/merge_manifests.py >> "$LOG" 2>&1
    echo "[finalize] re-ingesting GovInfo + aggregate + viz $(date)" >> "$LOG"
    $PY -m analysis.run ingest-govinfo >> "$LOG" 2>&1
    $PY -m analysis.run aggregate >> "$LOG" 2>&1
    $PY -m analysis.run viz >> "$LOG" 2>&1
    echo "[finalize] FINALIZE_DONE $(date)" >> "$LOG"
    exit 0
  fi
  sleep 300
done
echo "[finalize] TIMEOUT waiting for workers $(date)" >> "$LOG"
