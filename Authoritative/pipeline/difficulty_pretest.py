"""Difficulty pretest pass (Pass 7).

Run 3 small/fast models closed-book on each candidate item. Use spread across
3 different providers so we hit 3 different rate pools and get genuine
disagreement signal:

  OpenAI    — gpt-4o-mini
  Together  — meta-llama/Llama-3.3-70B-Instruct-Turbo
  Gemini    — gemini-2.5-flash

Verdict:
  - Reject if all 3 models get it RIGHT (no signal — too easy / answer leakage)
  - Reject if all 3 models get it WRONG AND verifier confidence is low
    (likely broken — `low confidence` proxied as ambiguous OR Tier C)
  - Otherwise keep — this is the productive 1-2/3 zone
"""
from __future__ import annotations
import re

from . import clients
from . import schemas

EVAL_PROMPT = """Answer the following multiple-choice question. Reply with ONLY the letter of the correct answer.

Question: {question}

A) {a}
B) {b}
C) {c}
D) {d}
E) {e}
F) {f}

Your answer (single letter only):
"""

LETTER_RE = re.compile(r"\b([A-F])\b")


def _extract_letter(text: str) -> str:
    """Pull a single letter A-F from a model response. '' if none found."""
    if not text:
        return ""
    s = text.strip().upper()
    # Try first character
    if s and s[0] in "ABCDEF":
        return s[0]
    m = LETTER_RE.search(s)
    return m.group(1) if m else ""


def _ask(prompt: str, provider: str) -> str:
    if provider == "openai":
        txt, _ = clients.call_openai(prompt, model="gpt-4o-mini",
                                     temperature=0.0, max_tokens=8)
    elif provider == "together":
        txt, _ = clients.call_together(prompt, temperature=0.0, max_tokens=8)
    elif provider == "gemini":
        txt, _ = clients.call_gemini(prompt, model="gemini-2.5-flash",
                                     temperature=0.0, max_tokens=8)
    else:
        return ""
    return _extract_letter(txt)


def pretest(item: schemas.Item) -> tuple[dict, str]:
    """Returns (record, verdict).
      record: per-model parsed_answer + n_correct + verdict
      verdict: 'keep' | 'reject_too_easy' | 'reject_too_hard_low_conf'
    """
    opts = item.options
    prompt = EVAL_PROMPT.format(
        question=item.question,
        a=opts.get("A", ""), b=opts.get("B", ""),
        c=opts.get("C", ""), d=opts.get("D", ""),
        e=opts.get("E", "All of the above"),
        f=opts.get("F", "None of the above"),
    )
    answers = {
        "gpt-4o-mini": _ask(prompt, "openai"),
        "llama-3.3-70b": _ask(prompt, "together"),
        "gemini-2.5-flash": _ask(prompt, "gemini"),
    }
    correct = item.correct_answer
    n_correct = sum(1 for a in answers.values() if a == correct)

    ambiguous = bool(item.verification.get("ambiguous"))
    is_tier_c = (item.quality_tier == "C")
    low_confidence = ambiguous or is_tier_c

    if n_correct == 3:
        verdict = "reject_too_easy"
    elif n_correct == 0 and low_confidence:
        verdict = "reject_too_hard_low_conf"
    else:
        verdict = "keep"

    record = {
        "answers": answers,
        "n_correct": n_correct,
        "verdict": verdict,
    }
    return record, verdict
