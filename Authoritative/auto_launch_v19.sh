#!/bin/bash
# Self-contained chain: poll until whitelist build finishes, then launch v19.
# Survives any Bash tool timeout — runs as a true daemon via nohup.

WAIT_PID=9024
LOG=/Users/Shreyas2/Desktop/Berkeley/occupation_task/v2_pipeline/output/auto_launch_v19.log
ROOT=/Users/Shreyas2/Desktop/Berkeley/occupation_task/v2_pipeline

cd "$ROOT"

echo "[$(date)] auto_launch_v19 daemon started — polling PID $WAIT_PID" >> "$LOG"

# Poll every 30s until the whitelist build process exits
while ps -p "$WAIT_PID" > /dev/null 2>&1; do
    sleep 30
done

echo "[$(date)] Whitelist build PID $WAIT_PID exited — launching v19" >> "$LOG"

# Launch v19 (32 workers, target 20, replacement-trigger 15, floor 5)
nohup /usr/local/bin/python3 -u launch_diversity50.py \
    --workers 32 --target 20 --min-items 15 --floor 5 \
    > "$ROOT/output/wagebill500_v19_launcher.log" 2>&1 &

V19_PID=$!
echo "[$(date)] v19 launched: PID $V19_PID" >> "$LOG"

# Confirm launcher is alive after 10s
sleep 10
if ps -p "$V19_PID" > /dev/null 2>&1; then
    echo "[$(date)] v19 launcher confirmed alive after 10s" >> "$LOG"
else
    echo "[$(date)] WARNING: v19 launcher exited within 10s — check launcher log" >> "$LOG"
fi
