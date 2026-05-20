"""Judge open-ended answers against correct MCQ options.

Two scores per item:
  1. is_correct: binary YES/NO from gpt-4o LLM judge
  2. similarity: cosine similarity (0-1) between OpenAI embeddings of
                 model_answer and correct_option_text
"""
from __future__ import annotations
import argparse, time
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path("/Users/Shreyas2/Desktop/Berkeley/occupation_task")
OAI_KEY = (ROOT / "api_key.txt").read_text().strip()

JUDGE_PROMPT = """You are evaluating whether a model's open-ended answer matches the correct answer for a multiple-choice question. The model was NOT shown the multiple-choice options; it produced a free-form answer.

Question: {question}

Correct answer (the MCQ option that was supposed to be picked): "{correct_text}"

Model's free-form answer: "{model_answer}"

Does the model's answer semantically match the correct answer? Match means: the model's answer expresses the same specific recommendation, procedure, tool, or fact as the correct answer (paraphrasing allowed). Mismatch means: the model's answer expresses a different recommendation, hedges with multiple options, or is vague where the correct answer is specific.

Respond with ONLY one word: YES or NO."""


def make_client():
    from openai import OpenAI
    return OpenAI(api_key=OAI_KEY)


def judge_one(client, question, correct_text, model_answer):
    question = "" if pd.isna(question) else str(question)
    correct_text = "" if pd.isna(correct_text) else str(correct_text)
    model_answer = "" if pd.isna(model_answer) else str(model_answer)
    if not model_answer.strip():
        return False
    prompt = JUDGE_PROMPT.format(question=question[:1500], correct_text=correct_text[:500],
                                 model_answer=model_answer[:1500])
    for attempt in range(3):
        try:
            r = client.chat.completions.create(
                model="gpt-4o", max_tokens=8, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            txt = (r.choices[0].message.content or "").strip().upper()
            if "YES" in txt: return True
            if "NO" in txt: return False
            return None
        except Exception:
            if attempt == 2: return None
            time.sleep(1.5 * (attempt + 1))
    return None


def embed_batch(client, texts, model="text-embedding-3-small"):
    """Embed a list of texts, returning list of np arrays. Coerces NaN/None."""
    safe = [("" if pd.isna(t) else str(t))[:8000] or " " for t in texts]
    try:
        r = client.embeddings.create(model=model, input=safe)
        return [np.array(e.embedding) for e in r.data]
    except Exception as e:
        # On failure return zero vectors
        return [np.zeros(1536) for _ in texts]


def cosine(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0: return 0.0
    return float(np.dot(a, b) / (na * nb))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--in-csv", required=True)
    p.add_argument("--out-csv", required=True)
    p.add_argument("--ckpt", required=True)
    args = p.parse_args()

    df = pd.read_csv(args.in_csv, low_memory=False)
    print(f"Loaded {len(df)} answers from {args.in_csv}", flush=True)

    ckpt_path = Path(args.ckpt)
    if ckpt_path.exists():
        try:
            old = pd.read_csv(ckpt_path, low_memory=False)
            if "is_correct" in old.columns and "similarity" in old.columns and len(old) == len(df):
                df = old
                done_idx = df["is_correct"].notna() & df["similarity"].notna()
                print(f"Resume: {done_idx.sum()} already judged+embedded", flush=True)
        except: pass

    client = make_client()
    if "is_correct" not in df.columns: df["is_correct"] = None
    if "similarity" not in df.columns: df["similarity"] = None

    # Step 1: LLM judge (per row, with periodic save)
    print("Step 1: LLM judge...", flush=True)
    cnt = 0
    for i, row in df.iterrows():
        if pd.notna(row.get("is_correct")): continue
        ma = row.get("model_answer", "")
        if pd.isna(ma) or not str(ma).strip() or str(ma).startswith("__ERR__"):
            df.at[i, "is_correct"] = False
        else:
            verdict = judge_one(client, row["question"], row["correct_option_text"], ma)
            df.at[i, "is_correct"] = verdict
        cnt += 1
        if cnt % 50 == 0:
            df.to_csv(ckpt_path, index=False)
            n_yes = (df["is_correct"] == True).sum()
            print(f"  judge {cnt}/{len(df)} (running yes-rate: {100*n_yes/cnt:.1f}%)", flush=True)
    df.to_csv(ckpt_path, index=False)

    # Step 2: Embedding similarity (in batches of 100 for speed)
    print("\nStep 2: Embedding similarity...", flush=True)
    BATCH = 100
    needs_sim = df[df["similarity"].isna() | (df["similarity"] == "")].index.tolist()
    for start in range(0, len(needs_sim), BATCH):
        idxs = needs_sim[start:start+BATCH]
        sub = df.loc[idxs]
        texts_a = sub["model_answer"].tolist()
        texts_b = sub["correct_option_text"].tolist()
        embs_a = embed_batch(client, texts_a)
        embs_b = embed_batch(client, texts_b)
        for j, idx in enumerate(idxs):
            ma = sub.loc[idx, "model_answer"]
            if pd.isna(ma) or not str(ma).strip() or str(ma).startswith("__ERR__"):
                df.at[idx, "similarity"] = 0.0
            else:
                df.at[idx, "similarity"] = cosine(embs_a[j], embs_b[j])
        if (start // BATCH) % 5 == 0:
            df.to_csv(ckpt_path, index=False)
            print(f"  embed {start+len(idxs)}/{len(needs_sim)}", flush=True)

    df.to_csv(ckpt_path, index=False)
    df.to_csv(args.out_csv, index=False)
    n_yes = (df["is_correct"] == True).sum()
    mean_sim = df["similarity"].astype(float).mean()
    print(f"\nDONE: {len(df)} rows | yes-rate: {100*n_yes/len(df):.1f}% | mean similarity: {mean_sim:.3f}", flush=True)


if __name__ == "__main__":
    main()
