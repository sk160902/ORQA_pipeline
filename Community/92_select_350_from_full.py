"""
Step 92: Re-select 350 occupations from the full 923-occupation O*NET pool
(up from 878 in the prior selection). Keeps the existing 42 occupations,
fills the remaining 308 slots with SOC-diversity caps and
community-presence-friendly ranking.
"""
import json, re, csv
from pathlib import Path
from collections import defaultdict
import pandas as pd

ROOT = Path(__file__).parent
OUT = ROOT / "output"

TARGET_COUNT = 350
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

# Per-group caps (sums >350, selection stops at 350 using global rank)
PER_GROUP_TARGET = {
    "11": 25, "13": 25, "15": 20, "17": 20, "19": 20, "21": 14,
    "23": 8, "25": 30, "27": 18, "29": 35, "31": 12, "33": 10,
    "35": 10, "37": 8, "39": 14, "41": 20, "43": 25, "45": 10,
    "47": 22, "49": 20, "51": 30, "53": 22, "55": 5,
}


def jaccard(a, b):
    ta = set(re.findall(r"[a-z]{4,}", a.lower()))
    tb = set(re.findall(r"[a-z]{4,}", b.lower()))
    if not ta or not tb: return 0.0
    return len(ta & tb) / len(ta | tb)


def main():
    tasks_csv = OUT / "onet_tasks_parsed_full.csv"
    df = pd.read_csv(tasks_csv)
    df["major_group"] = df["onet_code"].astype(str).str[:2]

    occ_df = df.groupby(["occupation_title", "onet_code", "major_group"]).agg(
        n_tasks=("task_description", "nunique"),
        core_tasks=("task_type", lambda s: (s == "Core").sum()),
        task_text=("task_description", lambda s: " ".join(s.dropna().astype(str).tolist())[:4000]),
    ).reset_index()
    occ_df.rename(columns={"onet_code": "soc_6digit"}, inplace=True)
    print(f"Total unique O*NET occupations (with tasks): {len(occ_df)}")

    # Existing bank occupations
    existing_bank = json.load(open(OUT / "qa_auto_source_v4_final_validated_161.json"))
    existing_occs = {q["occupation"] for q in existing_bank}
    print(f"Existing occupations in bank: {len(existing_occs)}")

    # Load BLS employment ranks
    bls_emp = {}
    with open(ROOT / "67_select_scale_up_occupations.py") as f:
        src = f.read()
    for m in re.finditer(r'\(\s*"([0-9-]+)"\s*,\s*"([^"]+)"\s*,\s*(\d+)\s*\)', src):
        _soc, title, emp = m.group(1), m.group(2), int(m.group(3))
        bls_emp[title] = emp

    occ_df["employment"] = occ_df["occupation_title"].map(bls_emp).fillna(0).astype(int)
    # Prefer employment > core-task richness > total-task count
    occ_df["rank_score"] = (occ_df["employment"] * 10000
                            + occ_df["core_tasks"] * 100
                            + occ_df["n_tasks"])

    force_included = occ_df[occ_df["occupation_title"].isin(existing_occs)].copy()
    print(f"Force-including {len(force_included)} existing occupations "
          f"(missing from O*NET CSV: {existing_occs - set(occ_df['occupation_title'])})")

    remaining_pool = occ_df[~occ_df["occupation_title"].isin(existing_occs)].copy()

    selected = []
    per_group_count = defaultdict(int)
    selected_task_texts_by_group = defaultdict(list)

    # Force-include existing
    for _, r in force_included.iterrows():
        per_group_count[r["major_group"]] += 1
        selected.append({
            "occupation_title": r["occupation_title"],
            "soc_code": r["soc_6digit"],
            "major_group_code": r["major_group"],
            "major_group_name": SOC_MAJOR.get(r["major_group"], "Unknown"),
            "national_employment": int(r["employment"]),
            "n_tasks": int(r["n_tasks"]),
            "n_core_tasks": int(r["core_tasks"]),
            "selection_reason": "already_in_current_bank",
        })
        selected_task_texts_by_group[r["major_group"]].append(r["task_text"])

    # Fill remaining with rank score + SOC diversity + task-similarity dedup
    remaining_sorted = remaining_pool.sort_values("rank_score", ascending=False)
    for _, r in remaining_sorted.iterrows():
        if len(selected) >= TARGET_COUNT: break
        mg = r["major_group"]
        if per_group_count[mg] >= PER_GROUP_TARGET.get(mg, 10): continue
        task_text = r["task_text"]
        if any(jaccard(task_text, t) >= 0.55 for t in selected_task_texts_by_group[mg]):
            continue
        reason = "bls_employment_ranked" if r["employment"] > 0 else "onet_task_count_ranked"
        selected.append({
            "occupation_title": r["occupation_title"],
            "soc_code": r["soc_6digit"],
            "major_group_code": mg,
            "major_group_name": SOC_MAJOR.get(mg, "Unknown"),
            "national_employment": int(r["employment"]),
            "n_tasks": int(r["n_tasks"]),
            "n_core_tasks": int(r["core_tasks"]),
            "selection_reason": reason,
        })
        per_group_count[mg] += 1
        selected_task_texts_by_group[mg].append(task_text)

    print(f"\nSelected {len(selected)} occupations")
    for mg in sorted(per_group_count.keys()):
        print(f"  [{mg}] {SOC_MAJOR.get(mg,'?'):45s}  {per_group_count[mg]:3d}  (cap {PER_GROUP_TARGET.get(mg,0)})")

    out_csv = OUT / "scale_up_350_occupations_v2.csv"
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(selected[0].keys()))
        w.writeheader()
        for row in selected: w.writerow(row)
    print(f"\nWrote {out_csv}")

    existing_kept = sum(1 for s in selected if s["selection_reason"] == "already_in_current_bank")
    print(f"  Existing kept: {existing_kept}")
    print(f"  New added:     {len(selected) - existing_kept}")


if __name__ == "__main__":
    main()
