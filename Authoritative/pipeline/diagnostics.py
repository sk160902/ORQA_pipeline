"""Diagnostics aggregator.

Reads the run's items.jsonl + cards.jsonl + stats.json and prints a
paper-ready summary table per occupation:

  - items kept (with tier A/B/C breakdown)
  - source publisher concentration (flag if 1 publisher > 50%)
  - cognitive type distribution
  - difficulty pretest distribution (n_correct)
  - answer-letter distribution (flag if any letter > 40%)
  - rejection reasons summary
  - occupation flags: under_5_items, single_publisher, only_tier_B_C

Also writes a CSV summary suitable for the paper appendix.
"""
from __future__ import annotations
import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

OCC_FLAGS = {
    "under_5_items": "fewer than 5 accepted items",
    "single_publisher_dominant": "one publisher supplies >50% of items",
    "only_tier_B_C": "no Tier A items",
    "answer_letter_imbalanced": "any answer letter >40%",
    "high_pretest_easy_rate": "more than 30% items rejected by pretest as too easy",
}


def load_run(run_dir: Path) -> tuple[list[dict], list[dict], dict]:
    items = []
    if (run_dir / "items.jsonl").exists():
        for line in (run_dir / "items.jsonl").open():
            line = line.strip()
            if line:
                items.append(json.loads(line))
    cards = []
    if (run_dir / "cards.jsonl").exists():
        for line in (run_dir / "cards.jsonl").open():
            line = line.strip()
            if line:
                cards.append(json.loads(line))
    stats = {}
    if (run_dir / "stats.json").exists():
        stats = json.loads((run_dir / "stats.json").read_text())
    return items, cards, stats


def per_occupation_summary(items: list[dict]) -> dict:
    """Aggregate items per occupation."""
    by_soc: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        by_soc[it["onet_soc_code"]].append(it)

    out = {}
    for soc, lst in by_soc.items():
        title = lst[0]["occupation_title"]
        tiers = Counter(it.get("quality_tier") for it in lst)
        publishers = Counter(it.get("publisher") or it.get("source_domain", "") for it in lst)
        documents = {it.get("source_url", "") for it in lst}  # unique source documents
        cog_types = Counter(it.get("cognitive_type") or "other" for it in lst)
        letters = Counter(it["correct_answer"] for it in lst)
        n_correct_dist = Counter(
            it.get("difficulty_pretest", {}).get("n_correct", "?") for it in lst
        )

        n = len(lst)
        flags = []
        if n < 5:
            flags.append("under_5_items")
        if publishers.most_common(1)[0][1] / n > 0.5 and n >= 4:
            flags.append("single_publisher_dominant")  # >50% from one publisher
        if len(publishers) < 2 and n >= 3:
            flags.append("only_one_publisher")
        if len(documents) < 2 and n >= 3:
            flags.append("only_one_source_document")
        if len(documents) < 3 and n >= 5:
            flags.append("under_three_source_documents")  # ≥3 source docs preferred
        if tiers.get("A", 0) == 0:
            flags.append("only_tier_B_C")
        if any(c / n > 0.4 for c in letters.values()) and n >= 5:
            flags.append("answer_letter_imbalanced")

        out[soc] = {
            "occupation_title": title,
            "n_items": n,
            "tier_A": tiers.get("A", 0),
            "tier_B": tiers.get("B", 0),
            "tier_C": tiers.get("C", 0),
            "publishers": dict(publishers.most_common()),
            "n_publishers": len(publishers),
            "n_source_documents": len(documents),
            "top_publisher_pct": (publishers.most_common(1)[0][1] / n) if n else 0.0,
            "cognitive_types": dict(cog_types),
            "answer_letter_dist": dict(letters),
            "pretest_n_correct_dist": {str(k): v for k, v in n_correct_dist.items()},
            "flags": flags,
        }
    return out


def overall_rollup(per_occ: dict, items: list[dict], stats: dict) -> dict:
    n = sum(s["n_items"] for s in per_occ.values())
    tier_totals = Counter()
    cog_totals = Counter()
    letter_totals = Counter()
    publisher_totals = Counter()
    for soc, s in per_occ.items():
        tier_totals["A"] += s["tier_A"]
        tier_totals["B"] += s["tier_B"]
        tier_totals["C"] += s["tier_C"]
        for k, v in s["cognitive_types"].items():
            cog_totals[k] += v
        for k, v in s["answer_letter_dist"].items():
            letter_totals[k] += v
        for k, v in s["publishers"].items():
            publisher_totals[k] += v

    pretest_easy_rejects = sum(
        v.get("pretest_rejects_easy", 0) for v in stats.values() if isinstance(v, dict)
    )
    pretest_hard_rejects = sum(
        v.get("pretest_rejects_hard", 0) for v in stats.values() if isinstance(v, dict)
    )
    verify_rejects = sum(
        v.get("verify_rejects", 0) for v in stats.values() if isinstance(v, dict)
    )

    return {
        "n_items": n,
        "n_occupations_with_items": len(per_occ),
        "tier_breakdown": dict(tier_totals),
        "cognitive_type_breakdown": dict(cog_totals),
        "answer_letter_breakdown": dict(letter_totals),
        "top_10_publishers": dict(publisher_totals.most_common(10)),
        "n_publishers_total": len(publisher_totals),
        "rejection_reasons": {
            "verify_rejects": verify_rejects,
            "pretest_too_easy": pretest_easy_rejects,
            "pretest_too_hard_low_conf": pretest_hard_rejects,
        },
    }


def print_summary(per_occ: dict, overall: dict, out=print):
    out("\n" + "=" * 80)
    out(f"OVERALL  ({overall['n_items']} items across {overall['n_occupations_with_items']} occupations)")
    out("=" * 80)
    t = overall["tier_breakdown"]
    out(f"  tier A: {t.get('A', 0):>4d}   tier B: {t.get('B', 0):>4d}   tier C: {t.get('C', 0):>4d}")
    out(f"  total publishers: {overall['n_publishers_total']}")
    out(f"  rejections — verify: {overall['rejection_reasons']['verify_rejects']}, "
        f"pretest_easy: {overall['rejection_reasons']['pretest_too_easy']}, "
        f"pretest_hard: {overall['rejection_reasons']['pretest_too_hard_low_conf']}")
    out("\n  Cognitive type distribution:")
    for k, v in sorted(overall["cognitive_type_breakdown"].items(), key=lambda kv: -kv[1]):
        out(f"    {k:<28s} {v:>4d}")
    out("\n  Answer-letter distribution (A-D should each be near 25%):")
    total_letters = sum(overall["answer_letter_breakdown"].values()) or 1
    for letter in "ABCD":
        c = overall["answer_letter_breakdown"].get(letter, 0)
        out(f"    {letter}: {c:>4d}  ({100*c/total_letters:>4.1f}%)")
    out("\n  Top 10 publishers:")
    for pub, c in list(overall["top_10_publishers"].items())[:10]:
        out(f"    {c:>4d}  {pub}")

    out("\n" + "=" * 80)
    out("PER-OCCUPATION SUMMARY")
    out("=" * 80)
    out(f"  {'SOC':<11s} {'n':>3s} {'A':>3s} {'B':>3s} {'C':>3s}  {'top-pub %':>9s}  {'flags':<35s}  occupation")
    for soc, s in sorted(per_occ.items(), key=lambda kv: -kv[1]["n_items"]):
        flags = ",".join(s["flags"]) or "-"
        out(f"  {soc:<11s} {s['n_items']:>3d} {s['tier_A']:>3d} {s['tier_B']:>3d} {s['tier_C']:>3d}"
            f"  {100*s['top_publisher_pct']:>8.1f}%  {flags:<35s}  {s['occupation_title'][:50]}")


def write_csvs(per_occ: dict, overall: dict, run_dir: Path):
    occ_csv = run_dir / "diagnostics_per_occupation.csv"
    with occ_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["soc", "occupation_title", "n_items", "tier_A", "tier_B", "tier_C",
                    "n_publishers", "top_publisher_pct", "flags"])
        for soc, s in sorted(per_occ.items()):
            w.writerow([soc, s["occupation_title"], s["n_items"],
                        s["tier_A"], s["tier_B"], s["tier_C"],
                        s["n_publishers"], f"{s['top_publisher_pct']:.3f}",
                        ",".join(s["flags"])])
    overall_json = run_dir / "diagnostics_overall.json"
    overall_json.write_text(json.dumps({"overall": overall, "per_occupation": per_occ}, indent=2))
    return occ_csv, overall_json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    args = ap.parse_args()
    run_dir = Path(args.run_dir)
    items, cards, stats = load_run(run_dir)
    per_occ = per_occupation_summary(items)
    overall = overall_rollup(per_occ, items, stats)
    print_summary(per_occ, overall)
    occ_csv, overall_json = write_csvs(per_occ, overall, run_dir)
    print(f"\nWrote {occ_csv}")
    print(f"Wrote {overall_json}")


if __name__ == "__main__":
    main()
