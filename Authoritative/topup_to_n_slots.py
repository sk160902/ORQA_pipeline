"""Top up an existing run to N populated slots via expanded reserves +
cross-SOC backfill.

Process:
  1. Read existing run dir (items.jsonl, stats.json, replacements.json)
  2. Identify currently populated slots (those whose SOC is NOT in removed_socs
     OR whose entry has replacement_for set with kept ≥15)
  3. For each dropped slot (SOC in removed_socs that doesn't yet have a
     successful replacement):
       Try EXPANDED reserves (reserves_v2_expanded.json) in SOC group order
       Skip reserves already tried in this run
       Stop when one yields ≥15
  4. After in-SOC exhaustion, if still <N populated:
       Cross-SOC backfill — try unused reserves from ANY SOC group
       (round-robin across SOCs to maintain some diversity)
  5. Apply ≥15 floor cleanup + giveaway filter at end

Parallel: uses N worker threads with locks around shared state.

Usage:
  /usr/local/bin/python3 v2_pipeline/topup_to_n_slots.py \
      --run-dir output/pilot20_parallel_v4 \
      --workers 8 --target-slots 20 --min-items-for-accept 15
"""
from __future__ import annotations
import argparse
import json
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent

import sys
sys.path.insert(0, str(ROOT))
from pipeline import config, orchestrator, post_replace
from pipeline.item_generator import _GIVEAWAY_DISTRACTOR_RES, _SOURCE_NAMING_RES


def _check_giveaway(item: dict) -> bool:
    """Returns True if any distractor matches a giveaway pattern OR correct
    option names a source. Used in the post-hoc cleanup filter."""
    opts = item.get("options") or {}
    correct = item.get("correct_answer", "")
    for letter in "ABCD":
        if letter == correct: continue
        opt = opts.get(letter, "") or ""
        for r in _GIVEAWAY_DISTRACTOR_RES:
            if r.search(opt): return True
    correct_opt = opts.get(correct, "") or ""
    for r in _SOURCE_NAMING_RES:
        if r.search(correct_opt): return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--target", type=int, default=20)  # items per slot
    ap.add_argument("--target-slots", type=int, default=20)
    ap.add_argument("--min-items-for-accept", type=int, default=15)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    items_path = run_dir / "items.jsonl"
    cards_path = run_dir / "cards.jsonl"
    stats_path = run_dir / "stats.json"
    repl_path = run_dir / "replacements.json"

    config.PER_OCC_ASSOCIATIONS = config.V2_OUT / "per_occupation_associations_v2.json"

    # Load expanded reserves
    expanded_reserves: dict[str, list[tuple[str, str]]] = {}
    expanded_path = config.V2_OUT / "reserves_v2_expanded.json"
    raw = json.loads(expanded_path.read_text())
    for grp, lst in raw.items():
        expanded_reserves[grp] = [(it["occupation"], it["soc"]) for it in lst]

    # Load existing state
    merged_stats = json.loads(stats_path.read_text())
    existing_repl = json.loads(repl_path.read_text()) if repl_path.exists() else []

    # Identify currently populated slots from items.jsonl
    items = [json.loads(l) for l in items_path.open() if l.strip()]
    populated_socs: set[str] = set(it["onet_soc_code"] for it in items)
    items_per_soc = defaultdict(int)
    for it in items:
        items_per_soc[it["onet_soc_code"]] += 1
    populated_slots = {soc for soc, n in items_per_soc.items() if n >= args.min_items_for_accept}
    print(f"Currently populated slots (≥{args.min_items_for_accept} items): {len(populated_slots)}")
    for soc in sorted(populated_slots):
        print(f"  {soc}: {items_per_soc[soc]} items")

    # Identify SOCs we've already tried (primary + previous reserve attempts)
    tried_socs: set[str] = set()
    for k in merged_stats:
        if "__replacement_for_" in k:
            soc = k.split("__")[0]
            tried_socs.add(soc)
        else:
            tried_socs.add(k)
    print(f"\nSOCs already tried: {len(tried_socs)}")

    # Identify dropped slots (in removed_socs from replacements.json)
    removed_socs = set(r.get("removed_soc") for r in existing_repl if r.get("removed_soc"))
    print(f"SOCs that triggered replacement (some succeeded, some failed): {len(removed_socs)}")

    # We need (target_slots - populated) more populated slots
    needed = args.target_slots - len(populated_slots)
    print(f"\nTarget: {args.target_slots} slots; current: {len(populated_slots)}; need: {needed} more\n")
    if needed <= 0:
        print("Already at target. Done.")
        return

    # Build the queue of (soc_to_replace, candidate_reserves)
    # First: dropped SOCs whose in-SOC reserves are not exhausted
    backfill_queue = []
    for r in existing_repl:
        primary_soc = r.get("removed_soc")
        if not primary_soc: continue
        # If this primary already has a successful replacement (replacement_soc not None
        # and replacement_items_kept >= min), don't re-try
        if r.get("replacement_soc") and r.get("replacement_items_kept", 0) >= args.min_items_for_accept:
            continue
        grp = primary_soc[:2]
        # Get expanded reserves for this group, exclude already-tried
        avail = [(occ, soc) for occ, soc in expanded_reserves.get(grp, [])
                 if soc not in tried_socs and soc not in populated_socs]
        if avail:
            backfill_queue.append((primary_soc, avail))
    print(f"Dropped slots with available expanded reserves: {len(backfill_queue)}")

    # Locks / shared state
    populated_lock = threading.Lock()
    used_reserve_socs: set[str] = set()
    reserve_lock = threading.Lock()
    write_lock = threading.Lock()
    log_lock = threading.Lock()

    log_path = run_dir / "topup.log"
    log_f = log_path.open("w")
    def log(msg):
        with log_lock:
            print(msg)
            log_f.write(msg + "\n")
            log_f.flush()

    log(f"\n========== TOPUP TO {args.target_slots} SLOTS ==========")
    log(f"Workers: {args.workers}; min_items_for_accept: {args.min_items_for_accept}")

    items_f = items_path.open("a")
    cards_f = cards_path.open("a")

    def _try_one_dropped_slot(primary_soc, candidate_reserves):
        """Iterate through reserves for one dropped slot. Stop on first ≥min success
        OR target_slots reached globally."""
        for r_idx, (r_occ, r_soc) in enumerate(candidate_reserves, 1):
            with populated_lock:
                if len(populated_slots) >= args.target_slots:
                    return
            with reserve_lock:
                if r_soc in used_reserve_socs:
                    continue
                if r_soc in populated_socs:
                    continue
                used_reserve_socs.add(r_soc)
            log(f"  [for {primary_soc}] trying reserve {r_idx}/{len(candidate_reserves)}: {r_soc} ({r_occ})")
            try:
                result = orchestrator.process_occupation(
                    r_occ, r_soc, target_items=args.target, log=log,
                    replacement_for=primary_soc,
                )
            except Exception as e:
                log(f"  [for {primary_soc}] !! ERROR running reserve {r_soc}: {e}")
                continue
            r_kept = result["stats"].get("items_kept", 0)
            log(f"  [for {primary_soc}] reserve {r_soc} yielded {r_kept} items")
            if r_kept >= args.min_items_for_accept:
                with write_lock:
                    for it in result["items"]:
                        items_f.write(json.dumps(it.to_dict(), ensure_ascii=False) + "\n")
                    items_f.flush()
                    for c in result["cards"]:
                        cards_f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
                    cards_f.flush()
                rec_key = f"{r_soc}__replacement_for_{primary_soc}"
                merged_stats[rec_key] = {"occupation": r_occ, **result["stats"]}
                stats_path.write_text(json.dumps(merged_stats, indent=2))
                with populated_lock:
                    populated_slots.add(r_soc)
                    populated_socs.add(r_soc)
                # Update replacements.json
                existing_repl.append({
                    "removed_soc": primary_soc,
                    "replacement_soc": r_soc,
                    "replacement_occupation": r_occ,
                    "soc_major_group": primary_soc[:2],
                    "replacement_items_kept": r_kept,
                    "replacement_reason": "topup_in_soc",
                    "reserves_tried": r_idx,
                })
                repl_path.write_text(json.dumps(existing_repl, indent=2))
                log(f"  [for {primary_soc}] ✓ filled by {r_soc} ({r_kept} items)")
                return

    # Phase 1: in-SOC backfill (parallel)
    log("\n--- Phase 1: in-SOC iterative backfill ---")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(_try_one_dropped_slot, ps, av) for ps, av in backfill_queue]
        for f in as_completed(futures):
            try: f.result()
            except Exception as e: log(f"  worker exception: {e}")

    log(f"\n--- After Phase 1: {len(populated_slots)} populated slots ---")

    # Phase 2: cross-SOC backfill if still under target
    if len(populated_slots) < args.target_slots:
        # Build a global pool of unused reserves (from any SOC), excluding already-tried
        global_pool = []
        for grp, rs in expanded_reserves.items():
            for occ, soc in rs:
                if soc in used_reserve_socs: continue
                if soc in tried_socs: continue
                if soc in populated_socs: continue
                global_pool.append((grp, occ, soc))
        log(f"\n--- Phase 2: cross-SOC backfill — {len(global_pool)} global reserves available ---")

        def _try_one_cross_soc(grp, r_occ, r_soc):
            with populated_lock:
                if len(populated_slots) >= args.target_slots: return
            with reserve_lock:
                if r_soc in used_reserve_socs: return
                if r_soc in populated_socs: return
                used_reserve_socs.add(r_soc)
            log(f"  [cross-SOC, group={grp}] trying {r_soc} ({r_occ})")
            try:
                result = orchestrator.process_occupation(
                    r_occ, r_soc, target_items=args.target, log=log,
                    replacement_for="cross_soc_backfill",
                )
            except Exception as e:
                log(f"  [cross-SOC] !! ERROR: {e}"); return
            r_kept = result["stats"].get("items_kept", 0)
            if r_kept >= args.min_items_for_accept:
                with write_lock:
                    for it in result["items"]:
                        items_f.write(json.dumps(it.to_dict(), ensure_ascii=False) + "\n")
                    items_f.flush()
                    for c in result["cards"]:
                        cards_f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
                    cards_f.flush()
                rec_key = f"{r_soc}__cross_soc_backfill"
                merged_stats[rec_key] = {"occupation": r_occ, **result["stats"]}
                stats_path.write_text(json.dumps(merged_stats, indent=2))
                with populated_lock:
                    populated_slots.add(r_soc)
                    populated_socs.add(r_soc)
                existing_repl.append({
                    "removed_soc": "cross_soc_backfill",
                    "replacement_soc": r_soc,
                    "replacement_occupation": r_occ,
                    "soc_major_group": grp,
                    "replacement_items_kept": r_kept,
                    "replacement_reason": "cross_soc_backfill",
                    "reserves_tried": 1,
                })
                repl_path.write_text(json.dumps(existing_repl, indent=2))
                log(f"  [cross-SOC] ✓ filled by {r_soc} ({r_kept} items)")

        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = []
            # Round-robin order across SOC groups for diversity
            global_pool.sort(key=lambda t: (t[0],))
            for grp, occ, soc in global_pool:
                with populated_lock:
                    if len(populated_slots) >= args.target_slots: break
                futures.append(ex.submit(_try_one_cross_soc, grp, occ, soc))
            for f in as_completed(futures):
                try: f.result()
                except Exception as e: log(f"  cross-SOC worker exception: {e}")

    items_f.close()
    cards_f.close()

    # Apply giveaway filter + ≥15 floor cleanup at end
    log("\n========== POST-HOC FILTERS ==========")
    items = [json.loads(l) for l in items_path.open() if l.strip()]
    kept = [it for it in items if not _check_giveaway(it)]
    n_giveaway = len(items) - len(kept)
    by_soc = defaultdict(list)
    for it in kept:
        by_soc[it["onet_soc_code"]].append(it)
    floor_kept = [it for soc, lst in by_soc.items() if len(lst) >= args.min_items_for_accept for it in lst]
    n_floor = len(kept) - len(floor_kept)
    items_path.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in floor_kept) + "\n")
    log(f"  giveaway filter dropped: {n_giveaway}")
    log(f"  ≥15 floor dropped:       {n_floor}")

    # Final tally
    final_items = [json.loads(l) for l in items_path.open() if l.strip()]
    by_soc_final = defaultdict(int)
    for it in final_items:
        by_soc_final[it["onet_soc_code"]] += 1
    log(f"\n========== FINAL ==========")
    log(f"Total items: {len(final_items)}")
    log(f"Populated slots: {len(by_soc_final)}")
    for soc, n in sorted(by_soc_final.items(), key=lambda x: -x[1]):
        log(f"  {soc}: {n} items")

    log_f.close()


if __name__ == "__main__":
    main()
