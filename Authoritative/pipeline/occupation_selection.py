"""Wage-bill weighted occupation selection.

Per Abhishek's call: weight by wage_bill = employment × annual_median_wage,
not just median wage. Stratify across SOC major groups. Pick top-X per group.

This module is built for the next scale-up beyond the pilot 20. The pilot
itself reuses the existing pilot 20 list (config.PILOT_20) so we get a
clean A/B comparison against the 294-item bank.

Inputs (when run for the first time):
  - oesm24nat/national_M2024_dl.xlsx  (download from BLS — see _bls_url())
  - O*NET tasks parsed CSV (already on disk)

Output:
  - output/v2_selected_occupations.csv with all metadata fields
"""
from __future__ import annotations
import csv
from collections import defaultdict
from pathlib import Path

import pandas as pd

from . import config

OEWS_XLSX = config.REPO_ROOT / "oesm24nat" / "national_M2024_dl.xlsx"

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


def _bls_url() -> str:
    return ("https://www.bls.gov/oes/special-requests/oesm24nat.zip "
            "(unzip oesm24nat/national_M2024_dl.xlsx into the repo root)")


def _coerce_num(v) -> float:
    """Handle BLS '*' / '#' / '**' codes for suppressed values."""
    if v is None:
        return float("nan")
    s = str(v).replace(",", "").strip()
    if s in ("", "*", "**", "#", "***"):
        return float("nan")
    try:
        return float(s)
    except Exception:
        return float("nan")


def load_oews() -> pd.DataFrame:
    """Returns a DataFrame with columns: soc_code, occupation_title, employment, annual_median_wage, annual_mean_wage, hourly_median_wage, wage_bill.

    Filters to OEWS 'detailed' occupations only (831 rows in 2024 release),
    excluding aggregate roll-ups (major / minor / broad / total).
    """
    if not OEWS_XLSX.exists():
        raise FileNotFoundError(
            f"OEWS file not found at {OEWS_XLSX}. Download from: {_bls_url()}"
        )
    # 2024 release: header on row 0, columns UPPERCASE.
    df = pd.read_excel(OEWS_XLSX, sheet_name=0)
    if "O_GROUP" in df.columns:
        df = df[df["O_GROUP"] == "detailed"].copy()
    # Normalize columns
    rename = {
        "OCC_CODE": "soc_code",
        "OCC_TITLE": "occupation_title",
        "TOT_EMP": "employment",
        "A_MEDIAN": "annual_median_wage",
        "A_MEAN": "annual_mean_wage",
        "H_MEDIAN": "hourly_median_wage",
    }
    df = df.rename(columns=rename)
    keep = ["soc_code", "occupation_title", "employment",
            "annual_median_wage", "annual_mean_wage", "hourly_median_wage"]
    keep = [c for c in keep if c in df.columns]
    df = df[keep].copy()
    for c in ("employment", "annual_median_wage", "annual_mean_wage", "hourly_median_wage"):
        if c in df.columns:
            df[c] = df[c].apply(_coerce_num)
    # Wage fallback: median → mean → hourly × 2080
    def _annual(row):
        m = row.get("annual_median_wage")
        if m == m and m > 0: return m
        n = row.get("annual_mean_wage")
        if n == n and n > 0: return n
        h = row.get("hourly_median_wage")
        if h == h and h > 0: return h * 2080.0
        return float("nan")
    df["annual_wage_used"] = df.apply(_annual, axis=1)
    df["wage_bill"] = df["employment"] * df["annual_wage_used"]
    return df


def select(top_per_group: int = 7, max_total: int = 150) -> list[dict]:
    """Stratified top-X-per-SOC-major-group by wage bill.

    With top_per_group=7 across 22 major groups, ~150 occupations total.
    Returns list of dicts with selection metadata.
    """
    df = load_oews()
    df["soc_major"] = df["soc_code"].astype(str).str[:2]
    df = df.dropna(subset=["wage_bill"])
    df = df[df["wage_bill"] > 0]
    df = df[df["soc_code"].astype(str).str.len() >= 7]

    selected = []
    for grp, sub in df.sort_values("wage_bill", ascending=False).groupby("soc_major"):
        for rank, row in enumerate(sub.head(top_per_group).itertuples(), start=1):
            selected.append({
                "onet_soc_code": row.soc_code,
                "occupation_title": row.occupation_title,
                "soc_major_group": grp,
                "soc_major_group_name": SOC_MAJOR.get(grp, "Unknown"),
                "national_employment": int(row.employment) if row.employment == row.employment else 0,
                "annual_wage_used": float(row.annual_wage_used) if row.annual_wage_used == row.annual_wage_used else 0.0,
                "occupation_wage_bill": float(row.wage_bill),
                "selection_rank_within_soc_group": rank,
                "selection_reason": "top_wage_bill_in_soc_major_group",
            })
    selected.sort(key=lambda r: r["occupation_wage_bill"], reverse=True)
    return selected[:max_total]


def select_proportional(*, target_total: int = 100, floor_per_group: int = 3,
                         cap_per_group: int | None = None,
                         reserves_per_group: int = 3) -> tuple[list[dict], dict[str, list[dict]]]:
    """Plan §3.2 proportional allocation (corrected).

    Allocates `target_total` slots across SOC major groups proportionally to
    each group's share of total wage_bill. Applies floor (≥3 per included
    group) and optional cap (no group dominates). Within each group, ranks
    occupations by wage_bill and selects top.

    Reserves are picked from the same group, ranked by wage_bill, NOT
    overlapping with primaries.

    Returns (primary_list, reserves_by_soc_major_group).
    """
    df = load_oews()
    df["soc_major"] = df["soc_code"].astype(str).str[:2]
    df = df.dropna(subset=["wage_bill"])
    df = df[df["wage_bill"] > 0]
    df = df[df["soc_code"].astype(str).str.len() >= 7]

    # Step 1: compute each group's total wage_bill
    group_wage_bill = df.groupby("soc_major")["wage_bill"].sum().to_dict()
    total_wage_bill = sum(group_wage_bill.values())

    # Step 2: initial proportional allocation
    raw_alloc = {grp: max(1, round(target_total * (wb / total_wage_bill)))
                 for grp, wb in group_wage_bill.items()}

    # Step 3: apply floor — each group with eligible occupations gets ≥floor_per_group
    eligible_groups = set(df["soc_major"].unique())
    alloc = {}
    for grp in eligible_groups:
        n_avail = len(df[df["soc_major"] == grp])
        proposed = max(raw_alloc.get(grp, 0), floor_per_group)
        alloc[grp] = min(proposed, n_avail)  # don't exceed available

    # Step 4: apply cap if any group too dominant (default: no group >25% of total)
    if cap_per_group is None:
        cap_per_group = max(floor_per_group, int(target_total * 0.25))
    for grp in alloc:
        alloc[grp] = min(alloc[grp], cap_per_group)

    # Step 5: trim to target_total — if total > target, remove from largest groups
    while sum(alloc.values()) > target_total:
        largest = max(alloc, key=lambda g: alloc[g])
        if alloc[largest] <= floor_per_group:
            break  # don't go below floor for any group
        alloc[largest] -= 1
    # if sum < target_total, add to highest-wage_bill groups (under cap)
    while sum(alloc.values()) < target_total:
        candidates = [g for g in alloc if alloc[g] < cap_per_group
                      and alloc[g] < len(df[df["soc_major"] == g])]
        if not candidates: break
        # add 1 to highest wage_bill group
        biggest = max(candidates, key=lambda g: group_wage_bill[g])
        alloc[biggest] += 1

    # Step 6: within each group, rank by wage_bill, take top alloc[grp] for primary
    primary = []
    reserves = {}
    for grp, sub in df.sort_values("wage_bill", ascending=False).groupby("soc_major"):
        n_p = alloc.get(grp, 0)
        n_total = n_p + reserves_per_group
        rows = list(sub.head(n_total).itertuples())
        for rank, row in enumerate(rows, start=1):
            rec = {
                "onet_soc_code": row.soc_code,
                "occupation_title": row.occupation_title,
                "soc_major_group": grp,
                "soc_major_group_name": SOC_MAJOR.get(grp, "Unknown"),
                "national_employment": int(row.employment) if row.employment == row.employment else 0,
                "annual_wage_used": float(row.annual_wage_used) if row.annual_wage_used == row.annual_wage_used else 0.0,
                "occupation_wage_bill": float(row.wage_bill),
                "selection_rank_within_soc_group": rank,
                "soc_group_share_of_total_wage_bill": round(group_wage_bill[grp] / total_wage_bill, 4),
                "soc_group_alloc_primary": n_p,
                "selection_reason": ("proportional_top_wage_bill_in_soc_major_group"
                                     if rank <= n_p else "reserve_for_backfill_same_rule"),
            }
            if rank <= n_p:
                primary.append(rec)
            else:
                reserves.setdefault(grp, []).append(rec)
    primary.sort(key=lambda r: r["occupation_wage_bill"], reverse=True)
    return primary, reserves


def select_with_reserves(*, primary_per_group: int = 1, reserves_per_group: int = 2,
                         max_primary: int = 20) -> tuple[list[dict], dict[str, list[dict]]]:
    """Returns (primary_list, reserves_by_soc_major_group).

    For each SOC major group, picks `primary_per_group` primary occupations
    by wage_bill, then `reserves_per_group` more as backups. Primary is
    capped at `max_primary` total.
    """
    df = load_oews()
    df["soc_major"] = df["soc_code"].astype(str).str[:2]
    df = df.dropna(subset=["wage_bill"])
    df = df[df["wage_bill"] > 0]
    df = df[df["soc_code"].astype(str).str.len() >= 7]

    n_per_group = primary_per_group + reserves_per_group
    primary_all: list[dict] = []
    reserves: dict[str, list[dict]] = {}

    for grp, sub in df.sort_values("wage_bill", ascending=False).groupby("soc_major"):
        rows = list(sub.head(n_per_group).itertuples())
        for rank, row in enumerate(rows, start=1):
            rec = {
                "onet_soc_code": row.soc_code,
                "occupation_title": row.occupation_title,
                "soc_major_group": grp,
                "soc_major_group_name": SOC_MAJOR.get(grp, "Unknown"),
                "national_employment": int(row.employment) if row.employment == row.employment else 0,
                "annual_wage_used": float(row.annual_wage_used) if row.annual_wage_used == row.annual_wage_used else 0.0,
                "occupation_wage_bill": float(row.wage_bill),
                "selection_rank_within_soc_group": rank,
                "selection_reason": ("top_wage_bill_in_soc_major_group" if rank <= primary_per_group
                                     else "reserve_for_backfill"),
            }
            if rank <= primary_per_group:
                primary_all.append(rec)
            else:
                reserves.setdefault(grp, []).append(rec)

    primary_all.sort(key=lambda r: r["occupation_wage_bill"], reverse=True)
    primary = primary_all[:max_primary]
    return primary, reserves


def write_selection(out_csv: Path, top_per_group: int = 7, max_total: int = 150):
    rows = select(top_per_group=top_per_group, max_total=max_total)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return rows
