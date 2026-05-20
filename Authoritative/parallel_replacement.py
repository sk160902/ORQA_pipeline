"""Parallel iterative replacement pass over a completed primary-pass run dir.

Use after launch_pilot20_parallel.py has finished its primary pass (or after
killing it mid-replacement). Reads the merged primary state and runs
iterative replacement IN PARALLEL across N threads, with a lock around
reserve assignment to prevent two slots in the same SOC group from racing
for the same reserve.

Behavior matches the iterative replacement in launch_pilot20_parallel.py:
- For each under-yield primary, try reserves in SOC group order until one
  yields ≥min_items_for_accept, OR all reserves exhausted.
- Successful reserve's items appended to items.jsonl; reserve marked used.
- Failed slots recorded in replacements.json with all_reserves_exhausted.
- Final strict REPLACE filter applied at end.

Usage:
  /usr/local/bin/python3 v2_pipeline/parallel_replacement.py \
      --run-dir output/pilot20_parallel_v4 \
      --workers 8 --min-items-for-accept 15
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--target", type=int, default=20)
    ap.add_argument("--min-items-for-accept", type=int, default=15)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    items_path = run_dir / "items.jsonl"
    cards_path = run_dir / "cards.jsonl"
    stats_path = run_dir / "stats.json"

    if not stats_path.exists():
        print(f"ERROR: missing {stats_path}")
        return

    # Switch to wage-bill v2 whitelist (matches what launch_pilot20_parallel uses)
    wagebill_assocs = config.V2_OUT / "per_occupation_associations_v2.json"
    if wagebill_assocs.exists():
        config.PER_OCC_ASSOCIATIONS = wagebill_assocs

    merged_stats = json.loads(stats_path.read_text())

    # Reconstruct primary_items per SOC for source-concentration analysis
    primary_items = defaultdict(list)
    if items_path.exists():
        for line in items_path.open():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            class _Shim:
                def __init__(self, d):
                    self.publisher = d.get("publisher", "")
                    self.source_domain = d.get("source_domain", "")
                    self.source_url = d.get("source_url", "")
                    self.source_quote = d.get("source_quote", "")
            primary_items[d["onet_soc_code"]].append(_Shim(d))

    # Load reserves
    reserves_path = config.V2_OUT / "reserves_v2.json"
    reserves: dict = {}
    if reserves_path.exists():
        raw = json.loads(reserves_path.read_text())
        for grp, lst in raw.items():
            reserves[grp] = [(it["occupation"], it["soc"]) for it in lst]

    # Identify under-yield slots that need replacement
    needs_replacement = []  # list of (primary_soc, primary_stats, top_share, n_pubs, n_docs, triggers)
    for primary_soc, stats in merged_stats.items():
        if "__replacement_for_" in primary_soc:
            continue
        kept = stats.get("items_kept", 0)
        occ_items = primary_items.get(primary_soc, [])
        top_share, n_pubs, n_docs = orchestrator._publisher_concentration(occ_items)
        triggers = []
        if kept < args.min_items_for_accept:
            triggers.append(f"items={kept}<{args.min_items_for_accept}")
        if kept > 0 and top_share > 0.7:
            triggers.append(f"top_pub={top_share*100:.0f}%>70%")
        if kept > 0 and n_pubs < 2:
            triggers.append(f"n_pubs={n_pubs}<2")
        if kept > 0 and n_docs < 3:
            triggers.append(f"n_docs={n_docs}<3")
        if triggers:
            needs_replacement.append((primary_soc, stats, top_share, n_pubs, n_docs, triggers))

    print(f"Under-yield slots needing replacement: {len(needs_replacement)}")
    for ps, st, *_ in needs_replacement:
        print(f"  {ps} ({st.get('occupation','?')[:50]}) — kept={st.get('items_kept',0)}")

    # Locks for thread-safe shared state
    used_reserve_socs: set[str] = set()
    reserve_lock = threading.Lock()
    write_lock = threading.Lock()
    stats_lock = threading.Lock()
    log_lock = threading.Lock()

    log_path = run_dir / "replacement_pass_parallel.log"
    log_f = log_path.open("w")
    def log(msg):
        with log_lock:
            print(msg)
            log_f.write(msg + "\n")
            log_f.flush()

    log(f"\n========== PARALLEL ITERATIVE REPLACEMENT PASS ==========")
    log(f"Workers: {args.workers}")
    log(f"Triggers: items<{args.min_items_for_accept} OR top-pub>70% OR n_pubs<2 OR n_docs<3")
    log(f"Strategy: try reserves in SOC group order until ≥{args.min_items_for_accept}, OR exhausted")

    items_f = items_path.open("a")
    cards_f = cards_path.open("a")

    replacements_log = []
    repl_log_lock = threading.Lock()

    def _try_one_under_yield(primary_soc, stats, top_share, n_pubs, n_docs, triggers):
        """Process one under-yield slot — try reserves until one succeeds or exhausted."""
        grp = primary_soc[:2]
        reserve_pool = reserves.get(grp, [])
        log(f"\n  primary {primary_soc} ({stats.get('occupation','?')[:40]}, kept={stats.get('items_kept',0)}) "
            f"under-yielded: triggers=[{', '.join(triggers)}]; reserves available in SOC {grp}: {len(reserve_pool)}")

        for r_idx, (r_occ, r_soc) in enumerate(reserve_pool, 1):
            # Lock around reserve assignment — only one thread gets each reserve
            with reserve_lock:
                if r_soc in used_reserve_socs:
                    continue
                if r_soc in merged_stats:
                    continue
                used_reserve_socs.add(r_soc)
            log(f"  [primary {primary_soc}] trying reserve {r_idx}/{len(reserve_pool)}: {r_soc} ({r_occ})")
            try:
                result = orchestrator.process_occupation(
                    r_occ, r_soc, target_items=args.target, log=log,
                    replacement_for=primary_soc,
                )
            except Exception as e:
                log(f"  [primary {primary_soc}] !! ERROR running reserve {r_soc}: {e}")
                continue
            r_kept = result["stats"].get("items_kept", 0)
            log(f"  [primary {primary_soc}] reserve {r_soc} yielded {r_kept} items")
            if r_kept >= args.min_items_for_accept:
                with write_lock:
                    for it in result["items"]:
                        items_f.write(json.dumps(it.to_dict(), ensure_ascii=False) + "\n")
                    items_f.flush()
                    for c in result["cards"]:
                        cards_f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
                    cards_f.flush()
                rec_key = f"{r_soc}__replacement_for_{primary_soc}"
                with stats_lock:
                    merged_stats[rec_key] = {"occupation": r_occ, **result["stats"]}
                    stats_path.write_text(json.dumps(merged_stats, indent=2))
                with repl_log_lock:
                    replacements_log.append({
                        "removed_soc": primary_soc,
                        "removed_occupation": stats.get("occupation", ""),
                        "removed_items_kept": stats.get("items_kept", 0),
                        "removed_top_publisher_share": top_share,
                        "removed_unique_publishers": n_pubs,
                        "removed_unique_documents": n_docs,
                        "replacement_soc": r_soc,
                        "replacement_occupation": r_occ,
                        "soc_major_group": grp,
                        "replacement_items_kept": r_kept,
                        "replacement_reason": "; ".join(triggers),
                        "reserves_tried": r_idx,
                    })
                    (run_dir / "replacements.json").write_text(json.dumps(replacements_log, indent=2))
                log(f"  [primary {primary_soc}] ✓ replacement successful: {r_soc} → {r_kept} items (≥{args.min_items_for_accept})")
                return  # success — stop
            else:
                log(f"  [primary {primary_soc}] reserve {r_soc} under-yielded ({r_kept}<{args.min_items_for_accept}); discarding, trying next")

        # All reserves exhausted
        log(f"  [primary {primary_soc}] ✗ all reserves exhausted; slot will be empty in final bank")
        with repl_log_lock:
            replacements_log.append({
                "removed_soc": primary_soc,
                "removed_occupation": stats.get("occupation", ""),
                "removed_items_kept": stats.get("items_kept", 0),
                "removed_top_publisher_share": top_share,
                "removed_unique_publishers": n_pubs,
                "removed_unique_documents": n_docs,
                "replacement_soc": None,
                "replacement_occupation": None,
                "soc_major_group": grp,
                "replacement_items_kept": 0,
                "replacement_reason": "; ".join(triggers) + "; all reserves exhausted",
                "reserves_tried": len(reserve_pool),
            })
            (run_dir / "replacements.json").write_text(json.dumps(replacements_log, indent=2))

    # Run in parallel
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(_try_one_under_yield, *u) for u in needs_replacement]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                log(f"  !! worker exception: {e}")

    items_f.close()
    cards_f.close()

    # Apply strict REPLACE filter
    log("\n========== STRICT REPLACE FILTER ==========")
    result = post_replace.apply_replace_filter(run_dir)
    log(json.dumps(result, indent=2))

    log(f"\n========== RUN END ==========")
    log_f.close()

    final_n = sum(1 for _ in items_path.open())
    print(f"\nDONE. Final bank: {final_n} items at {items_path}")


if __name__ == "__main__":
    main()
