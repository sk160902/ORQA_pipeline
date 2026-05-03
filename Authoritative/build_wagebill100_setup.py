"""Build wage-weighted setup (proportional allocation).


  - Allocate slots PROPORTIONALLY to each SOC major group's share of total wage_bill
  - Floor: ≥3 per included major group
  - Cap: prevent any group from dominating
  - Within each group, rank by wage_bill, take top
  - Reserves use SAME RULE (wage-bill within same SOC group)

target ≥100 surviving SOCs at ≥5 questions each.
Over-seed primaries to 130 to compensate for ~25% attrition below ≥5 floor.

Outputs:
  - output/pilot20_v2_chunk.csv        (130 wage-weighted primaries)
  - output/reserves_v2_expanded.json   (66 wage-weighted reserves, same-rule)
  - output/wagebill100_selected.csv    (full metadata for paper appendix)
"""
from __future__ import annotations
import csv, json, shutil

from pipeline import config, occupation_selection


def main():
    # Load O*NET-SOC valid codes
    onet_socs = set()
    onet_path = "/Users/Shreyas2/Desktop/Berkeley/occupation_task/data/db_29_1_text/Occupation Data.txt"
    with open(onet_path) as f:
        next(f)
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2:
                onet_socs.add(p[0])

    def to_onet(soc: str) -> str:
        return soc if "." in soc else f"{soc}.00"

    # Plan §3.2 proportional allocation, scaled to 500 primaries
    primary_raw, reserves_raw = occupation_selection.select_proportional(
        target_total=500,    # max coverage; expect 200-260 surviving at ≥5, 80-120 at ≥15
        floor_per_group=3,   # plan §3.2: floor ≥3 per included group
        cap_per_group=80,    # cap to prevent any group from dominating (max ~16% per group)
        reserves_per_group=3,
    )
    # Filter primaries to O*NET-valid SOCs (some BLS aggregates aren't in O*NET DB)
    primary_valid = [r for r in primary_raw if to_onet(r["onet_soc_code"]) in onet_socs]
    primary = primary_valid
    # If we lost some to O*NET filter, top up to 130 by including additional candidates
    n_dropped = len(primary_raw) - len(primary_valid)
    print(f"PLAN §3.2 PROPORTIONAL ALLOCATION")
    print(f"Primaries (proportional, wage-bill weighted): {len(primary)} (dropped {n_dropped} non-O*NET BLS aggregates)")
    # Filter reserves
    reserves = {}
    for grp, recs in reserves_raw.items():
        valid = [r for r in recs if to_onet(r["onet_soc_code"]) in onet_socs]
        if valid:
            reserves[grp] = valid[:3]
    print(f"Reserves: {sum(len(v) for v in reserves.values())} across {len(reserves)} SOC major groups")
    print(f"Over-seeded by {len(primary)-100}; expect ~100 surviving at ≥5 floor")

    # Backup current chunk + reserves
    chunk_path = config.V2_OUT / "pilot20_v2_chunk.csv"
    reserves_path = config.V2_OUT / "reserves_v2_expanded.json"
    if chunk_path.exists():
        shutil.copy2(chunk_path, chunk_path.with_suffix(".csv.bak_pre_proportional"))
    if reserves_path.exists():
        shutil.copy2(reserves_path, reserves_path.with_suffix(".json.bak_pre_proportional"))

    # Write chunk CSV
    with chunk_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["occupation_title", "soc_code"])
        for r in primary:
            w.writerow([r["occupation_title"], to_onet(r["onet_soc_code"])])
    print(f"\nWrote {chunk_path}: {len(primary)} primaries")

    # Write reserves JSON
    reserves_serializable = {
        grp: [{"occupation": r["occupation_title"], "soc": to_onet(r["onet_soc_code"])}
              for r in recs]
        for grp, recs in reserves.items()
    }
    reserves_path.write_text(json.dumps(reserves_serializable, indent=2))
    print(f"Wrote {reserves_path}: {sum(len(v) for v in reserves_serializable.values())} reserves "
          f"across {len(reserves_serializable)} SOC major groups")

    # Selection metadata for paper appendix
    sel_path = config.V2_OUT / "wagebill100_selected.csv"
    with sel_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(primary[0].keys()))
        w.writeheader()
        for r in primary:
            w.writerow(r)
    print(f"Wrote {sel_path}: full metadata for {len(primary)} primaries")

    # Distribution summary
    from collections import Counter
    groups = Counter(r["soc_major_group"] for r in primary)
    print(f"\nFinal SOC group distribution:")
    for grp in sorted(groups):
        n = groups[grp]
        share = next((r["soc_group_share_of_total_wage_bill"] for r in primary if r["soc_major_group"]==grp), 0)
        print(f"  SOC {grp}: {n} primaries (wage-bill share={share*100:.1f}%)")
    return 0


if __name__ == "__main__":
    main()
