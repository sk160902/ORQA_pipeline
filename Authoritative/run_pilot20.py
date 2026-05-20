"""Top-level entry point: run the v2 pipeline.

Two modes:
  --legacy-pilot20         use the original pilot 20 list (for A/B vs the 294 bank)
  --wagebill (default)     use the wage-bill weighted top 20 + replacement loop

Outputs land in v2_pipeline/output/<run-name>/
"""
from __future__ import annotations
import argparse
import csv
import json
import time
from pathlib import Path

from pipeline import config
from pipeline import clients
from pipeline import orchestrator


def _load_wagebill_inputs():
    """Load the wage-bill primary list + reserves (built by build_v2_whitelists.py)."""
    primary_csv = config.V2_OUT / "pilot20_v2_chunk.csv"
    reserves_json = config.V2_OUT / "reserves_v2.json"
    associations_json = config.V2_OUT / "per_occupation_associations_v2.json"
    if not primary_csv.exists() or not associations_json.exists():
        raise SystemExit(
            f"Wage-bill inputs not found. Run:\n"
            f"  /usr/local/bin/python3 v2_pipeline/build_v2_whitelists.py"
        )
    primary = []
    with primary_csv.open() as f:
        for r in csv.DictReader(f):
            primary.append((r["occupation_title"], r["soc_code"]))
    reserves = {}
    if reserves_json.exists():
        raw = json.loads(reserves_json.read_text())
        for grp, lst in raw.items():
            reserves[grp] = [(it["occupation"], it["soc"]) for it in lst]
    return primary, reserves


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=config.TARGET_ITEMS_PER_OCCUPATION,
                    help="items per occupation (default 20)")
    ap.add_argument("--smoke", action="store_true",
                    help="run on Dental Hygienists with target=3 to smoke test")
    ap.add_argument("--legacy-pilot20", action="store_true",
                    help="use the legacy pilot 20 list instead of wage-bill selection")
    ap.add_argument("--chunk-file", default=None,
                    help="CSV with occupation_title,soc_code — overrides occupation list (used by parallel workers)")
    ap.add_argument("--skip-replacement", action="store_true",
                    help="skip replacement pass (used by workers; coordinator runs it after merge)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap number of occupations")
    ap.add_argument("--run-name", default=None,
                    help="override output dir name")
    ap.add_argument("--min-items-for-accept", type=int, default=15,
                    help="under this triggers replacement loop (default 15)")
    args = ap.parse_args()

    reserves = {}
    if args.chunk_file:
        # Worker mode: read the chunk CSV, use the wage-bill v2 whitelist
        occupations = []
        with open(args.chunk_file) as f:
            for r in csv.DictReader(f):
                occupations.append((r["occupation_title"], r["soc_code"]))
        target = args.target
        # Switch to wage-bill whitelist (workers running on wage-bill SOCs)
        wagebill_assocs = config.V2_OUT / "per_occupation_associations_v2.json"
        if wagebill_assocs.exists():
            config.PER_OCC_ASSOCIATIONS = wagebill_assocs
        # Workers don't run replacement pass — coordinator does it after merge
        if not args.skip_replacement:
            _, reserves = _load_wagebill_inputs()
    elif args.smoke:
        occupations = [(o, s) for o, s in config.PILOT_20 if s == "29-1292.00"]
        target = 3
    elif args.legacy_pilot20:
        occupations = list(config.PILOT_20)
        target = args.target
        if args.limit:
            occupations = occupations[:args.limit]
    else:
        # Wage-bill (default)
        occupations, reserves = _load_wagebill_inputs()
        target = args.target
        if args.limit:
            occupations = occupations[:args.limit]
        wagebill_assocs = config.V2_OUT / "per_occupation_associations_v2.json"
        if wagebill_assocs.exists():
            config.PER_OCC_ASSOCIATIONS = wagebill_assocs

    if args.skip_replacement:
        reserves = {}

    name = args.run_name or ("smoke" if args.smoke else f"pilot20_{time.strftime('%Y%m%d_%H%M%S')}")
    run_dir = config.V2_OUT / name

    status = clients.provider_status()
    print(f"Provider keys loaded: {status}")
    print(f"Serper key present:   {bool(config.SERPER_KEY)}")
    print(f"ScrapingBee present:  {bool(config.SCRAPINGBEE_KEY)}")
    print(f"Whitelist file:       {config.PER_OCC_ASSOCIATIONS.name}")
    print(f"Run dir:              {run_dir}")
    print(f"Occupations:          {len(occupations)}  target items: {target}")
    print(f"Reserves available:   {sum(len(v) for v in reserves.values())} across {len(reserves)} SOC groups")

    if not config.SERPER_KEY:
        raise SystemExit(
            "ERROR: api_key_serper.txt is missing or empty."
        )

    orchestrator.run(occupations, run_dir=run_dir, target_items=target,
                     reserves=reserves, min_items_for_accept=args.min_items_for_accept)
    print(f"\nDONE — see {run_dir}")


if __name__ == "__main__":
    main()
