"""Parallel diversity-50 run: 50 curated non-CDC-dominated occupations.

Differences from launch_pilot20_parallel.py:
  - Reads from output/pilot20_v2_chunk.csv (now 50 SOCs)
  - Uses wage-bill reserves (reserves_v2_expanded.json)
  - Hard ≥15 floor cleanup at the end: drops any occupation under 15 items
    from final bank (whether primary or replacement)
  - Writes final bank with publisher distribution

Usage:
  /usr/local/bin/python3 launch_diversity50.py [--workers 8] [--target 20] [--min-items 15]
"""
from __future__ import annotations
import argparse
import csv
import json
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--target", type=int, default=20)
    ap.add_argument("--min-items", type=int, default=15, help="replacement trigger threshold (SOCs below this trigger reserves); see --floor for final cleanup")
    ap.add_argument("--floor", type=int, default=None, help="final cleanup floor; SOCs below this are dropped from final bank (default: same as --min-items)")
    ap.add_argument("--run-name", default=None)
    ap.add_argument("--resume", default=None,
                    help="resume an existing run by name (e.g. --resume diversity50_20260430_180000). "
                         "Workers will skip SOCs that already have stats entries in their dirs.")
    args = ap.parse_args()

    if args.resume:
        # Resume mode: re-use the existing run dir + existing chunk files
        name = args.resume
        run_dir = ROOT / "output" / name
        if not run_dir.exists():
            print(f"ERROR: --resume {name} run dir does not exist at {run_dir}")
            sys.exit(1)
        print(f"RESUMING run {name}")
    else:
        name = args.run_name or f"diversity50_{time.strftime('%Y%m%d_%H%M%S')}"
        run_dir = ROOT / "output" / name
        run_dir.mkdir(parents=True, exist_ok=True)

    chunk_csv = ROOT / "output" / "pilot20_v2_chunk.csv"
    if not chunk_csv.exists():
        print(f"ERROR: missing {chunk_csv}.")
        sys.exit(1)
    occupations = []
    with chunk_csv.open() as f:
        for r in csv.DictReader(f):
            occupations.append((r["occupation_title"], r["soc_code"]))
    print(f"Loaded {len(occupations)} diversity occupations.")

    if args.resume:
        # Use existing chunk files (don't re-split or rewrite)
        existing_chunks = sorted(run_dir.glob("chunk_*.csv"))
        chunks = []
        for p in existing_chunks:
            chunk = []
            with p.open() as f:
                for r in csv.DictReader(f):
                    chunk.append((r["occupation_title"], r["soc_code"]))
            chunks.append(chunk)
        print(f"Resume: found {len(chunks)} existing chunks: {[len(c) for c in chunks]}")
        # Report per-worker resume status
        for i, chunk in enumerate(chunks):
            wd = run_dir / f"worker_{i}"
            stats_path = wd / "stats.json"
            if stats_path.exists():
                try:
                    sts = json.loads(stats_path.read_text())
                    done = sum(1 for soc, st in sts.items()
                               if "discovery_rounds" in st or "items_kept" in st)
                    print(f"  worker {i}: {done}/{len(chunk)} SOCs already done (will skip)")
                except Exception:
                    pass
    else:
        chunks = split_round_robin(occupations, args.workers)
        chunks = [c for c in chunks if c]
        print(f"Split into {len(chunks)} chunks: {[len(c) for c in chunks]}")

        for i, chunk in enumerate(chunks):
            write_chunk_csv(run_dir / f"chunk_{i}.csv", chunk)

    # Launch workers (they skip replacement; we run it post-merge)
    pids = []
    for i in range(len(chunks)):
        worker_dir = f"{name}/worker_{i}"
        stdout_path = run_dir / f"worker_{i}.stdout"
        cmd = [
            PY, "-u", str(ROOT / "run_pilot20.py"),
            "--chunk-file", str(run_dir / f"chunk_{i}.csv"),
            "--target", str(args.target),
            "--skip-replacement",
            "--run-name", worker_dir,
        ]
        with stdout_path.open("w") as out:
            p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, cwd=str(ROOT))
        pids.append(p)
        print(f"  worker {i}: PID {p.pid}  ({len(chunks[i])} occupations)  → {worker_dir}/")
        time.sleep(2)

    print(f"\nAll {len(pids)} workers launched.")
    print(f"Monitor: tail -f {run_dir}/worker_*.stdout")
    for i, p in enumerate(pids):
        rc = p.wait()
        print(f"  worker {i}: exited rc={rc}")

    # Merge worker outputs
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
                merged_stats.update(json.loads(ws.read_text()))
    stats_path.write_text(json.dumps(merged_stats, indent=2))
    n_items = sum(1 for _ in items_path.open())
    print(f"  merged: {n_items} items, {len(merged_stats)} occupation stat entries")

    # ===== Replacement pass with wage-bill reserves =====
    print("\nRunning replacement coordinator (wage-bill reserves)...")
    sys.path.insert(0, str(ROOT))
    from pipeline import config, orchestrator, post_replace

    wagebill_assocs = config.V2_OUT / "per_occupation_associations_v2.json"
    if wagebill_assocs.exists():
        config.PER_OCC_ASSOCIATIONS = wagebill_assocs

    # Prefer expanded reserves (6 per SOC group) over base (2 per group)
    reserves_path = config.V2_OUT / "reserves_v2_expanded.json"
    if not reserves_path.exists():
        reserves_path = config.V2_OUT / "reserves_v2.json"
    reserves: dict = {}
    if reserves_path.exists():
        raw = json.loads(reserves_path.read_text())
        for grp, lst in raw.items():
            reserves[grp] = [(it["occupation"], it["soc"]) for it in lst]
        print(f"  loaded reserves from {reserves_path.name}: {sum(len(v) for v in reserves.values())} reserves across {len(reserves)} SOC groups")

    # Reconstruct primary_items keyed by SOC for trigger checks
    primary_items = defaultdict(list)
    for line in items_path.open():
        line = line.strip()
        if not line: continue
        d = json.loads(line)
        class _ItemShim:
            def __init__(self, d):
                self.publisher = d.get("publisher", "")
                self.source_domain = d.get("source_domain", "")
                self.source_url = d.get("source_url", "")
                self.source_quote = d.get("source_quote", "")
        primary_items[d["onet_soc_code"]].append(_ItemShim(d))

    log_path = run_dir / "replacement_pass.log"
    log_f = log_path.open("w")
    def log(msg):
        print(msg)
        log_f.write(msg + "\n")
        log_f.flush()

    # Skip SOCs we already populated as primaries (don't replay them as reserves)
    used_reserve_socs: set[str] = set(merged_stats.keys())
    replacements_log = []
    fully_failed_socs: set[str] = set()

    log(f"\n========== PARALLEL ITERATIVE REPLACEMENT PASS ==========")
    log(f"Workers: {args.workers}  |  Triggers: items<{args.min_items} OR top-pub>70% OR n_pubs<2 OR n_docs<3")
    log(f"Strategy: try each reserve in same SOC major group until one yields ≥{args.min_items}, OR all reserves exhausted")

    # Collect under-yield slots
    needs_replacement = []
    for primary_soc, stats in list(merged_stats.items()):
        if "__replacement_for_" in primary_soc:
            continue
        kept = stats.get("items_kept", 0)
        occ_items = primary_items.get(primary_soc, [])
        top_share, n_pubs, n_docs = orchestrator._publisher_concentration(occ_items)
        triggers = []
        if kept < args.min_items:
            triggers.append(f"items={kept}<{args.min_items}")
        if kept > 0 and top_share > 0.7:
            triggers.append(f"top_pub={top_share*100:.0f}%>70%")
        if kept > 0 and n_pubs < 2:
            triggers.append(f"n_pubs={n_pubs}<2")
        if kept > 0 and n_docs < 3:
            triggers.append(f"n_docs={n_docs}<3")
        if triggers:
            needs_replacement.append((primary_soc, stats, top_share, n_pubs, n_docs, triggers))

    log(f"Under-yield slots needing replacement: {len(needs_replacement)}")

    # Thread-safe state
    reserve_lock = threading.Lock()
    write_lock = threading.Lock()
    stats_lock = threading.Lock()
    repl_log_lock = threading.Lock()
    items_f = items_path.open("a")
    cards_f = cards_path.open("a")

    def _try_one(primary_soc, stats, top_share, n_pubs, n_docs, triggers):
        grp = primary_soc[:2]
        reserve_pool = reserves.get(grp, [])
        log(f"\n  [{primary_soc}] {stats.get('occupation','?')[:40]} kept={stats.get('items_kept',0)} "
            f"triggers=[{', '.join(triggers)}]; reserves SOC {grp}: {len(reserve_pool)}")

        for r_idx, (r_occ, r_soc) in enumerate(reserve_pool, 1):
            with reserve_lock:
                if r_soc in used_reserve_socs:
                    continue
                if r_soc in merged_stats:
                    continue
                used_reserve_socs.add(r_soc)
            log(f"  [{primary_soc}] trying reserve {r_idx}/{len(reserve_pool)}: {r_soc} ({r_occ})")
            try:
                result = orchestrator.process_occupation(
                    r_occ, r_soc, target_items=args.target, log=log,
                    replacement_for=primary_soc,
                )
            except Exception as e:
                log(f"  [{primary_soc}] !! ERROR running reserve {r_soc}: {e}")
                continue
            r_kept = result["stats"].get("items_kept", 0)
            log(f"  [{primary_soc}] reserve {r_soc} yielded {r_kept} items")
            if r_kept >= args.min_items:
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
                log(f"  [{primary_soc}] ✓ replacement successful: {r_soc} → {r_kept} items")
                return
            else:
                log(f"  [{primary_soc}] reserve {r_soc} under-yielded ({r_kept}<{args.min_items}), trying next")

        log(f"  [{primary_soc}] ✗ all reserves exhausted; primary will be dropped by ≥{args.min_items} floor")
        fully_failed_socs.add(primary_soc)
        with repl_log_lock:
            replacements_log.append({
                "removed_soc": primary_soc,
                "removed_occupation": stats.get("occupation", ""),
                "removed_items_kept": stats.get("items_kept", 0),
                "replacement_soc": None,
                "replacement_occupation": None,
                "soc_major_group": grp,
                "replacement_items_kept": 0,
                "replacement_reason": "; ".join(triggers) + "; all reserves exhausted",
                "reserves_tried": len(reserve_pool),
            })
            (run_dir / "replacements.json").write_text(json.dumps(replacements_log, indent=2))

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(_try_one, *u) for u in needs_replacement]
        for f in as_completed(futures):
            try: f.result()
            except Exception as e: log(f"  !! worker exception: {e}")

    items_f.close(); cards_f.close()

    # Strict REPLACE filter: drop under-yield primaries that got replaced
    if replacements_log:
        log("\n========== STRICT REPLACE FILTER ==========")
        result = post_replace.apply_replace_filter(run_dir)
        log(json.dumps(result, indent=2))

    # ===== Final ≥15 hard floor cleanup =====
    floor_value = args.floor if args.floor is not None else args.min_items
    log(f"\n========== ≥{floor_value} HARD FLOOR CLEANUP ==========")
    items_by_soc: dict[str, list[dict]] = defaultdict(list)
    cards_by_soc: dict[str, list[dict]] = defaultdict(list)
    for line in items_path.open():
        line = line.strip()
        if not line: continue
        d = json.loads(line)
        items_by_soc[d["onet_soc_code"]].append(d)
    for line in cards_path.open():
        line = line.strip()
        if not line: continue
        d = json.loads(line)
        cards_by_soc[d["onet_soc_code"]].append(d)

    kept_socs, dropped_socs = [], []
    for soc, its in items_by_soc.items():
        if len(its) >= floor_value:
            kept_socs.append(soc)
        else:
            dropped_socs.append((soc, len(its)))

    log(f"Kept ({len(kept_socs)}):")
    for soc in sorted(kept_socs):
        occ = merged_stats.get(soc, {}).get("occupation", "?")
        log(f"  {soc}  {len(items_by_soc[soc]):3d} items  {occ}")
    log(f"\nDropped ({len(dropped_socs)}):")
    for soc, n in sorted(dropped_socs, key=lambda x: -x[1]):
        occ = merged_stats.get(soc, {}).get("occupation", "?")
        log(f"  {soc}  {n:3d} items  {occ}")

    with items_path.open("w") as f:
        for soc in kept_socs:
            for it in items_by_soc[soc]:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
    with cards_path.open("w") as f:
        for soc in kept_socs:
            for c in cards_by_soc.get(soc, []):
                f.write(json.dumps(c, ensure_ascii=False) + "\n")

    final_stats = {soc: merged_stats.get(soc, {}) for soc in kept_socs}
    stats_path.write_text(json.dumps(final_stats, indent=2))

    drop_log = run_dir / "dropped_under_floor.json"
    drop_log.write_text(json.dumps([
        {"soc": soc, "items_kept": n, "occupation": merged_stats.get(soc, {}).get("occupation", "?")}
        for soc, n in dropped_socs
    ], indent=2))

    final_n = sum(1 for _ in items_path.open())
    log(f"\n========== FINAL BANK ==========")
    log(f"SOCs: {len(kept_socs)}")
    log(f"Items: {final_n}")

    pubs = Counter()
    for soc in kept_socs:
        for it in items_by_soc[soc]:
            pubs[it.get("publisher", "?")] += 1
    log(f"\nTop 15 publishers:")
    for pub, n in pubs.most_common(15):
        pct = 100 * n / final_n if final_n else 0
        log(f"  {n:3d}  ({pct:5.1f}%)  {pub}")

    pub_dist_path = run_dir / "publisher_distribution.json"
    pub_dist_path.write_text(json.dumps({
        "total_items": final_n,
        "total_socs": len(kept_socs),
        "publishers": [{"publisher": p, "n": n, "pct": round(100*n/final_n, 2) if final_n else 0}
                       for p, n in pubs.most_common()],
    }, indent=2))
    log_f.close()
    print(f"\nWrote {pub_dist_path}")
    print(f"Run dir: {run_dir}")


if __name__ == "__main__":
    main()
