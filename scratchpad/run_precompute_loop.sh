#!/bin/bash
# Auto-resuming wrapper around `valleespyr valley catchments precompute`.
# pysheds' grid.catchment() can hit an unrecoverable native crash (see
# METHOD.md "Native crash on sparse tiles") that kills the whole process;
# results are saved incrementally, so just re-launch until a run finishes
# cleanly (exit 0) or we give up after too many crashes in a row.
set -u
cd /home/etienne/projets/valleespyr

MAX_ATTEMPTS=40
attempt=0
while [ "$attempt" -lt "$MAX_ATTEMPTS" ]; do
  attempt=$((attempt + 1))
  echo "=== attempt $attempt/$MAX_ATTEMPTS ($(date -Iseconds)) ==="
  uv run valleespyr valley catchments precompute COURDEAU0000002000894629 \
    --from-file data/raw/troncon_hydrographique_pyrenees.parquet \
    --bassins data/raw/bassin_versant_topographique_pyrenees.parquet \
    --dem-dir data/raw/dem \
    --dem-source s3 \
    -o data/processed/catchments.json
  code=$?
  echo "=== attempt $attempt exited with code $code ==="
  if [ "$code" -eq 0 ]; then
    echo "=== run completed cleanly, stopping loop ==="
    exit 0
  fi
  sleep 2
done
echo "=== gave up after $MAX_ATTEMPTS attempts ==="
exit 1
