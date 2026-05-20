"""
Step 103: Launch 15 parallel eval workers — one per model — against the
expanded 1,445-item validated bank with 3 seeds each. Per-model checkpointing
so any interruption resumes cleanly.
"""
import json, subprocess
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
EVAL = OUT / "eval_chunks"
EVAL.mkdir(parents=True, exist_ok=True)

PY = "/usr/local/bin/python3.13"
SCRIPT = ROOT / "102_eval_15models.py"
BANK = OUT / "qa_auto_source_v4_expanded_final.json"

MODELS = [
    "claude-opus-4-6",
    "gpt-5.3-chat-latest",
    "gpt-5.4",
    "o3",
    "gpt-5.2",
    "claude-sonnet-4-6",
    "gpt-4o",
    "claude-haiku-4.5",
    "llama-3.3-70B",
    "DeepSeek-V3",
    "gpt-4o-mini",
    "gpt-3.5-turbo",
    "Qwen2.5-7B-Instruct-Turbo",
    "gemini-3.1-pro",
    "gemini-2.5-flash",
]


def main():
    procs = []
    for m in MODELS:
        safe = m.replace("/", "_").replace(".", "_")
        out_csv = EVAL / f"eval_{safe}.csv"
        ckpt = EVAL / f"eval_{safe}_ckpt.csv"
        stdout = EVAL / f"eval_{safe}.stdout"
        cmd = [
            PY, str(SCRIPT),
            "--model", m,
            "--bank", str(BANK),
            "--out-csv", str(out_csv),
            "--ckpt", str(ckpt),
        ]
        f = open(stdout, "a", buffering=1)
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(ROOT))
        procs.append({"model": m, "pid": p.pid, "stdout": str(stdout)})
        print(f"  {m:30s}  pid={p.pid}  stdout={stdout.name}")

    (EVAL / "pids.json").write_text(json.dumps(procs, indent=2))
    print(f"\n{len(MODELS)} eval workers launched.")
    print(f"Monitor:  tail -f {EVAL}/eval_*.stdout | head")
    print("Stop all: pkill -f '102_eval_15models.py'")


if __name__ == "__main__":
    main()
