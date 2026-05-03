"""Build whitelists for the 30 diversity-curated occupations.

Loads existing per_occupation_associations_v2.json and adds entries for any
of the 30 target SOCs not already present. Writes back in place (with backup).
"""
from __future__ import annotations
import json
import shutil
import sys
import time
from pathlib import Path

from pipeline import config, source_selection

TARGETS = [
    ("Welders, Cutters, Solderers, and Brazers", "51-4121.00"),
    ("Aircraft Mechanics and Service Technicians", "49-3011.00"),
    ("Heating, Air Conditioning, and Refrigeration Mechanics and Installers", "49-9021.00"),
    ("Automotive Service Technicians and Mechanics", "49-3023.00"),
    ("Civil Engineers", "17-2051.00"),
    ("Mechanical Engineers", "17-2141.00"),
    ("Industrial Engineers", "17-2112.00"),
    ("Surveyors", "17-1022.00"),
    ("Architects, Except Landscape and Naval", "17-1011.00"),
    ("Operating Engineers and Other Construction Equipment Operators", "47-2073.00"),
    ("Accountants and Auditors", "13-2011.00"),
    ("Personal Financial Advisors", "13-2052.00"),
    ("Actuaries", "15-2011.00"),
    ("Tax Preparers", "13-2082.00"),
    ("Court Reporters and Simultaneous Captioners", "27-3092.00"),
    ("Information Security Analysts", "15-1212.00"),
    ("Software Developers", "15-1252.00"),
    ("Database Administrators", "15-1242.00"),
    ("Chemists", "19-2031.00"),
    ("Hydrologists", "19-2043.00"),
    ("Foresters", "19-1032.00"),
    ("Firefighters", "33-2011.00"),
    ("Security Guards", "33-9032.00"),
    ("Librarians and Media Collections Specialists", "25-4022.00"),
    ("Educational, Guidance, and Career Counselors and Advisors", "21-1012.00"),
    ("Special Education Teachers, Elementary School", "25-2056.00"),
    ("Optometrists", "29-1041.00"),
    ("Pharmacists", "29-1051.00"),
    ("Speech-Language Pathologists", "29-1127.00"),
    ("Veterinary Technologists and Technicians", "29-2056.00"),
]


def main():
    wl_path = config.V2_OUT / "per_occupation_associations_v2.json"
    existing = json.loads(wl_path.read_text())
    print(f"Loaded existing whitelist: {len(existing)} SOCs")

    missing = [(occ, soc) for occ, soc in TARGETS if soc not in existing]
    print(f"Missing from whitelist: {len(missing)}")
    for occ, soc in missing:
        print(f"  {soc}  {occ}")

    if not missing:
        print("Nothing to build.")
        return 0

    # Backup
    bak = wl_path.with_suffix(".json.bak_pre_diversity30")
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
