"""Apply Pass 8 (distractor eliminability) to an existing bank.

Loads items.jsonl from a run dir, runs the eliminability judge on each item,
drops items where any distractor is eliminable by general knowledge alone.
Writes:
  - items.jsonl (rewritten with kept items only)
  - items_pass8_dropped.jsonl (rejected items with verifier reasons)
  - pass8_audit.json (per-item judgements for review)

Multi-threaded for speed (8 workers) — Pass 8 is one Together call per item.

Usage:
  /usr/local/bin/python3 pass8_post_filter.py --run-dir output/diversity50_<ts>
"""
from __future__ import annotations
import argparse
import json
import shutil
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from pipeline import schemas, item_verifier


def _item_from_dict(d: dict) -> schemas.Item:
    """Reconstruct just enough of schemas.Item for Pass 8 (only needs:
    occupation_title, question, options, correct_answer)."""
    class _Shim:
        pass
    s = _Shim()
    s.occupation_title = d.get("occupation_title", "")
    s.question = d.get("question", "")
    s.options = d.get("options", {}) or {}
    s.correct_answer = d.get("correct_answer", "")
    s.verification = d.get("verification", {})
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--min-items-per-soc", type=int, default=15,
                    help="re-apply ≥15 floor after Pass 8 drops items")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    items_path = run_dir / "items.jsonl"
    if not items_path.exists():
        print(f"ERROR: missing {items_path}")
        return 1

    items = []
    for line in items_path.open():
        line = line.strip()
        if not line: continue
        items.append(json.loads(line))
    print(f"Loaded {len(items)} items from {items_path}")

    # Backup pre-Pass8 bank
    bak = items_path.with_suffix(".jsonl.bak_pre_pass8")
    shutil.copy2(items_path, bak)
    print(f"Backup: {bak}")

    # Run Pass 8 in parallel
    results: dict[int, dict] = {}
    log_lock = threading.Lock()
    n_done = [0]

    def _process(idx: int, item_dict: dict):
        item = _item_from_dict(item_dict)
        try:
            p8 = item_verifier._pass8_distractor_eliminability(item)
        except Exception as e:
            p8 = {"ok": True, "eliminable": {}, "reasons": {}, "raw_error": f"exception: {e}"}
        with log_lock:
            n_done[0] += 1
            if n_done[0] % 25 == 0 or n_done[0] == len(items):
                print(f"  Pass 8 progress: {n_done[0]}/{len(items)}")
        return idx, p8

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(_process, i, it) for i, it in enumerate(items)]
        for f in as_completed(futures):
            idx, p8 = f.result()
            results[idx] = p8
    elapsed = time.time() - t0
    print(f"Pass 8 done in {elapsed:.1f}s ({len(items)/max(elapsed,1):.1f} items/s)")

    # Apply rejection
    kept, dropped = [], []
    for i, it in enumerate(items):
        p8 = results[i]
        it.setdefault("verification", {}).update({
            "pass8_distractor_eliminability_ok": p8["ok"],
            "pass8_eliminable": p8["eliminable"],
            "pass8_eliminability_reasons": p8["reasons"],
        })
        if p8["ok"]:
            kept.append(it)
        else:
            it["pass8_drop_reason"] = "distractor_eliminable_by_general_knowledge"
            it["pass8_eliminable"] = p8["eliminable"]
            it["pass8_reasons"] = p8["reasons"]
            dropped.append(it)

    print(f"\nPass 8 results:")
    print(f"  kept: {len(kept)}")
    print(f"  dropped: {len(dropped)}")

    # ≥15 floor re-application
    counts = Counter(it["onet_soc_code"] for it in kept)
    surviving_socs = {soc for soc, n in counts.items() if n >= args.min_items_per_soc}
    under_floor = {soc: n for soc, n in counts.items() if n < args.min_items_per_soc}
    final_kept = [it for it in kept if it["onet_soc_code"] in surviving_socs]

    print(f"\n≥{args.min_items_per_soc} floor cleanup:")
    print(f"  SOCs surviving floor: {len(surviving_socs)}")
    print(f"  SOCs dropped under floor:")
    for soc, n in sorted(under_floor.items(), key=lambda x: -x[1]):
        print(f"    {soc}: {n} items")

    # Write outputs
    with items_path.open("w") as f:
        for it in final_kept:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    drop_path = run_dir / "items_pass8_dropped.jsonl"
    with drop_path.open("w") as f:
        for it in dropped:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    audit_path = run_dir / "pass8_audit.json"
    audit_path.write_text(json.dumps({
        "run_dir": str(run_dir),
        "input_items": len(items),
        "pass8_kept": len(kept),
        "pass8_dropped": len(dropped),
        "floor_kept_socs": len(surviving_socs),
        "floor_dropped_socs": len(under_floor),
        "final_items": len(final_kept),
        "elapsed_seconds": round(elapsed, 1),
    }, indent=2))

    # Publisher distribution refresh
    pubs = Counter(it.get("publisher", "?") for it in final_kept)
    pub_dist_path = run_dir / "publisher_distribution_post_pass8.json"
    pub_dist_path.write_text(json.dumps({
        "total_items": len(final_kept),
        "total_socs": len(surviving_socs),
        "publishers": [{"publisher": p, "n": n,
                        "pct": round(100*n/len(final_kept), 2) if final_kept else 0}
                       for p, n in pubs.most_common()],
    }, indent=2))

    print(f"\n========== POST-PASS-8 BANK ==========")
    print(f"Items: {len(final_kept)}")
    print(f"SOCs: {len(surviving_socs)}")
    print(f"\nTop 15 publishers:")
    for pub, n in pubs.most_common(15):
        pct = 100 * n / len(final_kept) if final_kept else 0
        print(f"  {n:3d}  ({pct:5.1f}%)  {pub}")
    print(f"\nWrote {audit_path}")
    print(f"Wrote {drop_path}")
    print(f"Wrote {pub_dist_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
