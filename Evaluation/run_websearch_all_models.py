"""Web-search-enabled MCQ evaluation across the 11 frontier models that have a
native web-search tool. Mirrors the closed-book MCQ format (question + 6 options)
but allows the model to call a web search during answering.

Models excluded: GPT-3.5-turbo, Llama 3.3 70B, DeepSeek V3, Qwen 2.5 7B
(no native web-search tool support).

Output: one column per model written into FINAL_CSV; per-model checkpoints in
OUT_DIR for resumability.

Usage:
    python3 run_websearch_all_models.py             # full run
    python3 run_websearch_all_models.py --smoke 20  # 20-item smoke test
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

ROOT = Path("/Users/Shreyas2/Desktop/Berkeley/occupation_task")
BANK = ROOT / "v2_pipeline/paper_results/bank_balanced_floor1_with_evals_20260505_101424.csv"
OUT_DIR = ROOT / "v2_pipeline/paper_results/websearch_runs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
FINAL_CSV = ROOT / "v2_pipeline/paper_results/websearch_results_20260507.csv"
COST_LOG = OUT_DIR / "cost_log.jsonl"

# CSV column prefix -> (provider, api id)
MODELS = {
    "claude_haiku_4_5_ws":     ("anthropic", "claude-haiku-4-5-20251001"),
    "claude_opus_4_6_ws":      ("anthropic", "claude-opus-4-6"),
    "claude_sonnet_4_6_ws":    ("anthropic", "claude-sonnet-4-6"),
    "gemini_2_5_flash_ws":     ("google",    "gemini-2.5-flash"),
    "gemini_3_1_pro_ws":       ("google",    "gemini-3.1-pro-preview"),
    "gpt_4o_ws":               ("openai",    "gpt-4o"),
    "gpt_4o_mini_ws":          ("openai",    "gpt-4o-mini"),
    "gpt_5_2_ws":              ("openai",    "gpt-5.2"),
    "gpt_5_3_chat_latest_ws":  ("openai",    "gpt-5.3-chat-latest"),
    "gpt_5_4_ws":              ("openai",    "gpt-5.4"),
    "o3_ws":                   ("openai",    "o3"),
}

# Per-provider parallel workers (tuned to each provider's typical rate limits with web search).
WORKERS = {
    "openai":    40,
    "anthropic": 25,
    "google":    25,
}

PROMPT_TEMPLATE = (
    "You are answering a single-best-answer multiple-choice question. "
    "You may call the web_search tool if you need authoritative information. "
    "After searching (if needed), respond with ONLY a single letter (A, B, C, D, E, or F) "
    "indicating the correct option. Do not explain.\n\n"
    "Question: {q}\n\n"
    "A) {A}\nB) {B}\nC) {C}\nD) {D}\nE) {E}\nF) {F}\n\n"
    "Answer with one letter only:"
)

OAI_KEY = (ROOT / "api_key.txt").read_text().strip()
_ant2 = ROOT / "api_key_anthropic_2.txt"
ANT_KEY = (_ant2 if _ant2.exists() else ROOT / "api_key_anthropic.txt").read_text().strip()
_gem2 = ROOT / "api_key_gemini_2.txt"
_gemf = ROOT / "api_key_gemini_final.txt"
GEM_KEY = (_gem2 if _gem2.exists() else _gemf if _gemf.exists() else ROOT / "api_key_gemini.txt").read_text().strip()


_clients = threading.local()
_cost_lock = threading.Lock()
_ckpt_locks: dict[str, threading.Lock] = {}
_ckpt_locks_lock = threading.Lock()


def get_ckpt_lock(col: str) -> threading.Lock:
    with _ckpt_locks_lock:
        lk = _ckpt_locks.get(col)
        if lk is None:
            lk = threading.Lock()
            _ckpt_locks[col] = lk
        return lk


def get_clients():
    if not hasattr(_clients, "openai"):
        from openai import OpenAI
        import anthropic
        from google import genai
        _clients.openai = OpenAI(api_key=OAI_KEY)
        _clients.anthropic = anthropic.Anthropic(api_key=ANT_KEY)
        _clients.google = genai.Client(api_key=GEM_KEY)
    return _clients


LETTER_RE = re.compile(r"\b([A-F])\b")


def extract_letter(text: str) -> str:
    if not text:
        return ""
    s = text.strip()
    # Most common: a single letter or "A." / "(A)"
    m = re.match(r"^[\s\(\[]*([A-F])[\.\)\]\s:]*", s)
    if m:
        return m.group(1)
    m = LETTER_RE.search(s)
    return m.group(1) if m else ""


def log_cost(model: str, provider: str, latency: float, tokens_in: int = 0, tokens_out: int = 0,
             search_calls: int = 0, raw_meta: dict | None = None):
    rec = dict(model=model, provider=provider, latency=latency,
               tokens_in=tokens_in, tokens_out=tokens_out, search_calls=search_calls,
               ts=time.time())
    if raw_meta:
        rec["meta"] = raw_meta
    with _cost_lock:
        with COST_LOG.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")


# ---------- OpenAI ----------

def call_openai(api_id: str, prompt: str) -> tuple[str, dict]:
    client = get_clients().openai
    is_reasoning = api_id.startswith("o") or api_id.startswith("gpt-5")
    kw = dict(
        model=api_id,
        input=prompt,
        tools=[{"type": "web_search"}],
    )
    if is_reasoning:
        kw["max_output_tokens"] = 4000
    else:
        kw["max_output_tokens"] = 400
    r = client.responses.create(**kw)
    text = (getattr(r, "output_text", "") or "").strip()
    meta = {}
    try:
        usage = getattr(r, "usage", None)
        if usage:
            meta["tokens_in"] = getattr(usage, "input_tokens", 0)
            meta["tokens_out"] = getattr(usage, "output_tokens", 0)
        # count web_search calls if visible
        n_search = 0
        for item in getattr(r, "output", []) or []:
            t = getattr(item, "type", None)
            if t and "web_search" in t:
                n_search += 1
        meta["search_calls"] = n_search
    except Exception:
        pass
    return text, meta


# ---------- Anthropic ----------

def call_anthropic(api_id: str, prompt: str) -> tuple[str, dict]:
    client = get_clients().anthropic
    r = client.messages.create(
        model=api_id,
        max_tokens=1024,
        temperature=0.0,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user", "content": prompt}],
    )
    text_parts, n_search = [], 0
    for b in r.content:
        bt = getattr(b, "type", "")
        if bt == "text":
            text_parts.append(b.text)
        elif "web_search" in bt:
            n_search += 1
    meta = {"search_calls": n_search}
    try:
        u = getattr(r, "usage", None)
        if u:
            meta["tokens_in"] = getattr(u, "input_tokens", 0)
            meta["tokens_out"] = getattr(u, "output_tokens", 0)
    except Exception:
        pass
    return "".join(text_parts).strip(), meta


# ---------- Google Gemini ----------

def call_google(api_id: str, prompt: str) -> tuple[str, dict]:
    client = get_clients().google
    from google.genai import types as gt
    cfg = gt.GenerateContentConfig(
        tools=[gt.Tool(google_search=gt.GoogleSearch())],
        temperature=0.0,
        max_output_tokens=2048,
    )
    r = client.models.generate_content(
        model=api_id, contents=prompt, config=cfg,
    )
    text = (getattr(r, "text", None) or "").strip()
    meta = {}
    try:
        usage = getattr(r, "usage_metadata", None)
        if usage:
            meta["tokens_in"] = getattr(usage, "prompt_token_count", 0)
            meta["tokens_out"] = getattr(usage, "candidates_token_count", 0)
    except Exception:
        pass
    # grounding metadata indicates search was used
    try:
        cands = getattr(r, "candidates", None) or []
        if cands:
            gm = getattr(cands[0], "grounding_metadata", None)
            if gm and getattr(gm, "web_search_queries", None):
                meta["search_calls"] = len(gm.web_search_queries)
    except Exception:
        pass
    return text, meta


PROVIDER_FN = {
    "openai":    call_openai,
    "anthropic": call_anthropic,
    "google":    call_google,
}


TRANSIENT_TOKENS = ("503", "UNAVAILABLE", "529", "overloaded", "rate_limit",
                    "RateLimitError", "Timeout", "timed out", "ServerError",
                    "InternalServerError", "502", "504", "ServiceUnavailable")


def is_transient(err: str) -> bool:
    return any(tok in err for tok in TRANSIENT_TOKENS)


def answer_one(provider: str, api_id: str, prompt: str, retries: int = 6) -> tuple[str, str, dict]:
    """Returns (raw_text, letter, meta). Retries with exponential backoff on transient errors."""
    fn = PROVIDER_FN[provider]
    last_err = ""
    for attempt in range(retries):
        try:
            t0 = time.time()
            text, meta = fn(api_id, prompt)
            meta["latency"] = round(time.time() - t0, 2)
            return text, extract_letter(text), meta
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            # Longer backoff on transient errors; short backoff otherwise.
            base = 4.0 if is_transient(last_err) else 1.5
            sleep_s = min(base * (2 ** attempt), 60.0)
            time.sleep(sleep_s)
    return f"__ERROR__ {last_err}", "", {"error": last_err}


# ---------- Per-model checkpoint persistence ----------

def ckpt_path(col: str) -> Path:
    return OUT_DIR / f"{col}.csv"


def load_ckpt(col: str) -> dict[int, dict]:
    p = ckpt_path(col)
    if not p.exists():
        return {}
    df = _read_ckpt_safely(p)
    out = {}
    for _, r in df.iterrows():
        # Skip cached error rows so they get retried this run.
        raw = str(r.get("raw", ""))
        if raw.startswith("__ERROR__") or not str(r.get("letter", "")).strip():
            continue
        out[int(r["row_id"])] = {"raw": raw, "letter": r["letter"]}
    return out


def append_ckpt(col: str, row_id: int, raw: str, letter: str, meta: dict):
    p = ckpt_path(col)
    rec = {"row_id": row_id, "raw": (raw or "")[:2000], "letter": letter,
           "search_calls": meta.get("search_calls", 0),
           "tokens_in": meta.get("tokens_in", 0),
           "tokens_out": meta.get("tokens_out", 0),
           "latency": meta.get("latency", 0)}
    lk = get_ckpt_lock(col)
    with lk:
        new = not p.exists()
        pd.DataFrame([rec]).to_csv(p, mode="a", header=new, index=False)


# ---------- Driver ----------

def run_model(col: str, provider: str, api_id: str, items: pd.DataFrame, max_workers: int):
    done = load_ckpt(col)
    pending = [(rid, row) for rid, row in items.iterrows() if rid not in done]
    print(f"[{col}] {len(done)} cached, {len(pending)} to do", flush=True)
    if not pending:
        return

    def task(rid_row):
        rid, row = rid_row
        prompt = PROMPT_TEMPLATE.format(
            q=row["Questions"],
            A=row["option_A"], B=row["option_B"], C=row["option_C"],
            D=row["option_D"], E=row["option_E"], F=row["option_F"],
        )
        raw, letter, meta = answer_one(provider, api_id, prompt)
        append_ckpt(col, rid, raw, letter, meta)
        log_cost(col, provider, meta.get("latency", 0),
                 meta.get("tokens_in", 0), meta.get("tokens_out", 0),
                 meta.get("search_calls", 0))
        return rid, letter, meta

    n_done, n_correct = 0, 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(task, rr) for rr in pending]
        for fut in as_completed(futs):
            try:
                rid, letter, meta = fut.result()
                n_done += 1
                if letter == items.loc[rid, "correct_answer"]:
                    n_correct += 1
                if n_done % 25 == 0:
                    print(f"[{col}] {n_done}/{len(pending)} acc-so-far={n_correct/max(n_done,1):.1%}", flush=True)
            except Exception as e:
                print(f"[{col}] ERROR {e}", flush=True)
    print(f"[{col}] DONE {n_done} new, acc-on-new={n_correct/max(n_done,1):.1%}", flush=True)


def _read_ckpt_safely(p: Path) -> pd.DataFrame:
    """Read a checkpoint csv, tolerating missing/duplicated headers from past races."""
    expected = ["row_id", "raw", "letter", "search_calls", "tokens_in", "tokens_out", "latency"]
    df = pd.read_csv(p, on_bad_lines="skip", dtype=str)
    if list(df.columns) != expected:
        # Header was eaten by a race; re-read with explicit names and drop
        # rows where row_id is the literal string "row_id" (header rows that ended up as data).
        df = pd.read_csv(p, header=None, names=expected, on_bad_lines="skip", dtype=str)
        df = df[df["row_id"] != "row_id"]
    df = df[pd.to_numeric(df["row_id"], errors="coerce").notna()].copy()
    df["row_id"] = df["row_id"].astype(int)
    return df.drop_duplicates("row_id", keep="last")


def merge_into_final(items: pd.DataFrame):
    """Write a standalone results CSV: row_id + question + correct_answer + per-model letters.

    Does NOT modify the original bank CSV. Web-search outputs stay in their own file.
    """
    out = pd.DataFrame({
        "row_id": items.index,
        "occupation": items["occupation"].values,
        "Questions": items["Questions"].values,
        "correct_answer": items["correct_answer"].values,
    })
    for col in MODELS:
        p = ckpt_path(col)
        if not p.exists():
            out[col] = ""
            continue
        df = _read_ckpt_safely(p)
        m = {int(r["row_id"]): r["letter"] for _, r in df.iterrows()}
        out[col] = out["row_id"].map(m).fillna("")
    out.to_csv(FINAL_CSV, index=False)
    print(f"Wrote {FINAL_CSV} with {len(MODELS)} model columns (separate from bank CSV)", flush=True)


def report_accuracy(items: pd.DataFrame):
    print("\n========= WEB-SEARCH ACCURACY =========")
    rows = []
    for col in MODELS:
        p = ckpt_path(col)
        if not p.exists():
            continue
        df = _read_ckpt_safely(p)
        # filter rows that are cached errors
        df = df[~df["raw"].astype(str).str.startswith("__ERROR__")]
        df = df.merge(items[["correct_answer"]], left_on="row_id", right_index=True)
        df["correct"] = df["letter"].astype(str) == df["correct_answer"].astype(str)
        sc = pd.to_numeric(df.get("search_calls"), errors="coerce")
        lat = pd.to_numeric(df.get("latency"), errors="coerce")
        tin = pd.to_numeric(df.get("tokens_in"), errors="coerce")
        tout = pd.to_numeric(df.get("tokens_out"), errors="coerce")
        rows.append({
            "model": col,
            "n": len(df),
            "acc_pct": round(100 * df["correct"].mean(), 2) if len(df) else 0,
            "mean_search_calls": round(sc.mean(), 2) if sc is not None and sc.notna().any() else float("nan"),
            "mean_tokens_in": round(tin.mean(), 1) if tin is not None and tin.notna().any() else float("nan"),
            "mean_tokens_out": round(tout.mean(), 1) if tout is not None and tout.notna().any() else float("nan"),
            "mean_latency_s": round(lat.mean(), 2) if lat is not None and lat.notna().any() else float("nan"),
        })
    res = pd.DataFrame(rows).sort_values("acc_pct", ascending=False)
    print(res.to_string(index=False))
    res.to_csv(OUT_DIR / "websearch_leaderboard.csv", index=False)
    print(f"Saved leaderboard to {OUT_DIR / 'websearch_leaderboard.csv'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", type=int, default=0,
                    help="Run smoke test on first N items only.")
    ap.add_argument("--only", type=str, default="",
                    help="Comma-separated subset of model column names to run.")
    args = ap.parse_args()

    items = pd.read_csv(BANK)
    items = items.reset_index(drop=True)
    if args.smoke > 0:
        items = items.head(args.smoke)
    print(f"Loaded {len(items)} items", flush=True)

    only = set(c.strip() for c in args.only.split(",") if c.strip())
    todo = [(col, prov, api_id) for col, (prov, api_id) in MODELS.items()
            if not only or col in only]

    # Group by provider so providers can run in parallel using their respective worker pools.
    by_provider: dict[str, list] = {}
    for col, prov, api_id in todo:
        by_provider.setdefault(prov, []).append((col, prov, api_id))

    threads = []
    for prov, lst in by_provider.items():
        def run_provider(lst=lst, prov=prov):
            for col, p, api_id in lst:
                run_model(col, p, api_id, items, WORKERS[prov])
        t = threading.Thread(target=run_provider, name=f"prov-{prov}", daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    merge_into_final(items)
    report_accuracy(items)


if __name__ == "__main__":
    main()
