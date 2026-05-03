"""
Step 99: Launch the Pass 1 (advice-agreement) filter in parallel against the
merged 2,395-item expanded bank. Splits into N_WORKERS chunks, launches
98_advice_filter_parallel.py against each chunk with per-worker paths.

Pass 3 (tangential rescue, 77_fix_tangential_items.py) is run as a separate
follow-up step after Pass 1 finishes — it depends on the Pass 1 flagged list.
"""
import json, subprocess
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
FILTER = OUT / "filter_chunks"
FILTER.mkdir(parents=True, exist_ok=True)

N_WORKERS = 4
PY = "/usr/local/bin/python3.13"
SCRIPT = ROOT / "98_advice_filter_parallel.py"
BANK = OUT / "qa_auto_source_v4_expanded.json.keep"


def main():
    bank = json.loads(BANK.read_text())
    print(f"Input bank: {len(bank)} items, {len({i['occupation'] for i in bank})} occupations")

    # Split into N_WORKERS round-robin so each chunk has a mix of sources/occs
    for i in range(N_WORKERS):
        chunk = bank[i::N_WORKERS]
        chunk_path = FILTER / f"chunk_{i}_input.json"
        chunk_path.write_text(json.dumps(chunk, indent=2))
        print(f"  worker {i}: {len(chunk)} items -> {chunk_path.name}")

    procs = []
    for i in range(N_WORKERS):
        cmd = [
            PY, str(SCRIPT),
            "--bank", str(FILTER / f"chunk_{i}_input.json"),
            "--filtered", str(FILTER / f"chunk_{i}_strict.json"),
            "--audit", str(FILTER / f"chunk_{i}_audit.csv"),
            "--summary", str(FILTER / f"chunk_{i}_summary.txt"),
            "--ckpt", str(FILTER / f"chunk_{i}_ckpt.json"),
        ]
        stdout_path = FILTER / f"worker_{i}.stdout"
        f = open(stdout_path, "a", buffering=1)
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(ROOT))
        procs.append((i, p.pid, stdout_path))
        print(f"  worker {i}: pid={p.pid}  stdout={stdout_path}")

    (FILTER / "pids.json").write_text(json.dumps(
        [{"worker": i, "pid": pid, "stdout": str(sp)} for i, pid, sp in procs],
        indent=2,
    ))
    print("\nAll 4 Pass-1 workers launched.")
    print(f"Monitor:  tail -f {FILTER}/worker_*.stdout")


if __name__ == "__main__":
    main()
