"""Recovery launcher: respawn fresh workers for SOCs damaged by Serper-dead state.

The supplement run had 12 workers that hit Serper HTTP 400 ("not enough credits")
and permanently marked the API key as exhausted in their process memory. Even
after the user topped up credits, those workers couldn't recover.

This script spawns fresh Python processes (with empty _EXHAUSTED_KEYS) targeting
the SOCs they failed on. Items written to a separate recovery worker dir, then
appended to the v19 base bank.

Usage:
  python3 launch_recovery.py
"""
from __future__ import annotations
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = "/usr/local/bin/python3"
N_WORKERS = 12
TARGET_ITEMS = 12

SUPP_DIR = ROOT / "output" / "v19_supplement_20260501_054536"
BASE_RUN_DIR = ROOT / "output" / "diversity50_20260430_195134"

RECOVERY_NAME = f"v19_recovery_{time.strftime('%Y%m%d_%H%M%S')}"
RECOVERY_DIR = ROOT / "output" / RECOVERY_NAME
RECOVERY_DIR.mkdir(parents=True, exist_ok=True)


def split_round_robin(items, n):
    chunks = [[] for _ in range(n)]
    for i, x in enumerate(items):
        chunks[i % n].append(x)
    return chunks


def main():
    # Load recovery SOCs
    recover_csv = SUPP_DIR / "recovery_chunk.csv"
    if not recover_csv.exists():
        print(f"ERROR: missing {recover_csv}")
        sys.exit(1)
    socs = []
    with recover_csv.open() as f:
        for r in csv.DictReader(f):
            socs.append((r["occupation_title"], r["soc_code"]))
    print(f"Loaded {len(socs)} SOCs to recover")

    chunks = split_round_robin(socs, N_WORKERS)
    chunks = [c for c in chunks if c]
    print(f"Split into {len(chunks)} worker chunks: {[len(c) for c in chunks]}")

    # Write chunk CSVs
    for i, chunk in enumerate(chunks):
        with (RECOVERY_DIR / f"chunk_{i}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["occupation_title", "soc_code"])
            for occ, soc in chunk:
                w.writerow([occ, soc])

    # Launch workers
    pids = []
    for i in range(len(chunks)):
        worker_dir = f"{RECOVERY_NAME}/worker_{i}"
        stdout_path = RECOVERY_DIR / f"worker_{i}.stdout"
        cmd = [
            PY, "-u", str(ROOT / "run_pilot20.py"),
            "--chunk-file", str(RECOVERY_DIR / f"chunk_{i}.csv"),
            "--target", str(TARGET_ITEMS),
            "--skip-replacement",
            "--run-name", worker_dir,
        ]
        with stdout_path.open("w") as out:
            p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT))
        pids.append(p)
        print(f"  worker {i}: PID {p.pid}  ({len(chunks[i])} SOCs)  → {worker_dir}/")
        time.sleep(1)

    print(f"\nAll {len(pids)} recovery workers launched.")
    print(f"Run dir: {RECOVERY_DIR}")
    print(f"Monitor: tail -f {RECOVERY_DIR}/worker_*.stdout")
    for i, p in enumerate(pids):
        rc = p.wait()
        print(f"  worker {i}: exited rc={rc}")

    # Merge recovery items into base v19 bank
    print("\n=== Merging recovery items into base v19 bank ===")
    base_items = BASE_RUN_DIR / "items.jsonl"
    base_cards = BASE_RUN_DIR / "cards.jsonl"

    seen_eids = set()
    if base_items.exists():
        for line in base_items.open():
            try:
                seen_eids.add(json.loads(line).get("evidence_id"))
            except: pass
        print(f"  base bank has {len(seen_eids)} existing items")

    n_new_items = 0
    n_new_cards = 0
    with base_items.open("a") as out:
        for i in range(len(chunks)):
            wi = RECOVERY_DIR / f"worker_{i}" / "items.jsonl"
            if not wi.exists(): continue
            for line in wi.open():
                try:
                    eid = json.loads(line).get("evidence_id")
                    if eid in seen_eids: continue
                    out.write(line)
                    seen_eids.add(eid)
                    n_new_items += 1
                except: continue

    if base_cards.exists():
        with base_cards.open("a") as out:
            for i in range(len(chunks)):
                wc = RECOVERY_DIR / f"worker_{i}" / "cards.jsonl"
                if not wc.exists(): continue
                for line in wc.open():
                    out.write(line)
                    n_new_cards += 1

    print(f"  appended {n_new_items} new items + {n_new_cards} new cards to v19 base")
    print(f"\nRecovery complete: {RECOVERY_DIR}")


if __name__ == "__main__":
    main()
