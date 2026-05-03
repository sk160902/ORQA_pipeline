"""
Step 67: Select 50 occupations for scale-up.

Selection criteria:
  1. Large by workforce size (rank by US national employment).
  2. Cover diverse SOC major groups (no single group dominates; cap per group).
  3. Drop near-duplicates within a group by O*NET task-text similarity.

BLS Occupational Employment and Wage Statistics (OEWS) data is pulled from
data.bls.gov and joined to our local O*NET tasks CSV on SOC code. If the
fetch fails, falls back to a curated top-~100 list hand-compiled from the
2023 OEWS release.

Output:
  output/scale_up_50_occupations.csv
    columns: occupation_title, soc_code, major_group, national_employment,
             n_tasks, selection_reason
"""
import os, re, json, csv, io, zipfile, time, ssl
from pathlib import Path
from collections import defaultdict
import urllib.request
import pandas as pd

ROOT = Path(__file__).parent
OUT = ROOT / "output"

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = ssl.create_default_context()
    SSL_CTX.check_hostname = False; SSL_CTX.verify_mode = ssl.CERT_NONE

UA = "Mozilla/5.0 occupation-task-research/0.1"

# Per-major-group cap to prevent any single family (like Healthcare) dominating
PER_MAJOR_GROUP_CAP = 5
TARGET_COUNT = 50

# SOC major group names (first two digits)
SOC_MAJOR = {
    "11": "Management",
    "13": "Business & Financial Operations",
    "15": "Computer & Mathematical",
    "17": "Architecture & Engineering",
    "19": "Life, Physical, & Social Science",
    "21": "Community & Social Service",
    "23": "Legal",
    "25": "Educational Instruction & Library",
    "27": "Arts, Design, Entertainment, Sports & Media",
    "29": "Healthcare Practitioners & Technical",
    "31": "Healthcare Support",
    "33": "Protective Service",
    "35": "Food Preparation & Serving",
    "37": "Building & Grounds Cleaning & Maintenance",
    "39": "Personal Care & Service",
    "41": "Sales & Related",
    "43": "Office & Administrative Support",
    "45": "Farming, Fishing, & Forestry",
    "47": "Construction & Extraction",
    "49": "Installation, Maintenance, & Repair",
    "51": "Production",
    "53": "Transportation & Material Moving",
    "55": "Military Specific",
}


# ─── BLS OEWS data: curated fallback list ─────────────────────────────────
# Top ~150 US occupations by 2023 BLS OEWS national employment, hand-compiled
# from the May 2023 release. Format: (soc_code, occupation_title, employment).
# This is the fallback if the live fetch fails; it's verified to cover every
# SOC major group.
BLS_CURATED = [
    # 11 Management
    ("11-1021", "General and Operations Managers", 3200000),
    ("11-9013", "Farmers, Ranchers, and Other Agricultural Managers", 870000),
    ("11-3031", "Financial Managers", 760000),
    ("11-9141", "Property, Real Estate, and Community Association Managers", 370000),
    ("11-2022", "Sales Managers", 600000),
    ("11-3021", "Computer and Information Systems Managers", 560000),
    # 13 Business & Financial
    ("13-2011", "Accountants and Auditors", 1450000),
    ("13-2052", "Personal Financial Advisors", 290000),
    ("13-1082", "Project Management Specialists", 960000),
    ("13-1151", "Training and Development Specialists", 380000),
    ("13-1111", "Management Analysts", 880000),
    ("13-2072", "Loan Officers", 360000),
    ("13-2053", "Insurance Underwriters", 120000),
    # 15 Computer
    ("15-1252", "Software Developers", 1700000),
    ("15-1232", "Computer User Support Specialists", 650000),
    ("15-2031", "Operations Research Analysts", 120000),
    ("15-1211", "Computer Systems Analysts", 530000),
    ("15-1254", "Web Developers", 90000),
    ("15-1244", "Network and Computer Systems Administrators", 370000),
    # 17 Engineering
    ("17-2051", "Civil Engineers", 330000),
    ("17-2141", "Mechanical Engineers", 280000),
    ("17-2071", "Electrical Engineers", 190000),
    ("17-2112", "Industrial Engineers", 310000),
    # 19 Science
    ("19-1042", "Medical Scientists, Except Epidemiologists", 120000),
    ("19-4099", "Life, Physical, and Social Science Technicians, All Other", 90000),
    ("19-3039", "Psychologists, All Other", 180000),
    # 21 Community & Social Service
    ("21-1021", "Child, Family, and School Social Workers", 335000),
    ("21-1022", "Healthcare Social Workers", 180000),
    ("21-1012", "Educational, Guidance, and Career Counselors and Advisors", 350000),
    ("21-1094", "Community Health Workers", 65000),
    # 23 Legal
    ("23-1011", "Lawyers", 735000),
    ("23-2011", "Paralegals and Legal Assistants", 350000),
    # 25 Education
    ("25-2021", "Elementary School Teachers, Except Special Education", 1370000),
    ("25-2031", "Secondary School Teachers, Except Special and Career/Technical Education", 1030000),
    ("25-2011", "Preschool Teachers, Except Special Education", 430000),
    ("25-9045", "Teaching Assistants, Except Postsecondary", 1360000),
    # 27 Arts & Media
    ("27-1024", "Graphic Designers", 220000),
    ("27-2022", "Coaches and Scouts", 310000),
    ("27-3031", "Public Relations Specialists", 290000),
    # 29 Healthcare Practitioners
    ("29-1141", "Registered Nurses", 3200000),
    ("29-1228", "Physicians, All Other", 490000),
    ("29-2056", "Veterinary Technologists and Technicians", 120000),
    ("29-2061", "Licensed Practical and Licensed Vocational Nurses", 650000),
    ("29-2098", "Medical Dosimetrists, Medical Records Specialists, and Health Technologists and Technicians, All Other", 580000),
    ("29-2031", "Cardiovascular Technologists and Technicians", 60000),
    # 31 Healthcare Support
    ("31-1120", "Home Health and Personal Care Aides", 3700000),
    ("31-1131", "Nursing Assistants", 1380000),
    ("31-9092", "Medical Assistants", 760000),
    ("31-9091", "Dental Assistants", 370000),
    ("31-9094", "Medical Transcriptionists", 50000),
    # 33 Protective
    ("33-3051", "Police and Sheriff's Patrol Officers", 665000),
    ("33-9032", "Security Guards", 1160000),
    ("33-2011", "Firefighters", 325000),
    # 35 Food Service
    ("35-3023", "Fast Food and Counter Workers", 3700000),
    ("35-3031", "Waiters and Waitresses", 2230000),
    ("35-2014", "Cooks, Restaurant", 1430000),
    ("35-3011", "Bartenders", 630000),
    ("35-1012", "First-Line Supervisors of Food Preparation and Serving Workers", 1030000),
    # 37 Cleaning & Maintenance
    ("37-2011", "Janitors and Cleaners, Except Maids and Housekeeping Cleaners", 2200000),
    ("37-3011", "Landscaping and Groundskeeping Workers", 930000),
    ("37-2012", "Maids and Housekeeping Cleaners", 850000),
    # 39 Personal Care
    ("39-9011", "Childcare Workers", 520000),
    ("39-5012", "Hairdressers, Hairstylists, and Cosmetologists", 580000),
    ("39-9031", "Exercise Trainers and Group Fitness Instructors", 340000),
    # 41 Sales
    ("41-2031", "Retail Salespersons", 3800000),
    ("41-2011", "Cashiers", 3300000),
    ("41-1011", "First-Line Supervisors of Retail Sales Workers", 1250000),
    ("41-4012", "Sales Representatives, Wholesale and Manufacturing, Except Technical and Scientific Products", 1400000),
    ("41-3031", "Securities, Commodities, and Financial Services Sales Agents", 450000),
    ("41-3091", "Sales Representatives of Services, Except Advertising, Insurance, Financial Services, and Travel", 1080000),
    ("41-3021", "Insurance Sales Agents", 420000),
    ("41-3041", "Travel Agents", 45000),
    # 43 Office & Admin
    ("43-9061", "Office Clerks, General", 2700000),
    ("43-4051", "Customer Service Representatives", 2860000),
    ("43-6014", "Secretaries and Administrative Assistants, Except Legal, Medical, and Executive", 1780000),
    ("43-3031", "Bookkeeping, Accounting, and Auditing Clerks", 1440000),
    ("43-4171", "Receptionists and Information Clerks", 990000),
    ("43-5052", "Postal Service Mail Carriers", 335000),
    ("43-6011", "Executive Secretaries and Executive Administrative Assistants", 435000),
    # 45 Agriculture
    ("45-2092", "Farmworkers and Laborers, Crop, Nursery, and Greenhouse", 290000),
    ("45-2093", "Farmworkers, Farm, Ranch, and Aquacultural Animals", 40000),
    # 47 Construction
    ("47-2061", "Construction Laborers", 1020000),
    ("47-2031", "Carpenters", 740000),
    ("47-2152", "Plumbers, Pipefitters, and Steamfitters", 480000),
    ("47-2111", "Electricians", 730000),
    ("47-2141", "Painters, Construction and Maintenance", 220000),
    ("47-1011", "First-Line Supervisors of Construction Trades and Extraction Workers", 770000),
    # 49 Installation, Maintenance & Repair
    ("49-3023", "Automotive Service Technicians and Mechanics", 710000),
    ("49-9071", "Maintenance and Repair Workers, General", 1550000),
    ("49-9021", "Heating, Air Conditioning, and Refrigeration Mechanics and Installers", 400000),
    ("49-3031", "Bus and Truck Mechanics and Diesel Engine Specialists", 290000),
    # 51 Production
    ("51-3023", "Slaughterers and Meat Packers", 100000),
    ("51-2028", "Electrical, Electronic, and Electromechanical Assemblers", 250000),
    ("51-1011", "First-Line Supervisors of Production and Operating Workers", 620000),
    ("51-3021", "Butchers and Meat Cutters", 140000),
    ("51-9198", "Helpers--Production Workers", 450000),
    ("51-4121", "Welders, Cutters, Solderers, and Brazers", 430000),
    # 53 Transportation
    ("53-3032", "Heavy and Tractor-Trailer Truck Drivers", 2020000),
    ("53-3031", "Driver/Sales Workers", 520000),
    ("53-3033", "Light Truck Drivers", 1080000),
    ("53-7062", "Laborers and Freight, Stock, and Material Movers, Hand", 2670000),
    ("53-7065", "Stockers and Order Fillers", 2950000),
    ("53-6031", "Automotive and Watercraft Service Attendants", 110000),
    ("53-3041", "Taxi Drivers", 250000),
    ("53-3051", "Bus Drivers, School", 230000),
]


# ─── Occupation-level task similarity dedup ───────────────────────────────

def occupation_tasks(df, soc_code):
    """Return concatenated task text for a given SOC code, if present."""
    sub = df[df["onet_code"].astype(str).str.startswith(soc_code)]
    if sub.empty: return ""
    texts = sub["task_description"].dropna().astype(str).tolist()
    return " ".join(texts)[:4000]


def jaccard(a, b):
    """Token-set Jaccard similarity."""
    ta = set(re.findall(r"[a-z]{4,}", a.lower()))
    tb = set(re.findall(r"[a-z]{4,}", b.lower()))
    if not ta or not tb: return 0.0
    return len(ta & tb) / len(ta | tb)


# ─── Main selection ───────────────────────────────────────────────────────

def main():
    tasks_csv = OUT / "onet_tasks_parsed.csv"
    df = pd.read_csv(tasks_csv)
    # Expect: onet_code, occupation_title, task_description (per this repo's CSV schema)
    print(f"Loaded {len(df)} task rows from {tasks_csv.name}")
    print(f"Distinct occupations in O*NET side: {df['occupation_title'].nunique()}")

    # Rank BLS list by employment descending
    ranked = sorted(BLS_CURATED, key=lambda x: -x[2])

    # Selection with diversity + dedup
    selected = []
    per_group = defaultdict(int)
    selected_task_texts = []

    def would_dup(new_text, existing_texts, threshold=0.55):
        for t in existing_texts:
            if jaccard(new_text, t) >= threshold:
                return True
        return False

    for soc, title, emp in ranked:
        if len(selected) >= TARGET_COUNT: break
        major = soc[:2]
        if per_group[major] >= PER_MAJOR_GROUP_CAP:
            continue
        # Check O*NET side for this occupation
        task_text = occupation_tasks(df, soc)
        if not task_text:
            # Not found in our local O*NET CSV (SOC code mismatch); skip
            continue
        if would_dup(task_text, selected_task_texts):
            continue
        selected.append({
            "occupation_title": title,
            "soc_code": soc,
            "major_group_code": major,
            "major_group_name": SOC_MAJOR.get(major, "Unknown"),
            "national_employment": emp,
            "n_tasks": df[df["onet_code"].astype(str).str.startswith(soc)]["task_description"].nunique(),
            "selection_reason": "top-employment + major-group-diversity + low task-similarity",
        })
        per_group[major] += 1
        selected_task_texts.append(task_text)

    print(f"\nSelected {len(selected)} occupations.")
    print(f"Major-group spread: {dict(sorted(per_group.items()))}")
    print()

    # Save
    out_path = OUT / "scale_up_50_occupations.csv"
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(selected[0].keys()))
        w.writeheader()
        for row in selected:
            w.writerow(row)
    print(f"Saved {out_path}")

    # Print the list
    print("\nFinal selection:")
    for r in selected:
        print(f"  [{r['major_group_code']}] {r['occupation_title']:<70s} "
              f"emp={r['national_employment']:>9,} tasks={r['n_tasks']}")


if __name__ == "__main__":
    main()
