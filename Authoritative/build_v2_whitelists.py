"""Build per-occupation whitelists for the wage-bill weighted top 20 + reserves.

Process:
  1. Compute wage-bill stratified primary 20 + reserves (2 per SOC major group)
  2. Map BLS SOC (e.g. 41-2031) to O*NET-SOC (41-2031.00)
  3. Merge with existing per_occupation_associations_pilot20.json (preserve overlap)
  4. For each NEW SOC (primary + reserves), build a whitelist via source_selection
  5. Write merged result to v2_pipeline/output/per_occupation_associations_v2.json
  6. Write the selection table + chunk CSV + reserves manifest

Run once before the pipeline. Takes ~10-20 min for ~50-60 new SOCs.
"""
from __future__ import annotations
import csv
import json
import sys
import time

from pipeline import config, occupation_selection, source_selection


def main():
    print("Computing wage-bill weighted primary 20 + reserves...")
    primary_recs, reserves_recs = occupation_selection.select_with_reserves(
        primary_per_group=1, reserves_per_group=2, max_primary=20,
    )

    # Map BLS soc → O*NET-SOC (.00 suffix)
    primary = [(r["occupation_title"], f"{r['onet_soc_code']}.00") for r in primary_recs]
    reserves = {grp: [(r["occupation_title"], f"{r['onet_soc_code']}.00") for r in rs]
                for grp, rs in reserves_recs.items()}

    print(f"\nPrimary {len(primary)} occupations:")
    for occ, soc in primary:
        print(f"  {soc}  {occ}")

    n_reserves = sum(len(rs) for rs in reserves.values())
    print(f"\nReserves: {n_reserves} across {len(reserves)} SOC major groups")

    all_pairs = list(primary)
    seen = set(soc for _, soc in all_pairs)
    for grp, rs in reserves.items():
        for occ, soc in rs:
            if soc not in seen:
                all_pairs.append((occ, soc))
                seen.add(soc)

    # Clean rebuild: do NOT merge v1 pilot 20 entries (they were picked
    # before the org_specificity sort and §4.2 enrichment landed). Re-pick
    # every SOC fresh so the new picker logic applies uniformly.
    existing = {}
    print(f"\nClean rebuild: not merging v1 pilot 20 entries")
    print(f"Need new whitelists for: {len(all_pairs)} SOCs (5 primary + reserves)\n")

    print("Building per-occupation whitelists with org_specificity sort + §4.2 enrichment...")
    t0 = time.time()
    merged = source_selection.build_whitelists(all_pairs, existing=existing)
    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")

    out_path = config.V2_OUT / "per_occupation_associations_v2.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(merged, indent=2))
    print(f"Wrote {out_path} ({len(merged)} SOCs total)")

    sel_path = config.V2_OUT / "selected_20_wagebill.csv"
    with sel_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(primary_recs[0].keys()))
        w.writeheader()
        for row in primary_recs:
            w.writerow(row)
    print(f"Wrote {sel_path}")

    chunk_path = config.V2_OUT / "pilot20_v2_chunk.csv"
    with chunk_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["occupation_title", "soc_code"])
        for occ, soc in primary:
            w.writerow([occ, soc])
    print(f"Wrote {chunk_path}")

    # Reserves manifest (for the orchestrator to load when running replacement)
    res_path = config.V2_OUT / "reserves_v2.json"
    res_serializable = {grp: [{"occupation": occ, "soc": soc} for occ, soc in pairs]
                        for grp, pairs in reserves.items()}
    res_path.write_text(json.dumps(res_serializable, indent=2))
    print(f"Wrote {res_path}")


if __name__ == "__main__":
    sys.exit(main())
