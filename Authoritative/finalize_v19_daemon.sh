#!/bin/bash
# Daemon: waits for all 3 v19 launchers to exit OR detects OpenAI quota exhaustion.
# Then runs export script to generate bank CSV + source distribution figure.
#
# Trigger conditions:
#   1. All 3 launcher PIDs exited cleanly
#   2. OpenAI quota exhausted (sustained 429/quota_exceeded errors across workers)
# Whichever comes first.

ROOT=/Users/Shreyas2/Desktop/Berkeley/occupation_task/v2_pipeline
LOG=$ROOT/output/finalize_v19_daemon.log
PY=/usr/local/bin/python3

V19_LAUNCHER=21808
SUPP_LAUNCHER=45775
RECOVERY_LAUNCHER=49292

V19_RUN=$ROOT/output/diversity50_20260430_195134
SUPP_RUN=$ROOT/output/v19_supplement_20260501_054536
RECOVERY_RUN=$ROOT/output/v19_recovery_20260501_062158

cd "$ROOT"

echo "[$(date)] finalize_v19_daemon started" >> "$LOG"
echo "  watching launchers: v19=$V19_LAUNCHER supp=$SUPP_LAUNCHER recovery=$RECOVERY_LAUNCHER" >> "$LOG"

QUOTA_HIT=false

while true; do
    v19_alive=false; supp_alive=false; rec_alive=false
    ps -p $V19_LAUNCHER > /dev/null 2>&1 && v19_alive=true
    ps -p $SUPP_LAUNCHER > /dev/null 2>&1 && supp_alive=true
    ps -p $RECOVERY_LAUNCHER > /dev/null 2>&1 && rec_alive=true

    if ! $v19_alive && ! $supp_alive && ! $rec_alive; then
        echo "[$(date)] All 3 launchers exited → trigger finalize" >> "$LOG"
        break
    fi

    # Check for OpenAI quota exhaustion across recent worker stdout
    quota_signals=$(find "$V19_RUN" "$SUPP_RUN" "$RECOVERY_RUN" -name "worker_*.stdout" -mmin -3 2>/dev/null \
        | xargs grep -l "RateLimitError\|insufficient_quota\|429.*OpenAI\|quota_exceeded" 2>/dev/null | wc -l | tr -d ' ')

    if [ "$quota_signals" -gt 5 ]; then
        echo "[$(date)] OpenAI quota signals detected in $quota_signals worker logs → trigger finalize" >> "$LOG"
        QUOTA_HIT=true
        break
    fi

    sleep 60
done

# Determine which run to export from (v19 base bank has all merged items)
echo "[$(date)] Running export script..." >> "$LOG"

# Concatenate any unmerged supplement/recovery items into v19 base bank first
# (in case daemon triggered before launchers finished their merge step)
"$PY" -u <<EOF >> "$LOG" 2>&1
import json
from pathlib import Path

V19 = Path("$V19_RUN")
SUPP = Path("$SUPP_RUN")
RECOVERY = Path("$RECOVERY_RUN")

base_items = V19 / "items.jsonl"
seen_eids = set()
all_items = []
if base_items.exists():
    for line in base_items.open():
        try:
            r = json.loads(line)
            eid = r.get("evidence_id")
            if eid not in seen_eids:
                seen_eids.add(eid)
                all_items.append(r)
        except: pass

# Pull in any worker items from supplement + recovery not yet merged
for run_dir in [SUPP, RECOVERY, V19]:
    for wi in run_dir.glob("worker_*/items.jsonl"):
        for line in wi.open():
            try:
                r = json.loads(line)
                eid = r.get("evidence_id")
                if eid and eid not in seen_eids:
                    seen_eids.add(eid)
                    all_items.append(r)
            except: pass

# Write merged bank
with base_items.open("w") as f:
    for r in all_items:
        f.write(json.dumps(r) + "\n")
print(f"Merged bank: {len(all_items)} unique items written to {base_items}")
EOF

# Run the export script
"$PY" -u "$ROOT/export_v19_final.py" >> "$LOG" 2>&1

echo "[$(date)] Daemon complete. See $V19_RUN for outputs." >> "$LOG"
echo "  - v19_final_bank_min5.csv" >> "$LOG"
echo "  - source_distribution_v19_final.png" >> "$LOG"
echo "  - source_distribution_v19_final.pdf" >> "$LOG"
echo "  - source_distribution_v19_final.json" >> "$LOG"
