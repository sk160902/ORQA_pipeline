"""
Step 72: Advice-agreement filter on v4 bank (243 items).

Rule (from user):
  - The top-upvoted response is the CORRECT advice.
  - Any lower-upvoted response that says the same thing (even in less detail)
    is ALSO correct advice — it should NOT appear as a distractor.
  - Distractors must express DIFFERENT advice (or be clearly off-topic).

Per item:
  (1) DIRECT-ANSWER check: the CORRECT option must directly answer the
      scenario's specific question. (Carried over from step 71.)
  (2) ADVICE-AGREEMENT check: for each of A-D, classify as
        AGREES       — expresses the same recommendation as the correct
                       option (possibly shorter, less detailed, or a
                       paraphrase).
        CONTRADICTS  — expresses different advice, or a recommendation
                       a domain expert would view as wrong/unsafe/worse.
        OFF_TOPIC    — does not give advice on the question asked
                       (unrelated anecdote, tangent, digression).

Keep decision:
  - correct is A-D:
      keep as-is          if {AGREES} == {correct letter}, i.e. only the
                          designated letter agrees and the other three
                          contradict or are off-topic.
      relabel to E, keep  if AGREES == {A,B,C,D}, i.e. all four options
                          express the correct advice. Update correct_answer
                          to "E".
      drop                otherwise (redundant distractor or wrong correct).
  - correct is E:
      keep if AGREES == {A,B,C,D}; drop otherwise.
  - correct is F:
      keep if AGREES == {} (none of A-D give the right advice); drop
      otherwise.

Output:
  output/qa_auto_source_v4_strict.json     (clean subset, possibly relabelled)
  output/v4_strict_audit.csv               (per-item agreement set + verdict)
  output/v4_strict_summary.txt
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
FILTERED = OUT / "qa_auto_source_v4_strict.json"
AUDIT = OUT / "v4_strict_audit.csv"
SUMMARY = OUT / "v4_strict_summary.txt"
CKPT = OUT / "v4_strict_ckpt.json"


DIRECT_ANSWER_PROMPT = """You are evaluating a multiple-choice question for a benchmark.
The scenario ends with a SPECIFIC question the asker wants answered (e.g.,
"Should I X?", "How do I Y?"). Verify that the CORRECT option directly
and clearly answers that specific question.

Decide:
- "DIRECT"      : the option gives a clear, direct answer to the specific
                  question asked.
- "TANGENTIAL"  : the option is related commentary / advice about the
                  general situation but does NOT cleanly answer the
                  specific question, or contradicts the scenario's framing.

Return EXACTLY one token: DIRECT or TANGENTIAL, then a short reason.

SCENARIO:
{scenario}

CORRECT OPTION:
{correct_text}
"""


AGREEMENT_PROMPT = """You are evaluating a multiple-choice question for a benchmark.
The top-upvoted forum response was paraphrased into the option labelled
CORRECT below. The other three candidate options (from the same thread,
lower upvotes) are labelled 1, 2, and 3.

For each of 1, 2, 3, classify its core recommendation against the CORRECT
option's recommendation:

- "AGREES"       : it expresses the SAME recommendation as CORRECT — even
                   if shorter, less detailed, phrased differently, or
                   focuses on a subset. If a domain expert would say
                   "yes, that's basically the same advice," classify AGREES.
- "CONTRADICTS"  : it expresses DIFFERENT advice, or advice a domain
                   expert would view as wrong / worse / unsafe.
- "OFF_TOPIC"    : it doesn't give actionable advice on the scenario's
                   specific question (anecdote, tangent, unrelated commentary).

Be strict about AGREES: if two pieces of advice could both be posted as
valid answers to the question, they AGREE even if the wording differs.

Return EXACTLY three lines, one per option, in the format:
1: AGREES|CONTRADICTS|OFF_TOPIC  (short reason)
2: AGREES|CONTRADICTS|OFF_TOPIC  (short reason)
3: AGREES|CONTRADICTS|OFF_TOPIC  (short reason)

SCENARIO:
{scenario}

CORRECT option (top-upvoted paraphrase):
{correct_text}

OPTION 1:
{o1}

OPTION 2:
{o2}

OPTION 3:
{o3}
"""


def _call(prompt, max_tokens=260):
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


def judge_direct(scenario, correct_text):
    txt, err = _call(DIRECT_ANSWER_PROMPT.format(
        scenario=scenario, correct_text=correct_text), max_tokens=140)
    if err: return "ERROR", err
    if re.search(r"^\s*DIRECT\b", txt, re.I): return "DIRECT", txt
    if re.search(r"^\s*TANGENTIAL\b", txt, re.I): return "TANGENTIAL", txt
    return "UNCLEAR", txt


def judge_agreement(scenario, correct_text, others):
    # others is a list of 3 (letter, text) tuples, in fixed order slot 1/2/3
    txt, err = _call(AGREEMENT_PROMPT.format(
        scenario=scenario, correct_text=correct_text,
        o1=others[0][1], o2=others[1][1], o3=others[2][1]))
    if err: return {}, err
    labels = {}
    for line in txt.splitlines():
        m = re.match(r"\s*([123])\s*[:\.\)]\s*(AGREES|CONTRADICTS|OFF_TOPIC)\b",
                     line, re.I)
        if m:
            idx = int(m.group(1))
            labels[idx] = m.group(2).upper()
    # Map back to letters
    result = {}
    for i, (letter, _) in enumerate(others, start=1):
        result[letter] = labels.get(i, "UNCLEAR")
    return result, txt


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
    relabel_E_count = 0

    for i, q in enumerate(bank):
        key = f"{q['occupation']}||{q.get('source_url','')}"
        correct = q.get("correct_answer", "")
        opts = q.get("options", {})
        correct_text = opts.get(correct, "")
        scenario = q["question"]

        if key in ckpt and "agreement" in ckpt[key]:
            item_result = ckpt[key]
        else:
            item_result = {}
            # (1) Direct-answer check (if correct is A-D)
            if correct in ("A", "B", "C", "D"):
                da_v, da_raw = judge_direct(scenario, correct_text)
            else:
                da_v, da_raw = "N/A", ""
            item_result["direct_answer"] = {"verdict": da_v, "raw": da_raw[:300]}
            time.sleep(0.15)

            # (2) Agreement check — judge the three non-correct letters of A-D
            #     (even if correct is E or F, we still compare against the
            #     correct text; for E that's "All of the above" which is
            #     meaningless as correct_text, so we skip judging E cases
            #     unless correct_text is informative — guard below).
            other_letters = [L for L in ("A", "B", "C", "D") if L != correct and L in opts]
            if correct in ("A", "B", "C", "D") and len(other_letters) == 3 and da_v == "DIRECT":
                others = [(L, opts[L]) for L in other_letters]
                labels, raw = judge_agreement(scenario, correct_text, others)
                item_result["agreement"] = {"labels": labels, "raw": raw[:500]}
            else:
                item_result["agreement"] = {"labels": {}, "raw": "skipped"}
            time.sleep(0.15)

            ckpt[key] = item_result
            if (i + 1) % 5 == 0:
                CKPT.write_text(json.dumps(ckpt, indent=2))

        da = item_result["direct_answer"]
        agree = item_result["agreement"]
        labels = agree.get("labels", {})

        # Decide keep / relabel / drop
        drop_reason = ""
        relabeled = False
        new_correct = correct

        if correct in ("A", "B", "C", "D"):
            if da["verdict"] != "DIRECT":
                drop_reason = f"correct_{da['verdict']}"
            elif any(v == "UNCLEAR" or v == "ERROR" for v in labels.values()):
                drop_reason = "agreement_unclear"
            else:
                agrees_letters = {L for L, v in labels.items() if v == "AGREES"}
                if not agrees_letters:
                    # Only the correct letter expresses the correct advice —
                    # clean single-correct item.
                    pass  # keep as-is
                elif agrees_letters == set(labels.keys()):
                    # All three other letters also agree — means all four
                    # options express the correct advice. Relabel to E.
                    if "E" in opts:
                        new_correct = "E"
                        relabeled = True
                        relabel_E_count += 1
                    else:
                        drop_reason = "all_agree_but_no_E_option"
                else:
                    # Some (but not all) distractors are duplicates of correct
                    drop_reason = f"redundant_distractors={','.join(sorted(agrees_letters))}"
        elif correct == "E":
            # All four A-D must agree on same advice. We don't have a
            # "correct text" per se; skip for now and drop to be safe.
            drop_reason = "correct_is_E_unhandled"
        elif correct == "F":
            drop_reason = "correct_is_F_unhandled"
        else:
            drop_reason = f"unknown_correct={correct}"

        keep = drop_reason == ""

        audit_rows.append({
            "occupation": q["occupation"],
            "source_url": q.get("source_url", ""),
            "original_correct": correct,
            "new_correct": new_correct if keep else "",
            "direct_answer": da["verdict"],
            "agreement_A": labels.get("A", ""),
            "agreement_B": labels.get("B", ""),
            "agreement_C": labels.get("C", ""),
            "agreement_D": labels.get("D", ""),
            "kept": keep,
            "relabeled_to_E": relabeled,
            "drop_reason": drop_reason,
        })

        if keep:
            q_out = dict(q)
            if relabeled:
                q_out["correct_answer"] = new_correct
                q_out["relabeled_from"] = correct
            kept_items.append(q_out)

        if (i + 1) % 10 == 0 or i + 1 == len(bank):
            print(f"  [{i+1}/{len(bank)}] kept: {len(kept_items)} "
                  f"(relabeled E: {relabel_E_count})", flush=True)

    CKPT.write_text(json.dumps(ckpt, indent=2))
    pd.DataFrame(audit_rows).to_csv(AUDIT, index=False)
    FILTERED.write_text(json.dumps(kept_items, indent=2))

    # Summary
    lines = []
    lines.append(f"Input:          {len(bank)} v4 items")
    lines.append(f"Kept:           {len(kept_items)}  ({len(kept_items)/len(bank)*100:.1f}%)")
    lines.append(f"  of which relabeled to E: {relabel_E_count}")
    lines.append(f"Dropped:        {len(bank) - len(kept_items)}")

    df = pd.DataFrame(audit_rows)
    lines.append("\nDrop reasons:")
    for v, n in df["drop_reason"].value_counts().items():
        tag = v if v else "kept"
        lines.append(f"  {tag:55s} {n}")

    lines.append("\nAgreement-label distribution across distractors (all items with agreement run):")
    all_labels = []
    for col in ("agreement_A", "agreement_B", "agreement_C", "agreement_D"):
        all_labels.extend([x for x in df[col] if x])
    vc = pd.Series(all_labels).value_counts()
    for v, n in vc.items():
        lines.append(f"  {v:12s} {n}")

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
