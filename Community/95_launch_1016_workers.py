"""
Step 95: Launch the full-scale 1016-occupation expansion.

Splits the has_tasks subset (923 occupations, minus those already at >=8 items
in v4_ckpt.json) into N_WORKERS chunks, writes per-worker CSVs, and launches
one background v4 worker per chunk. Each worker has its own checkpoint/bank/
coverage paths so they don't collide on writes. A 7th background worker runs
the authoritative-doc fallback against the 93 catch-all occupations.

Outputs:
  output/worker_chunks/chunk_{i}.csv          — per-worker input
  output/worker_chunks/worker_{i}_ckpt.json   — per-worker checkpoint
  output/worker_chunks/worker_{i}_bank.json   — per-worker raw bank
  output/worker_chunks/worker_{i}.log         — per-worker log
  output/worker_chunks/worker_{i}.stdout      — per-worker stdout/stderr
  output/worker_chunks/authdoc_catchall.stdout
"""
import json, subprocess, sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).parent
OUT = ROOT / "output"
CHUNKS = OUT / "worker_chunks"
CHUNKS.mkdir(parents=True, exist_ok=True)

N_COMMUNITY_WORKERS = 6
PY = "/usr/local/bin/python3.13"
V4_SCRIPT = ROOT / "71_auto_source_v7_alttitles.py"
AUTHDOC_SCRIPT = ROOT / "93_authoritative_doc_fallback.py"

SCALE_CSV = OUT / "scale_up_1016_occupations.csv"
V4_CKPT = OUT / "v4_ckpt.json"
TARGET_ITEMS = 8


def main():
    df = pd.read_csv(SCALE_CSV)
    has_tasks = df[df["has_tasks"]].copy()
    catchalls = df[~df["has_tasks"]].copy()
    print(f"Full taxonomy: {len(df)}  (has_tasks={len(has_tasks)}, catchalls={len(catchalls)})")

    # Current checkpoint state
    ckpt = json.loads(V4_CKPT.read_text()) if V4_CKPT.exists() else {}
    fully_done = {occ for occ, items in ckpt.items() if len(items) >= TARGET_ITEMS}
    print(f"Fully done in v4_ckpt: {len(fully_done)}")

    # Things to process: has_tasks occupations NOT yet fully done
    remaining = has_tasks[~has_tasks["occupation_title"].isin(fully_done)].copy()
    print(f"Community-worker queue: {len(remaining)} occupations")

    # Round-robin chunking so each worker gets a mix of SOC groups
    # (within SOC group, sort by employment desc so high-value occs run first)
    remaining = remaining.sort_values(
        ["major_group_code", "national_employment"],
        ascending=[True, False],
    ).reset_index(drop=True)
    remaining["worker"] = remaining.index % N_COMMUNITY_WORKERS

    for i in range(N_COMMUNITY_WORKERS):
        chunk = remaining[remaining["worker"] == i].drop(columns=["worker"])
        chunk_csv = CHUNKS / f"chunk_{i}.csv"
        chunk.to_csv(chunk_csv, index=False)
        print(f"  worker {i}: {len(chunk)} occupations -> {chunk_csv.name}")

    # Catch-alls CSV for auth-doc
    catchall_csv = CHUNKS / "catchalls.csv"
    catchalls.to_csv(catchall_csv, index=False)
    print(f"  authdoc (catchall): {len(catchalls)} -> {catchall_csv.name}")

    # Launch community workers
    print("\nLaunching community workers (background)...")
    procs = []
    for i in range(N_COMMUNITY_WORKERS):
        chunk_csv = CHUNKS / f"chunk_{i}.csv"
        stdout_path = CHUNKS / f"worker_{i}.stdout"
        cmd = [
            PY, str(V4_SCRIPT),
            "--from-csv", str(chunk_csv),
            "--ckpt-path", str(CHUNKS / f"worker_{i}_ckpt.json"),
            "--bank-path", str(CHUNKS / f"worker_{i}_bank.json"),
            "--coverage-path", str(CHUNKS / f"worker_{i}_coverage.csv"),
            "--log-path", str(CHUNKS / f"worker_{i}.log"),
        ]
        f = open(stdout_path, "a", buffering=1)
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(ROOT))
        procs.append((i, p, stdout_path))
        print(f"  worker {i}: pid={p.pid}  stdout={stdout_path}")

    # Launch auth-doc worker against catch-alls (independent; doesn't need
    # community results because these 93 have no community coverage by
    # definition — no O*NET tasks to search against).
    authdoc_stdout = CHUNKS / "authdoc_catchall.stdout"
    cmd = [PY, str(AUTHDOC_SCRIPT), "--from-csv", str(catchall_csv)]
    f = open(authdoc_stdout, "a", buffering=1)
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(ROOT))
    print(f"  authdoc: pid={p.pid}  stdout={authdoc_stdout}")

    # Write a pid file for reference
    pid_info = {
        "community": [{"worker": i, "pid": p.pid, "stdout": str(sp)} for i, p, sp in procs],
        "authdoc": {"pid": p.pid, "stdout": str(authdoc_stdout)},
    }
    (CHUNKS / "pids.json").write_text(json.dumps(pid_info, indent=2))

    print("\nAll workers launched. Monitor with:")
    print(f"  tail -f {CHUNKS}/worker_*.stdout")
    print(f"  python3.13 -c 'import json; d=json.load(open(\"{CHUNKS}/pids.json\")); print(d)'")
    print("\nStop a worker with:  kill <pid>")
    print("Stop all workers:    pkill -f '66_auto_source_v4_occupation.py'")


if __name__ == "__main__":
    main()
