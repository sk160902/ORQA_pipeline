"""
Step 102: Full 15-model evaluation on the expanded 1,445-item validated bank.

Providers:
  OpenAI:    gpt-5.4, gpt-5.3-chat-latest, gpt-5.2, o3, gpt-4o, gpt-4o-mini, gpt-3.5-turbo
  Anthropic: claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4.5
  Together:  meta-llama/Llama-3.3-70B-Instruct-Turbo,
             deepseek-ai/DeepSeek-V3,
             Qwen/Qwen2.5-7B-Instruct-Turbo
  Google:    gemini-2.5-pro, gemini-1.5-flash

Each (model, item) is evaluated with 3 seeds so we can compute standard errors
across the full seed×item observation set as discussed 16-Apr (Abhishek:
"if it's 141×3 observations per bar, we can compute SE across those").

Output CSV: model, occupation, correct_answer, model_answer, is_correct,
            source_url, seed, pipeline_stage, source_tier

Usage:
  python3 102_eval_15models.py --model gpt-4o --out /tmp/gpt-4o.csv
  (launcher 103 shards across models)
"""
import argparse, json, os, re, time, random
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).parent
OUT = ROOT / "output"

# API keys
OAI_KEY = (ROOT / "api_key.txt").read_text().strip()
ANT_KEY = (ROOT / "api_key_anthropic.txt").read_text().strip()
TOG_KEY = (ROOT / "api_key_together.txt").read_text().strip()
GEM_KEY = (ROOT / os.environ.get("GEMINI_KEY_FILE", "api_key_gemini.txt")).read_text().strip()

# Canonical model catalogue. Keys are user-facing labels; values are
# (provider, api_model_id).
MODEL_CATALOGUE = {
    # OpenAI
    "gpt-5.4":              ("openai", "gpt-5.4"),
    "gpt-5.3-chat-latest":  ("openai", "gpt-5.3-chat-latest"),
    "gpt-5.2":              ("openai", "gpt-5.2"),
    "o3":                   ("openai", "o3"),
    "gpt-4o":               ("openai", "gpt-4o"),
    "gpt-4o-mini":          ("openai", "gpt-4o-mini"),
    "gpt-3.5-turbo":        ("openai", "gpt-3.5-turbo"),
    # Anthropic
    "claude-opus-4-6":      ("anthropic", "claude-opus-4-6"),
    "claude-sonnet-4-6":    ("anthropic", "claude-sonnet-4-6"),
    "claude-haiku-4.5":     ("anthropic", "claude-haiku-4-5-20251001"),
    # Together (open-source)
    "llama-3.3-70B":        ("together", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "DeepSeek-V3":          ("together", "deepseek-ai/DeepSeek-V3"),
    "Qwen2.5-7B-Instruct-Turbo": ("together", "Qwen/Qwen2.5-7B-Instruct-Turbo"),
    # Google
    "gemini-3.1-pro":       ("google", "gemini-3.1-pro-preview"),
    "gemini-2.5-pro":       ("google", "gemini-2.5-pro"),
    "gemini-2.5-flash":     ("google", "gemini-2.5-flash"),
}

N_SEEDS = 3
MAX_RETRIES = 3

PROMPT = """Answer the following multiple-choice question. Reply with ONLY the letter of the correct answer.

Question: {question}

{option_lines}

Your answer (single letter only):"""


# ───────── API clients ─────────

def _openai_client():
    from openai import OpenAI
    return OpenAI(api_key=OAI_KEY)

def _anthropic_client():
    import anthropic
    return anthropic.Anthropic(api_key=ANT_KEY)

def _together_client():
    from openai import OpenAI
    return OpenAI(api_key=TOG_KEY, base_url="https://api.together.xyz/v1")

def _gemini_client():
    from google import genai
    return genai.Client(api_key=GEM_KEY)


def call_openai(client, api_id, prompt, seed):
    is_reasoning = api_id.startswith(("o1", "o3", "o4"))
    # Some newer chat models (e.g. gpt-5.3-chat-latest) only accept the default
    # temperature=1. Try with temperature=0 for determinism, fall back if the
    # model rejects it.
    kwargs = dict(model=api_id, messages=[{"role": "user", "content": prompt}])
    if is_reasoning:
        kwargs["max_completion_tokens"] = 2000
    else:
        kwargs["max_completion_tokens"] = 150
        kwargs["temperature"] = 0.0
        kwargs["seed"] = seed
    try:
        r = client.chat.completions.create(**kwargs)
    except Exception as e:
        msg = str(e)
        if "temperature" in msg and "does not support" in msg:
            kwargs.pop("temperature", None)
            # Vary seed instead for replicability; keep it if allowed
            r = client.chat.completions.create(**kwargs)
        else:
            raise
    return (r.choices[0].message.content or "").strip()


def call_anthropic(client, api_id, prompt, seed):
    # Anthropic doesn't accept a seed arg; temperature=0 is deterministic-ish.
    # To introduce seed variance we vary temperature slightly across seeds.
    temps = [0.0, 0.2, 0.4]
    r = client.messages.create(
        model=api_id,
        max_tokens=200,
        temperature=temps[seed % len(temps)],
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()


def call_together(client, api_id, prompt, seed):
    r = client.chat.completions.create(
        model=api_id,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150,
        temperature=0.0,
        seed=seed,
    )
    return (r.choices[0].message.content or "").strip()


def call_gemini(client, api_id, prompt, seed):
    from google.genai import types
    temps = [0.0, 0.3, 0.5]
    # Gemini 2.5+/3.x Pro are reasoning models and spend most of their token
    # budget on hidden thinking; 150 max_output_tokens returns empty text.
    # Bump to 4000 for reasoning-capable Pro models, keep lower for Flash.
    max_tokens = 4000 if "pro" in api_id.lower() else 1000
    r = client.models.generate_content(
        model=api_id,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=temps[seed % len(temps)],
            max_output_tokens=max_tokens,
        ),
    )
    try:
        return (r.text or "").strip()
    except Exception:
        return ""


CALLERS = {
    "openai":    (_openai_client, call_openai),
    "anthropic": (_anthropic_client, call_anthropic),
    "together":  (_together_client, call_together),
    "google":    (_gemini_client, call_gemini),
}


# ───────── Answer extraction ─────────

def extract_letter(raw):
    if not raw: return ""
    up = raw.upper()
    # Prefer an explicit "answer: X" cue
    m = re.search(r"ANSWER[^A-F]*([A-F])", up)
    if m: return m.group(1)
    # Otherwise first standalone letter
    m = re.search(r"(?<![A-Z])([A-F])(?![A-Z])", up)
    return m.group(1) if m else ""


def format_options(q):
    return "\n".join(f"{k}) {v}" for k, v in sorted(q["options"].items()))


def score(model_letter, correct):
    return (model_letter or "") == correct


# ───────── Main eval loop ─────────

def run(model_label, items, seeds, out_csv, ckpt_path):
    provider, api_id = MODEL_CATALOGUE[model_label]
    client_factory, caller = CALLERS[provider]
    client = client_factory()

    # Resume from checkpoint
    done_keys = set()
    existing_rows = []
    if ckpt_path.exists():
        df = pd.read_csv(ckpt_path)
        existing_rows = df.to_dict("records")
        done_keys = {(r["source_url"], r["seed"]) for r in existing_rows}
        print(f"Resume: {len(done_keys)} already-scored (model, item, seed) rows", flush=True)

    rows = list(existing_rows)
    tot = len(items) * len(seeds)
    cnt = len(done_keys)

    for seed in seeds:
        for i, q in enumerate(items):
            key = (q.get("source_url", ""), seed)
            if key in done_keys:
                continue
            prompt = PROMPT.format(question=q["question"], option_lines=format_options(q))
            raw = ""
            for attempt in range(MAX_RETRIES):
                try:
                    raw = caller(client, api_id, prompt, seed)
                    break
                except Exception as e:
                    if attempt == MAX_RETRIES - 1:
                        raw = f"__ERR__:{type(e).__name__}"
                    else:
                        time.sleep(1.5 * (attempt + 1))
            letter = extract_letter(raw)
            rows.append({
                "model": model_label,
                "occupation": q["occupation"],
                "correct_answer": q["correct_answer"],
                "model_answer": letter,
                "is_correct": score(letter, q["correct_answer"]),
                "source_url": q.get("source_url", ""),
                "seed": seed,
                "pipeline_stage": q.get("pipeline_stage", ""),
                "source_tier": q.get("source_tier", ""),
            })
            cnt += 1

            # Periodic save
            if cnt % 50 == 0 or cnt == tot:
                pd.DataFrame(rows).to_csv(ckpt_path, index=False)
                print(f"[{model_label}] {cnt}/{tot}  last={letter or '?'}", flush=True)

    # Final save
    df = pd.DataFrame(rows)
    df.to_csv(ckpt_path, index=False)
    df.to_csv(out_csv, index=False)
    print(f"[{model_label}] DONE: {len(df)} rows", flush=True)

    correct = df["is_correct"].sum()
    total = len(df)
    print(f"[{model_label}] accuracy: {correct}/{total} = {correct/total*100:.1f}%", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True, help="Label from MODEL_CATALOGUE")
    p.add_argument("--bank", default=str(OUT / "qa_auto_source_v4_expanded_final.json"))
    p.add_argument("--seeds", type=int, nargs="+", default=list(range(N_SEEDS)))
    p.add_argument("--out-csv", required=True)
    p.add_argument("--ckpt", required=True)
    p.add_argument("--limit", type=int, default=None, help="For smoke tests")
    p.add_argument("--shard", type=int, default=0, help="Shard index in [0, num-shards)")
    p.add_argument("--num-shards", type=int, default=1, help="Total shards; items[shard::num-shards] processed")
    args = p.parse_args()

    if args.model not in MODEL_CATALOGUE:
        raise SystemExit(f"unknown model {args.model}; pick from {list(MODEL_CATALOGUE)}")
    if not (0 <= args.shard < args.num_shards):
        raise SystemExit(f"--shard {args.shard} out of range for --num-shards {args.num_shards}")
    items = json.loads(Path(args.bank).read_text())
    if args.limit: items = items[:args.limit]
    if args.num_shards > 1:
        items = items[args.shard::args.num_shards]
        print(f"Shard {args.shard}/{args.num_shards}: {len(items)} items", flush=True)
    print(f"Evaluating {args.model} on {len(items)} items × {len(args.seeds)} seeds "
          f"= {len(items) * len(args.seeds)} calls", flush=True)
    run(args.model, items, args.seeds, Path(args.out_csv), Path(args.ckpt))


if __name__ == "__main__":
    main()
