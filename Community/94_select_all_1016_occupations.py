"""
Step 94: Build a superset occupation list covering ALL 1016 O*NET-SOC codes.

923 of the 1016 have task content in O*NET and are already in
onet_tasks_parsed_full.csv. The remaining 93 are "All Other" catch-all SOC
codes with zero published tasks — they're included here as placeholders
(has_tasks=False) for coverage completeness, but they cannot feed the v4
discovery/filtering pipeline because there are no O*NET tasks to ground
against.

Output: output/scale_up_1016_occupations.csv
"""
import json, re, csv
from pathlib import Path
from collections import defaultdict
import pandas as pd

ROOT = Path(__file__).parent
OUT = ROOT / "output"
DATA = ROOT / "data" / "db_29_1_text"

SOC_MAJOR = {
    "11": "Management", "13": "Business & Financial Operations",
    "15": "Computer & Mathematical", "17": "Architecture & Engineering",
    "19": "Life, Physical, & Social Science", "21": "Community & Social Service",
    "23": "Legal", "25": "Educational Instruction & Library",
    "27": "Arts, Design, Entertainment, Sports & Media",
    "29": "Healthcare Practitioners & Technical", "31": "Healthcare Support",
    "33": "Protective Service", "35": "Food Preparation & Serving",
    "37": "Building & Grounds Cleaning & Maintenance",
    "39": "Personal Care & Service", "41": "Sales & Related",
    "43": "Office & Administrative Support",
    "45": "Farming, Fishing, & Forestry", "47": "Construction & Extraction",
    "49": "Installation, Maintenance, & Repair",
    "51": "Production", "53": "Transportation & Material Moving",
    "55": "Military Specific",
}


def main():
    # Full taxonomy (all 1016)
    occ_full = pd.read_csv(DATA / "Occupation Data.txt", sep="\t")
    occ_full = occ_full.rename(columns={
        "O*NET-SOC Code": "onet_code",
        "Title": "occupation_title",
    })[["onet_code", "occupation_title"]]
    occ_full["major_group"] = occ_full["onet_code"].astype(str).str[:2]
    print(f"Full O*NET taxonomy: {len(occ_full)} occupations")

    # Task-level data for the 923 with published tasks
    tasks_df = pd.read_csv(OUT / "onet_tasks_parsed_full.csv")
    task_stats = tasks_df.groupby("onet_code").agg(
        n_tasks=("task_description", "nunique"),
        core_tasks=("task_type", lambda s: (s == "Core").sum()),
    ).reset_index()
    print(f"Occupations with task content: {len(task_stats)}")

    occ = occ_full.merge(task_stats, on="onet_code", how="left")
    occ["n_tasks"] = occ["n_tasks"].fillna(0).astype(int)
    occ["core_tasks"] = occ["core_tasks"].fillna(0).astype(int)
    occ["has_tasks"] = occ["n_tasks"] > 0

    # Existing bank occupations
    existing_bank = json.load(open(OUT / "qa_auto_source_v4_final_validated_161.json"))
    existing_occs = {q["occupation"] for q in existing_bank}
    print(f"Existing occupations in bank: {len(existing_occs)}")

    # Normalise a known title mismatch (bank uses 'Plumbers'; O*NET uses the
    # full SOC title). Map bank occupations to O*NET titles where needed.
    title_aliases = {
        "Plumbers": "Plumbers, Pipefitters, and Steamfitters",
    }
    normalised_existing = {title_aliases.get(t, t) for t in existing_occs}

    # BLS employment (from the earlier scale-up script's embedded list)
    bls_emp = {}
    with open(ROOT / "67_select_scale_up_occupations.py") as f:
        src = f.read()
    for m in re.finditer(r'\(\s*"([0-9-]+)"\s*,\s*"([^"]+)"\s*,\s*(\d+)\s*\)', src):
        _soc, title, emp = m.group(1), m.group(2), int(m.group(3))
        bls_emp[title] = emp
    occ["employment"] = occ["occupation_title"].map(bls_emp).fillna(0).astype(int)

    occ["rank_score"] = (occ["employment"] * 10000
                         + occ["core_tasks"] * 100
                         + occ["n_tasks"])

    def reason(row):
        if row["occupation_title"] in normalised_existing:
            return "already_in_current_bank"
        if not row["has_tasks"]:
            return "all_other_catchall_no_tasks"
        if row["employment"] > 0:
            return "bls_employment_ranked"
        return "onet_task_count_ranked"

    occ["selection_reason"] = occ.apply(reason, axis=1)

    # Sort: bank occupations first, then has-tasks by rank_score desc,
    # then catch-all no-task occupations last.
    reason_order = {
        "already_in_current_bank": 0,
        "bls_employment_ranked": 1,
        "onet_task_count_ranked": 1,
        "all_other_catchall_no_tasks": 2,
    }
    occ["_order"] = occ["selection_reason"].map(reason_order)
    occ = occ.sort_values(["_order", "rank_score"], ascending=[True, False])

    selected = [{
        "occupation_title": r["occupation_title"],
        "soc_code": r["onet_code"],
        "major_group_code": r["major_group"],
        "major_group_name": SOC_MAJOR.get(r["major_group"], "Unknown"),
        "national_employment": int(r["employment"]),
        "n_tasks": int(r["n_tasks"]),
        "n_core_tasks": int(r["core_tasks"]),
        "has_tasks": bool(r["has_tasks"]),
        "selection_reason": r["selection_reason"],
    } for _, r in occ.iterrows()]

    print(f"\nSelected {len(selected)} occupations")
    reasons = defaultdict(int)
    for s in selected: reasons[s["selection_reason"]] += 1
    for k, v in reasons.items():
        print(f"  {k}: {v}")

    per_group = defaultdict(lambda: [0, 0])  # [with_tasks, catchall]
    for s in selected:
        per_group[s["major_group_code"]][0 if s["has_tasks"] else 1] += 1
    print("\nPer-SOC-group coverage (with_tasks / catchall):")
    for mg in sorted(per_group.keys()):
        wt, ca = per_group[mg]
        print(f"  [{mg}] {SOC_MAJOR.get(mg,'?'):45s}  {wt:4d} / {ca:3d}")

    out_csv = OUT / "scale_up_1016_occupations.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(selected[0].keys()))
        w.writeheader()
        for row in selected: w.writerow(row)
    print(f"\nWrote {out_csv}")


if __name__ == "__main__":
    main()
