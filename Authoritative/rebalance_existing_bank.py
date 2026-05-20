"""Rebalance the existing 1,385-item authoritative bank by capping each SOC
major group, with round-robin stratification across occupations within a group.

Inputs (read-only):
  - ORQA_pipeline/Authoritative/outputs/final_bank.csv
  - output/scale_up_all_occupations.csv  (occupation -> SOC major group)

Outputs (written under v2_pipeline/output/rebalanced/):
  - rebalanced_bank_cap{N}.csv          subset of final_bank rows (all columns preserved)
  - rebalanced_bank_cap{N}_summary.csv  per-group / per-occupation counts after capping

Run from the repo root:
  python3 v2_pipeline/Authoritative_rebalance/rebalance_existing_bank.py
"""
from __future__ import annotations
import csv
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BANK = REPO / "ORQA_pipeline/Authoritative/outputs/final_bank.csv"
OCC_MAP = REPO / "output/scale_up_all_occupations.csv"
OUT_DIR = REPO / "v2_pipeline/output/rebalanced"

# Caps to evaluate. Smaller caps under-sample healthcare more aggressively.
CAPS = [30, 50, 75, 100]

# Manual patch for occupations missing from scale_up_all_occupations.csv.
# 'Special Education Teachers, All Other' is SOC 25-2059 (Education).
MANUAL_OCC_TO_GROUP = {
    "Special Education Teachers, All Other": ("25", "Educational Instruction & Library"),
}


def load_occ_to_group() -> dict[str, tuple[str, str]]:
    m: dict[str, tuple[str, str]] = {}
    with OCC_MAP.open() as f:
        for row in csv.DictReader(f):
            m[row["occupation_title"]] = (row["major_group_code"], row["major_group_name"])
    m.update(MANUAL_OCC_TO_GROUP)
    return m


def load_bank() -> tuple[list[str], list[dict]]:
    with BANK.open() as f:
        r = csv.DictReader(f)
        rows = list(r)
        return r.fieldnames, rows


def stratified_pick(rows_for_group: list[dict], cap: int) -> list[dict]:
    """Round-robin pick across occupations within a group until we hit `cap`.

    Preserves the original row order within each occupation (deterministic).
    Occupations with fewer items are exhausted first; the remaining cap is
    spread across occupations that still have items.
    """
    if len(rows_for_group) <= cap:
        return rows_for_group

    by_occ: dict[str, list[dict]] = defaultdict(list)
    for r in rows_for_group:
        by_occ[r["occupation"]].append(r)

    # Sort occupations by current size ascending so small ones aren't starved.
    occs = sorted(by_occ.keys(), key=lambda o: (len(by_occ[o]), o))
    cursors = {o: 0 for o in occs}
    picked: list[dict] = []

    while len(picked) < cap:
        progressed = False
        for o in occs:
            if cursors[o] < len(by_occ[o]):
                picked.append(by_occ[o][cursors[o]])
                cursors[o] += 1
                progressed = True
                if len(picked) >= cap:
                    break
        if not progressed:
            break  # everyone exhausted
    return picked


def rebalance(cap: int, occ_to_group: dict[str, tuple[str, str]],
              fieldnames: list[str], rows: list[dict]) -> tuple[list[dict], dict]:
    by_group: dict[str, list[dict]] = defaultdict(list)
    group_label: dict[str, str] = {}
    for r in rows:
        occ = r["occupation"]
        if occ not in occ_to_group:
            raise KeyError(f"unmapped occupation: {occ!r}")
        code, name = occ_to_group[occ]
        by_group[code].append(r)
        group_label[code] = f"{code} {name}"

    kept: list[dict] = []
    summary: dict[str, dict] = {}
    for code in sorted(by_group):
        group_rows = by_group[code]
        picked = stratified_pick(group_rows, cap)
        kept.extend(picked)
        per_occ_before = defaultdict(int)
        per_occ_after = defaultdict(int)
        for r in group_rows:
            per_occ_before[r["occupation"]] += 1
        for r in picked:
            per_occ_after[r["occupation"]] += 1
        summary[code] = {
            "label": group_label[code],
            "before": len(group_rows),
            "after": len(picked),
            "per_occ_before": dict(per_occ_before),
            "per_occ_after": dict(per_occ_after),
        }
    return kept, summary


def write_outputs(cap: int, kept: list[dict], summary: dict, fieldnames: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    bank_path = OUT_DIR / f"rebalanced_bank_cap{cap}.csv"
    with bank_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(kept)

    summary_path = OUT_DIR / f"rebalanced_bank_cap{cap}_summary.csv"
    with summary_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["soc_major_group", "occupation", "before", "after"])
        for code in sorted(summary, key=lambda c: -summary[c]["before"]):
            s = summary[code]
            w.writerow([s["label"], "<TOTAL>", s["before"], s["after"]])
            occs = sorted(set(s["per_occ_before"]) | set(s["per_occ_after"]),
                          key=lambda o: -s["per_occ_before"].get(o, 0))
            for o in occs:
                w.writerow([s["label"], o,
                            s["per_occ_before"].get(o, 0),
                            s["per_occ_after"].get(o, 0)])

    print(f"\ncap={cap}  ->  {len(kept)} items written to {bank_path.name}")
    print(f"  per-group:")
    for code in sorted(summary, key=lambda c: -summary[c]["before"]):
        s = summary[code]
        print(f"    {s['label']:50}  {s['before']:>4} -> {s['after']:>4}")


def main() -> None:
    occ_to_group = load_occ_to_group()
    fieldnames, rows = load_bank()
    print(f"loaded {len(rows)} rows from {BANK.relative_to(REPO)}")
    for cap in CAPS:
        kept, summary = rebalance(cap, occ_to_group, fieldnames, rows)
        write_outputs(cap, kept, summary, fieldnames)


if __name__ == "__main__":
    main()
