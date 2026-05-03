"""Build diversity-curated reserve pool for plan §3.4 compliance.

Same selection rule as primaries: occupations with rich non-CDC professional
ecosystems and distinct authority bodies. 2-3 reserves per SOC major group
that has primaries in our diversity-50.

Writes:
  - per_occupation_associations_v2.json (whitelist entries for new reserves)
  - reserves_v2_expanded.json (the diversity reserves, replacing wage-bill version)
  - reserves_v2_expanded.json.bak_wagebill (backup of old wage-bill reserves)
"""
from __future__ import annotations
import json
import shutil
import sys
import time

from pipeline import config, source_selection

# Diversity reserves: 2-3 per SOC group, all non-CDC ecosystems
DIVERSITY_RESERVES = {
    "13": [  # Business/Finance — primaries: Accountants, PFA, Tax, Compliance, PMP
        ("Insurance Underwriters", "13-2053.00"),       # CPCU/The Institutes
        ("Logisticians", "13-1081.00"),                 # APICS/CSCMP
        ("Cost Estimators", "13-1051.00"),              # AACE
    ],
    "15": [  # Computer/Math — primaries: Actuaries, Stats, InfoSec, SW Dev, DBA, NSA, Web, NA
        ("Mathematicians", "15-2021.00"),               # AMS/MAA
        ("Operations Research Analysts", "15-2031.00"), # INFORMS
        ("Computer Systems Analysts", "15-1211.00"),    # ACM/IEEE
    ],
    "17": [  # Engineering — primaries: Civil, Mech, Ind, Surveyors, Arch, Petr, Chem, Aero, Env, Mat, Elec, Mining
        ("Marine Engineers and Naval Architects", "17-2121.00"),  # SNAME
        ("Computer Hardware Engineers", "17-2061.00"),            # IEEE
        ("Electrical Engineers", "17-2071.00"),                   # IEEE/NEC
    ],
    "19": [  # Sciences — primaries: Chemists, Hydrologists, Foresters, Geosci, Conservation
        ("Atmospheric and Space Scientists", "19-2021.00"),  # AMS/NWS
        ("Microbiologists", "19-1022.00"),                   # ASM (Microbiology)
        ("Physicists", "19-2012.00"),                        # APS
    ],
    "21": [  # Social Service — primary: Counselors
        ("Marriage and Family Therapists", "21-1013.00"),   # AAMFT
        ("Child, Family, and School Social Workers", "21-1021.00"),  # NASW
    ],
    "25": [  # Education — primaries: Librarians, Special Ed Elementary
        ("Engineering Teachers, Postsecondary", "25-1032.00"),       # IEEE/ABET
        ("Mathematical Science Teachers, Postsecondary", "25-1022.00"),  # AMS
    ],
    "27": [  # Arts/Media — primaries: Court Reporters, Graphic Designers
        ("Editors", "27-3041.00"),                       # ACES
        ("Public Relations Specialists", "27-3031.00"),  # PRSA
        ("Technical Writers", "27-3042.00"),             # STC
    ],
    "29": [  # Healthcare Practitioners — primaries: Optometrists, Pharmacists, SLPs, OTs, Vet Techs
        # All non-CDC (society/cert-board led, not federal CDC)
        ("Athletic Trainers", "29-9091.00"),     # NATA/BOC
        ("Audiologists", "29-1181.00"),          # AAA/ASHA
        ("Physician Assistants", "29-1071.00"),  # AAPA/NCCPA
    ],
    "33": [  # Protective — primaries: Firefighters, Security Guards, Detectives
        ("Fire Inspectors and Investigators", "33-2021.00"),               # NFPA/IAAI
        ("Correctional Officers and Jailers", "33-3012.00"),               # ACA
        ("First-Line Supervisors of Police and Detectives", "33-1012.00"), # IACP
    ],
    "47": [  # Construction — primary: Operating Engineers
        ("Electricians", "47-2111.00"),                              # NEC/NECA/IBEW
        ("Plumbers, Pipefitters, and Steamfitters", "47-2152.00"),  # UA/IAPMO/ASPE
        ("Carpenters", "47-2031.00"),                                # UBC
    ],
    "49": [  # Maintenance — primaries: Aircraft Mech, HVAC, Auto Service
        ("Industrial Machinery Mechanics", "49-9041.00"),                       # SMRP
        ("Bus and Truck Mechanics and Diesel Engine Specialists", "49-3031.00"), # ASE/ATD
        ("Wind Turbine Service Technicians", "49-9081.00"),                     # NABCEP/AWEA
    ],
    "51": [  # Production — primary: Welders
        ("Machinists", "51-4041.00"),                                                                  # NIMS
        ("Inspectors, Testers, Sorters, Samplers, and Weighers", "51-9061.00"),                       # ASQ
        ("Tool and Die Makers", "51-4111.00"),                                                         # NTMA
    ],
    "53": [  # Transport — primaries: Aviation Inspectors, Locomotive Engineers
        ("Airline Pilots, Copilots, and Flight Engineers", "53-2011.00"),  # FAA/ALPA
        ("Air Traffic Controllers", "53-2021.00"),                          # FAA/NATCA
        ("Captains, Mates, and Pilots of Water Vessels", "53-5021.00"),    # USCG/AMO
    ],
}


def main():
    # Step 1: validate all reserve SOCs against O*NET DB
    onet_path = "/Users/Shreyas2/Desktop/Berkeley/occupation_task/data/db_29_1_text/Occupation Data.txt"
    onet_socs, onet_titles = set(), {}
    with open(onet_path) as f:
        next(f)
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                onet_socs.add(parts[0]); onet_titles[parts[0]] = parts[1]

    all_pairs = []
    invalid = []
    for grp, lst in DIVERSITY_RESERVES.items():
        for occ, soc in lst:
            if soc not in onet_socs:
                invalid.append((occ, soc))
            else:
                all_pairs.append((occ, soc))

    if invalid:
        print("INVALID SOCs (not in O*NET):")
        for occ, soc in invalid:
            print(f"  {soc}  {occ}")
        return 1

    print(f"All {len(all_pairs)} reserve SOCs validated against O*NET ✓")

    # Step 2: build whitelists for reserves not yet in whitelist
    wl_path = config.V2_OUT / "per_occupation_associations_v2.json"
    existing = json.loads(wl_path.read_text())
    print(f"Existing whitelist: {len(existing)} SOCs")

    missing = [(occ, soc) for occ, soc in all_pairs if soc not in existing]
    print(f"Missing whitelists: {len(missing)}")
    if missing:
        bak = wl_path.with_suffix(".json.bak_pre_diversity_reserves")
        shutil.copy2(wl_path, bak)
        print(f"Whitelist backup: {bak}")
        t0 = time.time()
        merged = source_selection.build_whitelists(missing, existing=existing)
        print(f"Whitelist build done in {time.time()-t0:.1f}s ({len(merged)} SOCs total)")
        wl_path.write_text(json.dumps(merged, indent=2))

    # Step 3: backup wage-bill reserves and write diversity reserves
    res_path = config.V2_OUT / "reserves_v2_expanded.json"
    if res_path.exists():
        bak = res_path.with_suffix(".json.bak_wagebill")
        shutil.copy2(res_path, bak)
        print(f"\nWage-bill reserves backup: {bak}")

    diversity_serializable = {
        grp: [{"occupation": occ, "soc": soc} for occ, soc in lst]
        for grp, lst in DIVERSITY_RESERVES.items()
    }
    res_path.write_text(json.dumps(diversity_serializable, indent=2))
    print(f"Wrote diversity reserves: {res_path}")
    n_res = sum(len(v) for v in DIVERSITY_RESERVES.values())
    print(f"  {n_res} reserves across {len(DIVERSITY_RESERVES)} SOC major groups")

    return 0


if __name__ == "__main__":
    sys.exit(main())
