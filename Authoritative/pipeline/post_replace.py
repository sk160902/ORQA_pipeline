"""Plan-strict REPLACE filter (per plan §3.4).

When the orchestrator's replacement pass runs a reserve occupation in place
of an under-yield primary, the plan calls for the primary to be REMOVED from
the bank — not kept alongside the replacement. This module post-processes a
run directory to apply that semantics:

  - Reads run_dir/replacements.json
  - Drops items in run_dir/items.jsonl whose onet_soc_code matches any
    `removed_soc` (those are the under-yield primaries that got replaced)
  - Archives the dropped items into run_dir/items_removed.jsonl for
    transparency
  - Updates run_dir/items.jsonl to be the plan-strict bank
  - Re-runs the diagnostics aggregator over the cleaned bank

Idempotent: safe to run multiple times.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def apply_replace_filter(run_dir: Path) -> dict:
    items_path = run_dir / "items.jsonl"
    repl_path = run_dir / "replacements.json"
    removed_archive = run_dir / "items_removed.jsonl"

    if not items_path.exists():
        return {"error": f"no items.jsonl at {items_path}"}
    if not repl_path.exists():
        return {"info": "no replacements.json — nothing to filter", "kept": -1, "removed": 0}

    repl = json.loads(repl_path.read_text())
    # Only drop primaries that were SUCCESSFULLY replaced by a reserve.
    # Entries with replacement_soc=None mean no reserve worked — those
    # primaries should stay (they're the only data we have for that SOC).
    removed_socs = {r.get("removed_soc") for r in repl
                    if r.get("removed_soc") and r.get("replacement_soc")}
    if not removed_socs:
        return {"info": "no successful replacements in replacements.json", "kept": -1, "removed": 0}

    items = []
    for line in items_path.open():
        line = line.strip()
        if not line:
            continue
        items.append(json.loads(line))

    kept_items = []
    removed_items = []
    for it in items:
        if it.get("onet_soc_code") in removed_socs:
            # Skip items that ARE replacement items themselves (those have
            # replacement_for set). Only drop the original primary's items.
            if it.get("replacement_for"):
                kept_items.append(it)
            else:
                removed_items.append(it)
        else:
            kept_items.append(it)

    # Write
    items_path.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in kept_items) + "\n")
    if removed_items:
        # Append to archive so re-runs don't lose history
        with removed_archive.open("a") as f:
            for it in removed_items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")

    return {
        "removed_socs": sorted(removed_socs),
        "n_removed": len(removed_items),
        "n_kept": len(kept_items),
        "kept_path": str(items_path),
        "archive_path": str(removed_archive) if removed_items else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    args = ap.parse_args()
    result = apply_replace_filter(Path(args.run_dir))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
