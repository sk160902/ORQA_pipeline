"""Post-hoc filter: re-check items.jsonl against the broadened giveaway regex
and the source-naming regex. Items where any distractor matches a banned
phrase, or where the correct option names a source document, get moved to
items_giveaway_filtered.jsonl. Surviving items stay in items.jsonl.

Run after the main pipeline if you've broadened the regex and want to
retroactively clean items generated under the older narrower regex.

Usage:
  /usr/local/bin/python3 v2_pipeline/post_filter_giveaway.py --run-dir output/pilot20_parallel_v4
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pipeline.item_generator import _GIVEAWAY_DISTRACTOR_RES, _SOURCE_NAMING_RES


def check_item(item: dict) -> str | None:
    """Returns failure reason if item should be filtered, else None."""
    opts = item.get("options") or {}
    correct = item.get("correct_answer", "")
    # Distractor giveaway check
    for letter in "ABCD":
        if letter == correct:
            continue
        opt = opts.get(letter, "") or ""
        for r in _GIVEAWAY_DISTRACTOR_RES:
            if r.search(opt):
                return f"option_{letter}_giveaway:{r.pattern[:40]}"
    # Correct-option-names-source check
    correct_opt = opts.get(correct, "") or ""
    for r in _SOURCE_NAMING_RES:
        if r.search(correct_opt):
            return f"correct_option_names_source:{r.pattern[:40]}"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    items_path = run_dir / "items.jsonl"
    out_archive = run_dir / "items_giveaway_filtered.jsonl"

    if not items_path.exists():
        print(f"ERROR: {items_path} missing")
        return

    items = []
    for line in items_path.open():
        line = line.strip()
        if line:
            items.append(json.loads(line))

    kept = []
    filtered = []
    reasons = {}
    for it in items:
        reason = check_item(it)
        if reason:
            it["_filter_reason"] = reason
            filtered.append(it)
            key = reason.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
        else:
            kept.append(it)

    items_path.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in kept) + "\n")
    if filtered:
        # Append (don't overwrite) so multiple runs accumulate the archive
        with out_archive.open("a") as f:
            for it in filtered:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")

    print(f"Total items inspected: {len(items)}")
    print(f"Items kept:            {len(kept)}")
    print(f"Items filtered out:    {len(filtered)}")
    if reasons:
        print("\nFilter reasons:")
        for k, n in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {n:>3d}  {k}")
    print(f"\nArchive: {out_archive}")


if __name__ == "__main__":
    main()
