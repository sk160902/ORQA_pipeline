"""Build whitelists for the additional 20 diversity occupations (extension of 30→50).

Loads existing per_occupation_associations_v2.json and adds entries for any
of the 20 extra SOCs not already present.
"""
from __future__ import annotations
import json
import shutil
import sys
import time

from pipeline import config, source_selection

EXTRAS = [
    ("Petroleum Engineers", "17-2171.00"),
    ("Chemical Engineers", "17-2041.00"),
    ("Aerospace Engineers", "17-2011.00"),
    ("Environmental Engineers", "17-2081.00"),
    ("Materials Engineers", "17-2131.00"),
    ("Electronics Engineers, Except Computer", "17-2072.00"),
    ("Mining and Geological Engineers, Including Mining Safety Engineers", "17-2151.00"),
    ("Statisticians", "15-2041.00"),
    ("Network and Computer Systems Administrators", "15-1244.00"),
    ("Web Developers", "15-1254.00"),
    ("Computer Network Architects", "15-1241.00"),
    ("Detectives and Criminal Investigators", "33-3021.00"),
    ("Aviation Inspectors", "53-6051.01"),
    ("Locomotive Engineers", "53-4011.00"),
    ("Geoscientists, Except Hydrologists and Geographers", "19-2042.00"),
    ("Conservation Scientists", "19-1031.00"),
    ("Compliance Officers", "13-1041.00"),
    ("Project Management Specialists", "13-1082.00"),
    ("Graphic Designers", "27-1024.00"),
    ("Occupational Therapists", "29-1122.00"),
]


def main():
    wl_path = config.V2_OUT / "per_occupation_associations_v2.json"
    existing = json.loads(wl_path.read_text())
    print(f"Loaded existing whitelist: {len(existing)} SOCs")

    missing = [(occ, soc) for occ, soc in EXTRAS if soc not in existing]
    print(f"Missing from whitelist: {len(missing)}")
    for occ, soc in missing:
        print(f"  {soc}  {occ}")

    if not missing:
        print("Nothing to build.")
        return 0

    bak = wl_path.with_suffix(".json.bak_pre_diversity50_extras")
    shutil.copy2(wl_path, bak)
    print(f"\nBackup: {bak}")

    print(f"\nBuilding whitelists for {len(missing)} SOCs...")
    t0 = time.time()
    merged = source_selection.build_whitelists(missing, existing=existing)
    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s ({len(merged)} SOCs total)")

    wl_path.write_text(json.dumps(merged, indent=2))
    print(f"Wrote {wl_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
