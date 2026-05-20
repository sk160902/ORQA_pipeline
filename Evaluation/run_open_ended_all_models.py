"""Generate open-ended answers for every question in the bank, across all 15 models.

For each item the model is shown only the question stem (no options, no source). Single
shot, temperature=0.0. Per-model checkpoints written to OUT_DIR every 25 items so the
run is resumable. Final merge writes one column per model into FINAL_CSV.
"""
from __future__ import annotations
import time, traceback, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import pandas as pd

ROOT = Path("/Users/Shreyas2/Desktop/Berkeley/occupation_task")
BANK = ROOT / "v2_pipeline/paper_results/bank_balanced_floor1_with_evals_20260505_101424.csv"
OUT_DIR = ROOT / "v2_pipeline/paper_results/open_ended_runs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
FINAL_CSV = ROOT / "v2_pipeline/paper_results/bank_balanced_with_open_ended_20260505.csv"

# CSV column prefix (matches existing seed columns) -> (provider, api id)
MODELS = {
    "DeepSeek_V3":               ("together",  "deepseek-ai/DeepSeek-V3"),
    "Qwen2_5_7B_Instruct_Turbo": ("together",  "Qwen/Qwen2.5-7B-Instruct-Turbo"),
    "claude_haiku_4_5":          ("anthropic", "claude-haiku-4-5-20251001"),
    "claude_opus_4_6":           ("anthropic", "claude-opus-4-6"),
    "claude_sonnet_4_6":         ("anthropic", "claude-sonnet-4-6"),
    "gemini_2_5_flash":          ("google",    "gemini-2.5-flash"),
    "gemini_3_1_pro":            ("google",    "gemini-3.1-pro-preview"),
    "gpt_3_5_turbo":             ("openai",    "gpt-3.5-turbo"),
    "gpt_4o":                    ("openai",    "gpt-4o"),
    "gpt_4o_mini":               ("openai",    "gpt-4o-mini"),
    "gpt_5_2":                   ("openai",    "gpt-5.2"),
    "gpt_5_3_chat_latest":       ("openai",    "gpt-5.3-chat-latest"),
    "gpt_5_4":                   ("openai",    "gpt-5.4"),
    "llama_3_3_70B":             ("together",  "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "o3":                        ("openai",    "o3"),
}

PROMPT = "Question: {q}\n\nProvide one specific approach. Use five lines or less."

OAI_KEY = (ROOT / "api_key.txt").read_text().strip()
_ant2 = ROOT / "api_key_anthropic_2.txt"
ANT_KEY = (_ant2 if _ant2.exists() else ROOT / "api_key_anthropic.txt").read_text().strip()
TOG_KEY = (ROOT / "api_key_together.txt").read_text().strip()
_gemf = ROOT / "api_key_gemini_final.txt"
GEM_KEY = (_gemf if _gemf.exists() else ROOT / "api_key_gemini.txt").read_text().strip()


def make_clients():
    from openai import OpenAI
    import anthropic
    from google import genai
    return {
        "openai":    OpenAI(api_key=OAI_KEY),
        "anthropic": anthropic.Anthropic(api_key=ANT_KEY),
        "together":  OpenAI(api_key=TOG_KEY, base_url="https://api.together.xyz/v1"),
        "google":    genai.Client(api_key=GEM_KEY),
    }


def call_openai(client, api_id, prompt):
    is_reasoning = api_id.startswith("o") or api_id.startswith("gpt-5")
    kw = dict(model=api_id, messages=[{"role": "user", "content": prompt}])
    if is_reasoning:
        kw["max_completion_tokens"] = 4000
    else:
        kw["max_tokens"] = 300
    r = client.chat.completions.create(**kw)
    return (r.choices[0].message.content or "").strip()


def call_anthropic(client, api_id, prompt):
    r = client.messages.create(
        model=api_id, max_tokens=300, temperature=0.0,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()


def call_together(client, api_id, prompt):
    r = client.chat.completions.create(
        model=api_id, messages=[{"role": "user", "content": prompt}],
        max_tokens=300, temperature=0.0, seed=0,
    )
    return (r.choices[0].message.content or "").strip()


def call_gemini(client, api_id, prompt):
    from google.genai import types
    max_tokens = 4000 if "pro" in api_id.lower() else 1000
    r = client.models.generate_content(
        model=api_id, contents=prompt,
        config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=max_tokens),
    )
    try:
        return (r.text or "").strip()
    except Exception:
        return ""


CALLERS = {
    "openai":    call_openai,
    "anthropic": call_anthropic,
    "together":  call_together,
    "google":    call_gemini,
}


def save_atomic(df: pd.DataFrame, path: Path):
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    tmp.replace(path)


# Per-provider concurrency caps (within a single model). Sized to stay well below
# typical per-account rate limits while saturating per-call latency.
INNER_WORKERS = {
    "openai":    8,   # tier 4+ RPM/TPM are very generous
    "anthropic": 6,
    "together":  8,
    "google":    4,   # gemini-pro is rate-limit sensitive (503s in smoke test)
}


def _do_one_call(caller, client, api_id, prompt):
    for attempt in range(3):
        try:
            return caller(client, api_id, prompt)
        except Exception as e:
            if attempt == 2:
                return f"__ERR__:{type(e).__name__}: {str(e)[:120]}"
            time.sleep(1.5 * (attempt + 1))


def run_one_model(col, provider, api_id, df, clients):
    out_path = OUT_DIR / f"{col}_open_ended.csv"
    answers: dict[int, str] = {}
    if out_path.exists():
        try:
            old = pd.read_csv(out_path)
            for _, r in old.iterrows():
                answers[int(r["row_id"])] = r["answer"]
        except Exception as e:
            print(f"[{col}] resume load failed ({e}); starting fresh", flush=True)
            answers = {}
        if answers:
            print(f"[{col}] resume from {len(answers)}/{len(df)}", flush=True)

    client = clients[provider]
    caller = CALLERS[provider]
    n_total = len(df)
    todo = [(i, PROMPT.format(q=row["Questions"]))
            for i, row in df.iterrows() if i not in answers]
    if not todo:
        print(f"[{col}] already complete: {len(answers)}/{n_total}", flush=True)
        return col

    inner_n = INNER_WORKERS.get(provider, 4)
    lock = threading.Lock()
    completed_since_save = [0]

    with ThreadPoolExecutor(max_workers=inner_n) as ex:
        futs = {ex.submit(_do_one_call, caller, client, api_id, p): i
                for i, p in todo}
        for f in as_completed(futs):
            i = futs[f]
            try:
                ans = f.result()
            except Exception as e:
                ans = f"__ERR__:{type(e).__name__}: {str(e)[:120]}"
            with lock:
                answers[i] = ans
                completed_since_save[0] += 1
                n_done = len(answers)
                if completed_since_save[0] >= 25 or n_done == n_total:
                    save_atomic(
                        pd.DataFrame(sorted(answers.items()),
                                     columns=["row_id", "answer"]),
                        out_path,
                    )
                    completed_since_save[0] = 0
                    print(f"[{col}] {n_done}/{n_total}", flush=True)

    save_atomic(
        pd.DataFrame(sorted(answers.items()), columns=["row_id", "answer"]),
        out_path,
    )
    print(f"[{col}] DONE", flush=True)
    return col


def merge():
    df = pd.read_csv(BANK).reset_index(drop=True)
    out = df.copy()
    for col in MODELS:
        p = OUT_DIR / f"{col}_open_ended.csv"
        if not p.exists():
            print(f"WARN: {col} CSV missing, column will be empty", flush=True)
            out[f"{col}_open_ended"] = pd.NA
            continue
        sub = pd.read_csv(p).set_index("row_id")["answer"]
        out[f"{col}_open_ended"] = out.index.map(sub)
    save_atomic(out, FINAL_CSV)
    print(f"Wrote {FINAL_CSV}\n  rows={len(out)}  cols={len(out.columns)}", flush=True)


def main():
    df = pd.read_csv(BANK).reset_index(drop=True)
    print(f"Loaded {len(df)} items from bank", flush=True)
    clients = make_clients()

    # Fan out across all 15 models in parallel; each runs its own 933 calls sequentially.
    with ThreadPoolExecutor(max_workers=len(MODELS)) as ex:
        futures = {
            ex.submit(run_one_model, col, p, a, df, clients): col
            for col, (p, a) in MODELS.items()
        }
        for f in as_completed(futures):
            col = futures[f]
            try:
                f.result()
            except Exception as e:
                print(f"[FAIL {col}] {e}\n{traceback.format_exc()}", flush=True)

    merge()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "merge":
        merge()
    else:
        main()
