"""Build whitelists for the 16 missing SOCs from the diversity-100 expansion."""
from __future__ import annotations
import json, shutil, sys, time
from pipeline import config, source_selection

EXTRAS = [
    ("Construction Managers", "11-9021.00"),
    ("Property, Real Estate, and Community Association Managers", "11-9141.00"),
    ("Computer Programmers", "15-1251.00"),
    ("Architectural and Civil Drafters", "17-3011.00"),
    ("Mechanical Drafters", "17-3013.00"),
    ("Cartographers and Photogrammetrists", "17-1021.00"),
    ("Sociologists", "19-3041.00"),
    ("Geographers", "19-3092.00"),
    ("Substance Abuse and Behavioral Disorder Counselors", "21-1011.00"),
    ("Adult Basic Education, Adult Secondary Education, and English as a Second Language Instructors", "25-3011.00"),
    ("Photographers", "27-4021.00"),
    ("Dietitians and Nutritionists", "29-1031.00"),
    ("Chiropractors", "29-1011.00"),
    ("Funeral Attendants", "39-4021.00"),
    ("Real Estate Sales Agents", "41-9022.00"),
    ("Bakers", "51-3011.00"),
]


def main():
    wl_path = config.V2_OUT / "per_occupation_associations_v2.json"
    existing = json.loads(wl_path.read_text())
    print(f"Existing whitelist: {len(existing)} SOCs")
    missing = [(occ, soc) for occ, soc in EXTRAS if soc not in existing]
    print(f"Missing whitelists to build: {len(missing)}")
    if not missing:
        return 0
    bak = wl_path.with_suffix(".json.bak_pre_diversity100")
    shutil.copy2(wl_path, bak)
    print(f"Backup: {bak}")
    t0 = time.time()
    merged = source_selection.build_whitelists(missing, existing=existing)
    print(f"Done in {time.time()-t0:.1f}s ({len(merged)} SOCs total)")
    wl_path.write_text(json.dumps(merged, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
