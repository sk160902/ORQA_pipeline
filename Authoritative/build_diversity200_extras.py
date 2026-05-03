"""Build whitelists for the 73 missing SOCs in the diversity-200 expansion."""
from __future__ import annotations
import json, shutil, sys, time, csv
from pipeline import config, source_selection


def main():
    wl_path = config.V2_OUT / "per_occupation_associations_v2.json"
    existing = json.loads(wl_path.read_text())
    print(f"Existing whitelist: {len(existing)} SOCs")

    pairs = []
    with open(config.V2_OUT / "pilot20_v2_chunk.csv") as f:
        for r in csv.DictReader(f):
            pairs.append((r["occupation_title"], r["soc_code"]))
    missing = [(o, s) for o, s in pairs if s not in existing]
    print(f"Missing whitelists to build: {len(missing)}")

    if not missing:
        return 0
    bak = wl_path.with_suffix(".json.bak_pre_diversity200")
    shutil.copy2(wl_path, bak)
    print(f"Backup: {bak}")

    t0 = time.time()
    merged = source_selection.build_whitelists(missing, existing=existing)
    print(f"\nDone in {time.time()-t0:.1f}s ({len(merged)} SOCs total)")

    wl_path.write_text(json.dumps(merged, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
