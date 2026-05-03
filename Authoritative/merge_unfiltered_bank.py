"""Reconstruct the FULL unfiltered bank from a run dir, including under-floor SOCs.

Use this AFTER a launch_diversity50 run completes. The launcher's final
items.jsonl only has SOCs ≥15 items (after the hard floor cleanup), but each
worker's items.jsonl preserves all items it ever wrote (including for SOCs
that ended below floor).

This script:
  - Reads all worker_*/items.jsonl (primary attempts, preserved per-SOC)
  - Reads the run-dir items.jsonl (post-floor-cleanup, only ≥15 SOCs)
  - Detects which SOCs in worker outputs but NOT in run-dir items.jsonl =
    under-floor SOCs that were dropped
  - Reads run-dir replacements.json to know which primary→replacement subs
    happened, so we don't double-count
  - Writes:
      items_unfiltered.jsonl — all items (≥15 AND <15 SOCs)
      bank_below_floor.json — list of SOCs that are <15 with their items
      bank_above_floor.json — list of SOCs that are ≥15 with their items

Usage:
  /usr/local/bin/python3 merge_unfiltered_bank.py --run-dir output/diversity50_<ts>
"""
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--floor", type=int, default=15)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)

    # 1. Load run-dir items.jsonl (post-floor-cleanup)
    above_floor_items = defaultdict(list)
    items_file = run_dir / "items.jsonl"
    if items_file.exists():
        for line in items_file.open():
            line = line.strip()
            if not line: continue
            d = json.loads(line)
            above_floor_items[d["onet_soc_code"]].append(d)
    print(f"Run-dir items.jsonl (post-floor): {sum(len(v) for v in above_floor_items.values())} items, {len(above_floor_items)} SOCs")

    # 2. Load all worker outputs (preserves under-floor primaries)
    worker_items = defaultdict(list)
    n_worker_items = 0
    for wd in sorted(run_dir.glob("worker_*")):
        if not wd.is_dir(): continue
        wi = wd / "items.jsonl"
        if not wi.exists(): continue
        for line in wi.open():
            line = line.strip()
            if not line: continue
            d = json.loads(line)
            worker_items[d["onet_soc_code"]].append(d)
            n_worker_items += 1
    print(f"All worker outputs: {n_worker_items} items, {len(worker_items)} SOCs")

    # 3. Load replacements.json to identify primary→replacement substitutions
    replacements_path = run_dir / "replacements.json"
    replaced_primaries = set()  # SOCs that were replaced (don't include their primary items)
    if replacements_path.exists():
        for r in json.loads(replacements_path.read_text()):
            if r.get("replacement_soc"):
                # successful replacement — drop the primary
                replaced_primaries.add(r["removed_soc"])
        print(f"Replaced primaries (will skip from worker outputs): {len(replaced_primaries)}")

    # 4. Combine: prefer run-dir items (post-replace) for SOCs that survived,
    #    add worker items for SOCs that were dropped by floor cleanup
    combined = {}
    for soc, items in above_floor_items.items():
        combined[soc] = list(items)
    for soc, items in worker_items.items():
        if soc in combined:
            continue  # already have it from run-dir
        if soc in replaced_primaries:
            continue  # primary was replaced; don't include
        combined[soc] = list(items)

    print(f"\nCombined unfiltered bank: {sum(len(v) for v in combined.values())} items, {len(combined)} SOCs")

    # 5. Stratify above/below floor
    above = {soc: items for soc, items in combined.items() if len(items) >= args.floor}
    below = {soc: items for soc, items in combined.items() if len(items) < args.floor}

    print(f"  Above floor (≥{args.floor}): {len(above)} SOCs, {sum(len(v) for v in above.values())} items")
    print(f"  Below floor (<{args.floor}): {len(below)} SOCs, {sum(len(v) for v in below.values())} items")

    # 6. Write outputs
    out_unfiltered = run_dir / "items_unfiltered.jsonl"
    with out_unfiltered.open("w") as f:
        for soc, items in sorted(combined.items()):
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
    print(f"\nWrote {out_unfiltered}")

    out_below = run_dir / "bank_below_floor.json"
    out_below.write_text(json.dumps({
        "floor": args.floor,
        "n_socs_below": len(below),
        "n_items_below": sum(len(v) for v in below.values()),
        "socs": [{"soc": soc,
                  "occupation": items[0].get("occupation_title", "?") if items else "?",
                  "items_count": len(items)}
                 for soc, items in sorted(below.items(), key=lambda x: -len(x[1]))],
    }, indent=2))
    print(f"Wrote {out_below} ({len(below)} SOCs)")

    out_above = run_dir / "bank_above_floor.json"
    out_above.write_text(json.dumps({
        "floor": args.floor,
        "n_socs_above": len(above),
        "n_items_above": sum(len(v) for v in above.values()),
        "socs": [{"soc": soc,
                  "occupation": items[0].get("occupation_title", "?") if items else "?",
                  "items_count": len(items)}
                 for soc, items in sorted(above.items(), key=lambda x: -len(x[1]))],
    }, indent=2))
    print(f"Wrote {out_above} ({len(above)} SOCs)")

    print("\n========== SUMMARY ==========")
    print(f"Above floor (≥{args.floor}):")
    for soc, items in sorted(above.items(), key=lambda x: -len(x[1])):
        print(f"  ✓ {len(items):3d}  {soc}  {items[0].get('occupation_title', '?')[:55]}")
    print(f"\nBelow floor (<{args.floor}, NOT dropped — saved for review):")
    for soc, items in sorted(below.items(), key=lambda x: -len(x[1])):
        print(f"    {len(items):3d}  {soc}  {items[0].get('occupation_title', '?')[:55]}")


if __name__ == "__main__":
    main()
