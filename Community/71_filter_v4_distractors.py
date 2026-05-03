"""
Step 71: Post-hoc quality filter on v4 bank (243 items).

Two checks per item:
  (1) DIRECT-ANSWER check: the CORRECT option must directly answer the
      specific question posed in the scenario.
  (2) UNIQUENESS check: among A-D, ONLY the designated-correct option
      should read as plausibly correct. If the designated-correct letter
      is E ("All of the above") then ALL four of A-D must be plausible.
      If it is F ("None of the above") then NONE of A-D should be
      plausible.

Drop an item if either check fails.

Output:
  output/qa_auto_source_v4_filtered.json   (clean subset)
  output/v4_distractor_audit.csv           (per-item, per-option verdicts)
  output/v4_filter_summary.txt
"""
import os, json, re, time
from pathlib import Path
import pandas as pd
from openai import OpenAI

ROOT = Path(__file__).parent
OUT = ROOT / "output"

oa_key = (ROOT / "api_key.txt").read_text().strip()
oa = OpenAI(api_key=oa_key)
JUDGE_MODEL = "gpt-4o"

BANK = OUT / "qa_auto_source_v4.json.keep"
FILTERED = OUT / "qa_auto_source_v4_filtered.json"
AUDIT = OUT / "v4_distractor_audit.csv"
SUMMARY = OUT / "v4_filter_summary.txt"
CKPT = OUT / "v4_filter_ckpt.json"


DIRECT_ANSWER_PROMPT = """You are evaluating a multiple-choice question for a benchmark.
The scenario ends with a SPECIFIC question the asker wants answered (e.g.,
"Should I X?", "How do I Y?", "What should I do?"). Verify that the CORRECT
option directly and clearly answers that specific question — not a tangential,
generic, or adjacent topic.

Decide:

- "DIRECT"      : the option gives a clear, direct answer to the specific
                  question asked.
- "TANGENTIAL"  : the option is related background / commentary / advice
                  about the general situation but does NOT cleanly answer
                  the specific question. Also TANGENTIAL if the option
                  contradicts the scenario's framing (e.g., scenario asks
                  "how can I stay and navigate this" but the option says
                  "leave the field").

Return EXACTLY one token: DIRECT or TANGENTIAL, then a short reason on the
next line.

SCENARIO (ends with a specific question):
{scenario}

CORRECT OPTION:
{correct_text}
"""


UNIQUENESS_PROMPT = """You are evaluating a multiple-choice question for a benchmark.
The scenario asks a specific question. Below are four candidate responses
labelled A, B, C, D. Your job: decide which of them a knowledgeable
practitioner would consider a PLAUSIBLY-CORRECT answer to the specific
question asked.

A response is PLAUSIBLY-CORRECT if it is on-topic, factually defensible,
and a reasonable answer to the question — even if not the single best one.
A response is NOT plausible if it is factually wrong, unsafe, misleading,
off-topic, or clearly worse than another option.

For a well-formed benchmark item, EXACTLY ONE of A/B/C/D should be
plausibly correct. If two or more seem plausible, or if none do, the item
is unusable.

Return a single line with the plausible letters, comma-separated, no spaces
(e.g., "B" or "A,C" or "NONE"). Then a short reason on the next line.

SCENARIO:
{scenario}

A) {a}

B) {b}

C) {c}

D) {d}
"""


def _call_judge(prompt, max_tokens=180):
    for attempt in range(3):
        try:
            r = oa.chat.completions.create(
                model=JUDGE_MODEL, temperature=0.0, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return (r.choices[0].message.content or "").strip(), ""
        except Exception as e:
            if attempt == 2:
                return "", f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))


def judge_direct_answer(scenario, correct_text):
    txt, err = _call_judge(
        DIRECT_ANSWER_PROMPT.format(scenario=scenario, correct_text=correct_text))
    if err: return "ERROR", err
    verdict = "UNCLEAR"
    if re.search(r"^\s*DIRECT\b", txt, re.I): verdict = "DIRECT"
    elif re.search(r"^\s*TANGENTIAL\b", txt, re.I): verdict = "TANGENTIAL"
    reason = txt.split("\n", 1)[1].strip() if "\n" in txt else txt
    return verdict, reason


def judge_uniqueness(scenario, a, b, c, d):
    txt, err = _call_judge(
        UNIQUENESS_PROMPT.format(scenario=scenario, a=a, b=b, c=c, d=d))
    if err: return set(), "ERROR", err
    first_line = txt.split("\n", 1)[0].strip().upper()
    reason = txt.split("\n", 1)[1].strip() if "\n" in txt else ""
    if "NONE" in first_line:
        return set(), "OK", reason
    letters = set(re.findall(r"[ABCD]", first_line))
    return letters, "OK", reason


def main():
    bank = json.loads(BANK.read_text())
    print(f"Loaded {len(bank)} v4 items", flush=True)

    ckpt = {}
    if CKPT.exists():
        try:
            ckpt = json.loads(CKPT.read_text())
            print(f"Resume: {len(ckpt)} items already judged", flush=True)
        except Exception:
            ckpt = {}

    audit_rows = []
    kept_items = []
    for i, q in enumerate(bank):
        key = f"{q['occupation']}||{q.get('source_url','')}"
        correct = q.get("correct_answer", "")
        opts = q.get("options", {})
        correct_text = opts.get(correct, "")
        scenario = q["question"]

        if key in ckpt and "uniqueness" in ckpt[key]:
            item_result = ckpt[key]
        else:
            item_result = {}
            # (1) Direct-answer check — only meaningful if correct is A-D
            if correct in ("A", "B", "C", "D"):
                da_verdict, da_reason = judge_direct_answer(scenario, correct_text)
            else:
                da_verdict, da_reason = "N/A", "correct is E or F"
            item_result["direct_answer"] = {
                "verdict": da_verdict, "reason": da_reason,
                "correct_text": correct_text,
            }
            time.sleep(0.15)
            # (2) Uniqueness check on A-D (always run)
            if all(k in opts for k in ("A", "B", "C", "D")):
                plausible, status, u_reason = judge_uniqueness(
                    scenario, opts["A"], opts["B"], opts["C"], opts["D"])
                item_result["uniqueness"] = {
                    "plausible_letters": sorted(plausible),
                    "status": status, "reason": u_reason,
                }
            else:
                item_result["uniqueness"] = {
                    "plausible_letters": [], "status": "MISSING",
                    "reason": "A/B/C/D not all present",
                }
            time.sleep(0.15)
            ckpt[key] = item_result
            if (i + 1) % 5 == 0:
                CKPT.write_text(json.dumps(ckpt, indent=2))

        da = item_result["direct_answer"]
        u = item_result["uniqueness"]
        plausible = set(u.get("plausible_letters", []))

        # Decide keep/drop
        drop_reason = ""
        if u.get("status") in ("ERROR", "MISSING"):
            drop_reason = "uniqueness_error"
        elif correct in ("A", "B", "C", "D"):
            if da["verdict"] != "DIRECT":
                drop_reason = f"correct_{da['verdict']}"
            elif plausible != {correct}:
                drop_reason = f"uniqueness_fail_plausible={','.join(sorted(plausible)) or 'NONE'}"
        elif correct == "E":
            # All of the above → all four A-D must be plausible
            if plausible != {"A", "B", "C", "D"}:
                drop_reason = f"E_requires_all_plausible_got={','.join(sorted(plausible)) or 'NONE'}"
        elif correct == "F":
            # None of the above → no A-D should be plausible
            if plausible:
                drop_reason = f"F_requires_none_plausible_got={','.join(sorted(plausible))}"
        else:
            drop_reason = f"unknown_correct_letter={correct}"

        keep = drop_reason == ""

        audit_rows.append({
            "occupation": q["occupation"],
            "source_url": q.get("source_url", ""),
            "correct_letter": correct,
            "direct_answer_verdict": da["verdict"],
            "direct_answer_reason": da["reason"][:300],
            "plausible_letters": ",".join(sorted(plausible)),
            "uniqueness_reason": u.get("reason", "")[:300],
            "kept": keep,
            "drop_reason": drop_reason,
        })

        if keep:
            kept_items.append(q)

        if (i + 1) % 10 == 0 or i + 1 == len(bank):
            print(f"  [{i+1}/{len(bank)}] kept so far: {len(kept_items)}", flush=True)

    CKPT.write_text(json.dumps(ckpt, indent=2))
    pd.DataFrame(audit_rows).to_csv(AUDIT, index=False)
    FILTERED.write_text(json.dumps(kept_items, indent=2))

    # Summary
    lines = []
    lines.append(f"Input:   {len(bank)} v4 items")
    lines.append(f"Kept:    {len(kept_items)}  ({len(kept_items)/len(bank)*100:.1f}%)")
    lines.append(f"Dropped: {len(bank) - len(kept_items)}")

    df = pd.DataFrame(audit_rows)
    lines.append("\nDrop reasons:")
    for v, n in df["drop_reason"].value_counts().items():
        tag = v if v else "kept"
        lines.append(f"  {tag:55s} {n}")

    lines.append("\nDirect-answer verdict (when correct is A-D):")
    for v, n in df["direct_answer_verdict"].value_counts().items():
        lines.append(f"  {v:12s} {n}")

    lines.append("\nPlausible-letter counts (how many of A-D were judged plausible):")
    df["n_plausible"] = df["plausible_letters"].apply(
        lambda s: 0 if not s else len(s.split(",")))
    for v, n in df["n_plausible"].value_counts().sort_index().items():
        lines.append(f"  {v:>3}  letters plausible  -> {n} items")

    lines.append("\nPer-occupation (kept / total):")
    occs_bank = {q["occupation"]: 0 for q in bank}
    for q in bank: occs_bank[q["occupation"]] += 1
    occs_kept = {q["occupation"]: 0 for q in bank}
    for q in kept_items: occs_kept[q["occupation"]] = occs_kept.get(q["occupation"], 0) + 1
    for occ in sorted(occs_bank):
        lines.append(f"  {occ[:55]:55s}  {occs_kept.get(occ,0):3d} / {occs_bank[occ]:3d}")

    summary = "\n".join(lines)
    SUMMARY.write_text(summary)
    print("\n" + summary)


if __name__ == "__main__":
    main()
