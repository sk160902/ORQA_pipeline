"""V19 supplement launcher: re-discover STEM + healthcare SOCs with the new
PMC efetch fix, IEEE iel5/iel7 filter, and Crossref+Unpaywall integration.

Filters pilot20_v2_chunk.csv to SOC major groups 15/17/19/29/31 (where the
academic-source fixes actually deliver new content), runs 32 workers against
a fresh run directory, then merges the supplement items into the v19 bank.

Usage:
  python3 launch_supplement.py --base-run diversity50_20260430_195134 [--workers 32] [--target 12]
"""
from __future__ import annotations
import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = "/usr/local/bin/python3"

# SOC major groups where Crossref+Unpaywall, PMC efetch, and arXiv add value.
# Excludes admin/sales/service SOCs where academic literature is sparse.
ELIGIBLE_GROUPS = frozenset({
    "15",  # Computer & Mathematical
    "17",  # Architecture & Engineering
    "19",  # Life, Physical, & Social Science
    "25",  # Educational Instruction & Library (some academic literature exists)
    "29",  # Healthcare Practitioners & Technical
    "31",  # Healthcare Support
})


def split_round_robin(items, n):
    chunks = [[] for _ in range(n)]
    for i, x in enumerate(items):
        chunks[i % n].append(x)
    return chunks


def write_chunk_csv(path: Path, rows):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["occupation_title", "soc_code"])
        for occ, soc in rows:
            w.writerow([occ, soc])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-run", required=True,
                    help="Existing v19 run name to merge supplement items into")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--target", type=int, default=12,
                    help="Lower target than primary run since this is supplement work")
    ap.add_argument("--run-name", default=None)
    args = ap.parse_args()

    base_run_dir = ROOT / "output" / args.base_run
    if not base_run_dir.exists():
        print(f"ERROR: base run dir does not exist: {base_run_dir}")
        sys.exit(1)

    name = args.run_name or f"v19_supplement_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir = ROOT / "output" / name
    run_dir.mkdir(parents=True, exist_ok=True)

    # Load and filter primary chunk
    primary_csv = ROOT / "output" / "pilot20_v2_chunk.csv"
    if not primary_csv.exists():
        print(f"ERROR: missing {primary_csv}")
        sys.exit(1)
    occupations = []
    with primary_csv.open() as f:
        for r in csv.DictReader(f):
            soc = r["soc_code"]
            grp = soc.split("-")[0] if "-" in soc else soc[:2]
            if grp in ELIGIBLE_GROUPS:
                occupations.append((r["occupation_title"], soc))
    print(f"Filtered to {len(occupations)} STEM+healthcare SOCs (from {primary_csv.name})")

    if not occupations:
        print("No eligible SOCs found.")
        sys.exit(1)

    chunks = split_round_robin(occupations, args.workers)
    chunks = [c for c in chunks if c]
    print(f"Split into {len(chunks)} worker chunks: {[len(c) for c in chunks]}")

    for i, chunk in enumerate(chunks):
        write_chunk_csv(run_dir / f"chunk_{i}.csv", chunk)

    # Launch workers
    pids = []
    for i in range(len(chunks)):
        worker_dir = f"{name}/worker_{i}"
        stdout_path = run_dir / f"worker_{i}.stdout"
        cmd = [
            PY, "-u", str(ROOT / "run_pilot20.py"),
            "--chunk-file", str(run_dir / f"chunk_{i}.csv"),
            "--target", str(args.target),
            "--skip-replacement",
            "--run-name", worker_dir,
        ]
        with stdout_path.open("w") as out:
            p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT))
        pids.append(p)
        print(f"  worker {i}: PID {p.pid}  ({len(chunks[i])} SOCs)  → {worker_dir}/")
        time.sleep(2)

    print(f"\nAll {len(pids)} supplement workers launched.")
    print(f"Monitor: tail -f {run_dir}/worker_*.stdout")
    print(f"Will merge into base run: {args.base_run}")
    for i, p in enumerate(pids):
        rc = p.wait()
        print(f"  worker {i}: exited rc={rc}")

    # Merge supplement items into base v19 bank
    print("\n=== Merging supplement into base run ===")
    base_items = base_run_dir / "items.jsonl"
    base_cards = base_run_dir / "cards.jsonl"

    n_supplement_items = 0
    n_supplement_cards = 0
    seen_evidence_ids = set()
    if base_items.exists():
        for line in base_items.open():
            try:
                r = json.loads(line)
                seen_evidence_ids.add(r.get("evidence_id"))
            except Exception:
                continue
        print(f"  base run has {len(seen_evidence_ids)} existing items")

    # Append supplement items (skip dups by evidence_id)
    with base_items.open("a") as items_out:
        for i in range(len(chunks)):
            wi = run_dir / f"worker_{i}" / "items.jsonl"
            if not wi.exists():
                continue
            for line in wi.open():
                try:
                    r = json.loads(line)
                    eid = r.get("evidence_id")
                    if eid in seen_evidence_ids:
                        continue
                    items_out.write(line)
                    seen_evidence_ids.add(eid)
                    n_supplement_items += 1
                except Exception:
                    continue

    if base_cards.exists():
        with base_cards.open("a") as cards_out:
            for i in range(len(chunks)):
                wc = run_dir / f"worker_{i}" / "cards.jsonl"
                if not wc.exists():
                    continue
                for line in wc.open():
                    cards_out.write(line)
                    n_supplement_cards += 1

    print(f"  appended {n_supplement_items} new items + {n_supplement_cards} new cards to {args.base_run}")
    print(f"\nSupplement run dir: {run_dir}")
    print(f"Base bank: {base_items}")


if __name__ == "__main__":
    main()
