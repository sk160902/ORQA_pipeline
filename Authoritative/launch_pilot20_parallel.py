"""Parallel orchestrator: 20 wage-bill occupations across N workers.

Each worker runs the FULL per-occupation pipeline (extract → build → verify →
Pass 7 → cognitive type) on its chunk of occupations, writing to its own
subdirectory. The coordinator then:
  1. Merges all workers' items.jsonl / cards.jsonl / stats.json
  2. Runs the replacement pass over the merged primary results (so reserves
     are picked from the same SOC major group across the whole batch)
  3. Applies the strict REPLACE filter (drops under-yield primaries
     that got replaced)

Workers run with --skip-replacement so they don't each try to replace.

Usage:
  /usr/local/bin/python3 v2_pipeline/launch_pilot20_parallel.py [--workers 8] [--target 20]
"""
from __future__ import annotations
import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = "/usr/local/bin/python3"


def split_round_robin(items, n):
    chunks = [[] for _ in range(n)]
    for i, x in enumerate(items):
        chunks[i % n].append(x)
    return chunks


def write_chunk_csv(path: Path, rows):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["occupation_title", "soc_code"])
        for occ, soc in rows:
            w.writerow([occ, soc])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8, help="parallel workers")
    ap.add_argument("--target", type=int, default=20, help="items per occupation")
    ap.add_argument("--run-name", default=None, help="output dir name (default pilot20_parallel_<ts>)")
    ap.add_argument("--min-items-for-accept", type=int, default=15)
    args = ap.parse_args()

    name = args.run_name or f"pilot20_parallel_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir = ROOT / "output" / name
    run_dir.mkdir(parents=True, exist_ok=True)

    # Load the wage-bill 20 chunk CSV
    chunk_csv = ROOT / "output" / "pilot20_v2_chunk.csv"
    if not chunk_csv.exists():
        print(f"ERROR: missing {chunk_csv}. Run build_v2_whitelists.py first.")
        sys.exit(1)
    occupations = []
    with chunk_csv.open() as f:
        for r in csv.DictReader(f):
            occupations.append((r["occupation_title"], r["soc_code"]))
    print(f"Loaded {len(occupations)} wage-bill occupations.")

    chunks = split_round_robin(occupations, args.workers)
    chunks = [c for c in chunks if c]  # drop empty chunks if workers > occs
    print(f"Split into {len(chunks)} chunks: {[len(c) for c in chunks]}")

    # Write chunk CSVs
    for i, chunk in enumerate(chunks):
        write_chunk_csv(run_dir / f"chunk_{i}.csv", chunk)

    # Launch N workers in parallel — each runs run_pilot20.py with its chunk,
    # --skip-replacement (we handle replacement after merge).
    pids = []
    stdout_paths = []
    for i in range(len(chunks)):
        worker_dir = f"{name}/worker_{i}"
        stdout_path = run_dir / f"worker_{i}.stdout"
        stdout_paths.append(stdout_path)
        cmd = [
            PY, "-u", str(ROOT / "run_pilot20.py"),
            "--chunk-file", str(run_dir / f"chunk_{i}.csv"),
            "--target", str(args.target),
            "--skip-replacement",
            "--run-name", worker_dir,
        ]
        with stdout_path.open("w") as out:
            p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                 cwd=str(ROOT))
        pids.append(p)
        print(f"  worker {i}: PID {p.pid}  ({len(chunks[i])} occupations)  → {worker_dir}/")
        # Light stagger to avoid burst rate-limit collisions
        time.sleep(2)

    print(f"\nAll {len(pids)} workers launched. Waiting for completion...")
    print(f"Monitor: tail -f {run_dir}/worker_*.stdout")

    # Wait for all
    for i, p in enumerate(pids):
        rc = p.wait()
        print(f"  worker {i}: exited rc={rc}")

    # Merge primary outputs
    print("\nMerging worker outputs...")
    items_path = run_dir / "items.jsonl"
    cards_path = run_dir / "cards.jsonl"
    stats_path = run_dir / "stats.json"

    merged_stats = {}
    with items_path.open("w") as items_f, cards_path.open("w") as cards_f:
        for i in range(len(chunks)):
            wd = ROOT / "output" / name / f"worker_{i}"
            wi = wd / "items.jsonl"
            wc = wd / "cards.jsonl"
            ws = wd / "stats.json"
            if wi.exists():
                items_f.write(wi.read_text())
            if wc.exists():
                cards_f.write(wc.read_text())
            if ws.exists():
                worker_stats = json.loads(ws.read_text())
                merged_stats.update(worker_stats)
    stats_path.write_text(json.dumps(merged_stats, indent=2))

    n_items = sum(1 for _ in items_path.open())
    print(f"  merged: {n_items} primary items, {len(merged_stats)} occupation entries")

    # Replacement pass over merged data
    print("\nRunning replacement coordinator...")
    sys.path.insert(0, str(ROOT))
    from pipeline import config, orchestrator, schemas, post_replace

    # Switch whitelist to wage-bill v2
    wagebill_assocs = config.V2_OUT / "per_occupation_associations_v2.json"
    if wagebill_assocs.exists():
        config.PER_OCC_ASSOCIATIONS = wagebill_assocs

    # Load reserves
    reserves_path = config.V2_OUT / "reserves_v2.json"
    reserves: dict = {}
    if reserves_path.exists():
        raw = json.loads(reserves_path.read_text())
        for grp, lst in raw.items():
            reserves[grp] = [(it["occupation"], it["soc"]) for it in lst]

    # Reconstruct primary_items from items.jsonl (keyed by SOC)
    from collections import defaultdict
    primary_items = defaultdict(list)
    for line in items_path.open():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        # Wrap in a tiny shim so _publisher_concentration can read attributes
        class _ItemShim:
            def __init__(self, d):
                self.publisher = d.get("publisher", "")
                self.source_domain = d.get("source_domain", "")
                self.source_url = d.get("source_url", "")
                self.source_quote = d.get("source_quote", "")
        primary_items[d["onet_soc_code"]].append(_ItemShim(d))

    # Identify under-yield / source-dominated occupations + run ITERATIVE replacements
    # Replacement keeps trying reserves until one yields ≥15 OR
    # all reserves in the SOC group are exhausted. This guarantees a slot has
    # ≥15 items whenever the source ecosystem can support it; if all reserves
    # under-yield, we drop the slot and the bank reflects that honestly.
    log_path = run_dir / "replacement_pass.log"
    log_f = log_path.open("w")
    def log(msg):
        print(msg)
        log_f.write(msg + "\n")
        log_f.flush()

    log(f"\n========== ITERATIVE REPLACEMENT PASS (post-merge) ==========")
    log(f"Triggers: items<{args.min_items_for_accept}  OR  top-pub>70%  OR  unique-pubs<2  OR  unique-docs<3")
    log(f"Strategy: try each reserve in same SOC major group until one yields ≥{args.min_items_for_accept} items, OR all reserves exhausted")
    used_reserve_socs: set[str] = set()
    replacements_log = []
    fully_failed_socs: set[str] = set()  # primaries that triggered + all reserves exhausted under-yield

    items_f = items_path.open("a")
    cards_f = cards_path.open("a")

    for primary_soc, stats in list(merged_stats.items()):
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
        if not triggers:
            continue

        grp = primary_soc[:2]
        reserve_pool = reserves.get(grp, [])
        log(f"\n  primary {primary_soc} ({stats.get('occupation','?')}, kept={kept}) under-yielded: triggers=[{', '.join(triggers)}]")
        log(f"  reserves available in SOC {grp}: {len(reserve_pool)}")

        successful_replacement = None
        for r_idx, (r_occ, r_soc) in enumerate(reserve_pool, 1):
            if r_soc in used_reserve_socs:
                continue
            if r_soc in merged_stats:
                continue
            log(f"  trying reserve {r_idx}/{len(reserve_pool)}: {r_soc} ({r_occ})")
            used_reserve_socs.add(r_soc)
            try:
                result = orchestrator.process_occupation(
                    r_occ, r_soc,
                    target_items=args.target, log=log,
                    replacement_for=primary_soc,
                )
            except Exception as e:
                log(f"  !! ERROR running reserve {r_soc}: {e}")
                continue
            r_kept = result["stats"].get("items_kept", 0)
            log(f"  reserve {r_soc} yielded {r_kept} items")
            if r_kept >= args.min_items_for_accept:
                # Success — write items, record, stop trying
                for it in result["items"]:
                    items_f.write(json.dumps(it.to_dict(), ensure_ascii=False) + "\n")
                for c in result["cards"]:
                    cards_f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
                rec_key = f"{r_soc}__replacement_for_{primary_soc}"
                merged_stats[rec_key] = {"occupation": r_occ, **result["stats"]}
                stats_path.write_text(json.dumps(merged_stats, indent=2))
                replacements_log.append({
                    "removed_soc": primary_soc,
                    "removed_occupation": stats.get("occupation", ""),
                    "removed_items_kept": kept,
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
                successful_replacement = (r_occ, r_soc, r_kept)
                log(f"  ✓ replacement successful: {r_soc} → {r_kept} items (≥{args.min_items_for_accept})")
                break
            else:
                # Under-yielded — discard this reserve's items, mark used, try next
                log(f"  reserve {r_soc} under-yielded ({r_kept}<{args.min_items_for_accept}), discarding and trying next")

        if not successful_replacement:
            log(f"  ✗ all reserves exhausted for {primary_soc}; slot will be empty in final bank")
            fully_failed_socs.add(primary_soc)
            replacements_log.append({
                "removed_soc": primary_soc,
                "removed_occupation": stats.get("occupation", ""),
                "removed_items_kept": kept,
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

    items_f.close()
    cards_f.close()

    # Apply strict REPLACE filter — drop under-yield primaries that got replaced
    if replacements_log:
        log("\n========== STRICT REPLACE FILTER ==========")
        result = post_replace.apply_replace_filter(run_dir)
        log(json.dumps(result, indent=2))

    log(f"\n========== RUN END ==========")
    log_f.close()

    final_n = sum(1 for _ in items_path.open())
    print(f"\nDONE. Final bank: {final_n} items at {items_path}")
    print(f"Run dir: {run_dir}")


if __name__ == "__main__":
    main()
