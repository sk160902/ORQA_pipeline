"""
Step 34: Reddit-first multi-correct generator (replaces v5 web-based approach).

Prioritizes practitioner reasoning from Reddit threads where humans discuss
real work scenarios. Each occupation is paired with relevant subreddits so
the search is contextual rather than generic.

- SE API is rate-limited until tomorrow; skipped.
- No OpenAI web search (which surfaced SEO blog content in v3/v4).
- Falls back to general Reddit search if subreddit-targeted search is thin.

Writes:
  - output/qa_gap_v5.json (multi-correct, reddit-sourced)
  - output/ground_truth_review_v5.xlsx
"""
import os, sys, json, time, re, argparse
import pandas as pd
import requests
from openai import OpenAI

sys.path.insert(0, os.path.dirname(__file__))
import importlib.util
spec = importlib.util.spec_from_file_location("gen3", os.path.join(os.path.dirname(__file__), "3_generate_qa.py"))
gen3 = importlib.util.module_from_spec(spec); spec.loader.exec_module(gen3)

OUTPUT_DIR = "output"
HUMAN_QA = os.path.join(OUTPUT_DIR, "qa_40occ_human_verified.json")
TASKS_CSV = os.path.join(OUTPUT_DIR, "onet_tasks_graded.csv")
OUT_JSON = os.path.join(OUTPUT_DIR, "qa_gap_v5.json")
REVIEW_XLSX = os.path.join(OUTPUT_DIR, "ground_truth_review_v5.xlsx")

# Per-occupation subreddit hints — where practitioners actually discuss their jobs
OCC_SUBREDDITS = {
    "Bookkeeping, Accounting, and Auditing Clerks": ["Accounting", "Bookkeeping", "smallbusiness"],
    "Tax Preparers": ["tax", "Accounting", "taxpros"],
    "Loan Officers": ["mortgages", "personalfinance", "CreditCards"],
    "Loan Interviewers and Clerks": ["mortgages", "CreditCards", "personalfinance"],
    "Credit Counselors": ["personalfinance", "CRedit", "CreditCards"],
    "Credit Analysts": ["Accounting", "FinancialCareers"],
    "Securities, Commodities, and Financial Services Sales Agents": ["investing", "financialindependence", "FinancialCareers"],
    "Insurance Sales Agents": ["Insurance", "InsuranceAgent", "personalfinance"],
    "Insurance Claims and Policy Processing Clerks": ["Insurance", "InsuranceClaims", "personalfinance"],
    "Tellers": ["Banking", "TalesFromTheFrontDesk", "TellerLife"],
    "Billing and Posting Clerks": ["medicalbilling", "Accounting"],
    "Payroll and Timekeeping Clerks": ["Payroll", "Accounting", "HumanResources"],
    "Procurement Clerks": ["procurement", "supplychain"],
    "Legal Secretaries and Administrative Assistants": ["legaladvice", "LawFirm", "paralegal"],
    "Human Resources Specialists": ["AskHR", "HumanResources", "recruitinghell"],
    "Executive Secretaries and Executive Administrative Assistants": ["AdministrativeAssistants", "AskHR", "jobs"],
    "Web Developers": ["webdev", "web_design", "Frontend"],
    "Computer Network Support Specialists": ["sysadmin", "networking", "ITCareerQuestions"],
    "Search Marketing Strategists": ["SEO", "bigseo", "PPC", "marketing"],
    "Technical Writers": ["technicalwriting", "writing"],
    "Tutors": ["tutor", "Teachers", "GradSchool"],
    "Mathematicians": ["math", "academia", "PhD"],
    "Operations Research Analysts": ["datascience", "OperationsResearch", "learnmath"],
    "Statistical Assistants": ["statistics", "datascience", "AskStatistics"],
    "Library Technicians": ["Libraries", "librarians"],
    "Dental Assistants": ["Dentistry", "DentalAssistants", "Dentalschool"],
    "Dietitians and Nutritionists": ["Nutrition", "dietetics", "ScienceBasedParenting"],
    "Travel Agents": ["travelagents", "travel", "TravelHacks"],
    "Reservation and Transportation Ticket Agents and Travel Clerks": ["flightattendants", "travelagents", "travel"],
    "Hotel, Motel, and Resort Desk Clerks": ["TalesFromTheFrontDesk", "hospitality", "hotels"],
    "Customer Service Representatives": ["TalesFromCallCenters", "callcentres", "CustomerService"],
    "Telemarketers": ["TalesFromCallCenters", "sales", "callcentres"],
    "Online Merchants": ["Entrepreneur", "Etsy", "ecommerce", "smallbusiness"],
    "Food Service Managers": ["KitchenConfidential", "restaurateur", "restaurantowners"],
    "Order Clerks": ["customerservice", "retail"],
    "File Clerks": ["administrative", "OfficeWorkers"],
    "Data Entry Keyers": ["dataentry", "WorkOnline"],
    "Word Processors and Typists": ["writing", "WorkOnline"],
    "Interviewers, Except Eligibility and Loan": ["AskHR", "recruiting"],
    "Logistics Analysts": ["supplychain", "logistics", "Warehousing"],
}

MULTI_PROMPT = """You are creating a MULTI-CORRECT multiple-choice benchmark for an occupation-task verification study.

**Occupation:** {occupation}
**Task context:** {task}

**Source material** — Reddit threads where practitioners discuss their work:
---
{source_text}
---

Generate {n_questions} MULTIPLE-CHOICE questions where EACH question has EXACTLY 2 or 3 CORRECT options out of A, B, C, D. The correct answers must all be directly supported by reasoning in the Reddit source above (typically in top-upvoted comments where practitioners explain what works or why). Questions with only 1 correct answer are NOT acceptable.

Each question should:
1. Pose a realistic "select all that apply" type scenario a {occupation} might face.
2. Have 4 options where 2 or 3 are supported by the practitioners' reasoning and 1-2 are plausible but wrong (common mistakes).
3. Test applied reasoning in ambiguous situations, NOT trivia lookup.
4. Ground each correct option in a specific line of reasoning from the source.

Format the correct answer as comma-separated letters: "A,C" or "A,B,D".

Return a JSON array:
[
  {{
    "question": "...",
    "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
    "correct_answer": "A,C",
    "explanation": "which source comments support each correct option; why wrong ones are wrong",
    "difficulty": "medium" | "hard",
    "question_type": "situational"
  }}
]

SKIP (generate fewer) rather than fabricate if the source doesn't support 2+ correct options. Questions MUST be multi-correct."""


def search_subreddit(subreddit, query, max_results=2):
    """Search within a specific subreddit."""
    try:
        resp = requests.get(
            f"https://www.reddit.com/r/{subreddit}/search.json",
            params={"q": query, "restrict_sr": "1", "sort": "top",
                    "t": "all", "limit": max_results},
            headers={"User-Agent": "OccupationBench/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        items = resp.json().get("data", {}).get("children", [])
        results = []
        for it in items:
            d = it.get("data", {})
            if d.get("num_comments", 0) < 3:
                continue  # need real discussion
            results.append({
                "permalink": d.get("permalink", ""),
                "title": d.get("title", ""),
                "score": d.get("score", 0),
            })
        return results
    except Exception as e:
        print(f"    subreddit search {subreddit} error: {e}")
        return []


def gather_reddit_sources(client, occupation, task, max_sources=5):
    queries = make_keyword_queries(client, occupation, task)
    sources = []
    subs = OCC_SUBREDDITS.get(occupation, ["AskReddit"])

    # 1. Targeted subreddit search
    for sub in subs[:3]:
        for q in queries[:3]:
            for hit in search_subreddit(sub, q, max_results=2):
                permalink = hit.get("permalink") or ""
                if not permalink:
                    continue
                txt = gen3.fetch_reddit_thread(permalink, max_chars=5000)
                if txt and len(txt) > 800:
                    sources.append({
                        "url": f"https://reddit.com{permalink}",
                        "title": hit.get("title", ""),
                        "text": txt, "type": "reddit",
                        "subreddit": sub, "score": hit.get("score", 0),
                    })
            if len(sources) >= max_sources:
                break
        if len(sources) >= max_sources:
            break

    # 2. Fallback: general reddit search if subreddit-targeted is thin
    if len(sources) < 2:
        for q in queries[:3]:
            for hit in gen3.search_reddit(q, max_results=2):
                permalink = hit.get("permalink") or ""
                if not permalink:
                    continue
                txt = gen3.fetch_reddit_thread(permalink, max_chars=5000)
                if txt and len(txt) > 800:
                    sources.append({
                        "url": f"https://reddit.com{permalink}",
                        "title": hit.get("title", ""),
                        "text": txt, "type": "reddit",
                        "subreddit": "general", "score": 0,
                    })
            if len(sources) >= max_sources:
                break

    return sources[:max_sources]


def make_keyword_queries(client, occupation, task):
    prompt = f"""Generate 4 short keyword search queries (2-3 words each, no sentences) that would surface practitioner discussions on Reddit about this work scenario.

Occupation: {occupation}
Task: {task}

Return ONLY a JSON array of 4 strings. Each is a 2-3 word phrase (e.g. "rejecting candidates", "denying claim"). NO sentences, NO occupation name."""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4, max_tokens=200,
        )
        content = resp.choices[0].message.content.strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        qs = json.loads(content)
        return [q for q in qs if isinstance(q, str) and 1 <= len(q.split()) <= 4][:4]
    except Exception:
        return [task[:40]]


def generate_multi_qa(client, occupation, task, sources, n_questions):
    if not sources:
        return []
    src_text = "\n\n---\n\n".join(
        f"[Source {i+1}: r/{s.get('subreddit','?')} | score={s.get('score',0)} | {s['url']}]\n{s['text']}"
        for i, s in enumerate(sources)
    )[:20000]
    prompt = MULTI_PROMPT.format(occupation=occupation, task=task,
                                 source_text=src_text, n_questions=n_questions)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5, max_tokens=2500,
        )
        content = resp.choices[0].message.content.strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        items = json.loads(content)
        items = [q for q in items if "," in q.get("correct_answer", "")]
        primary = sources[0]
        for q in items:
            q["source_url"] = primary["url"]
            q["source_type"] = "reddit"
            q["source_subreddit"] = primary.get("subreddit", "")
        return items
    except Exception as e:
        print(f"    QA gen error: {e}")
        return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-per-occ", type=int, default=6)
    parser.add_argument("--max-occupations", type=int, default=None)
    args = parser.parse_args()

    client = OpenAI(api_key=open("api_key.txt").read().strip())

    with open(HUMAN_QA) as f:
        verified = json.load(f)
    df = pd.DataFrame([{"occ": q["occupation"], "type": q.get("answer_type", "?")}
                       for q in verified])
    multi_per_occ = df[df["type"] == "multi"].groupby("occ").size()

    tasks_df = pd.read_csv(TASKS_CSV)

    with open(os.path.join(OUTPUT_DIR, "qa_40occ_balanced.json")) as f:
        balanced = json.load(f)
    all_occupations = sorted({q["occupation"] for q in balanced})

    gaps = []
    for occ in all_occupations:
        have_multi = int(multi_per_occ.get(occ, 0))
        gap = max(0, args.target_per_occ - have_multi)
        if gap > 0:
            gaps.append((occ, have_multi, gap))
    gaps.sort(key=lambda x: -x[2])
    if args.max_occupations:
        gaps = gaps[:args.max_occupations]

    print(f"Occupations needing more multi questions: {len(gaps)}", flush=True)

    all_new = []
    for occ, cur, gap in gaps:
        n_to_gen = gap * 3
        occ_tasks = tasks_df[tasks_df["occupation_title"] == occ]
        if occ_tasks.empty:
            continue
        top = occ_tasks.sort_values("composite_score", ascending=False).head(2)
        per_task = max(3, (n_to_gen + len(top) - 1) // len(top))
        subs = OCC_SUBREDDITS.get(occ, [])
        print(f"\n=== {occ} (have {cur} multi, need {gap}, gen ~{n_to_gen}) "
              f"[subs: {', '.join(subs[:3])}] ===", flush=True)
        produced = 0
        for _, t in top.iterrows():
            if produced >= n_to_gen:
                break
            print(f"  Task: {t['task_description'][:80]}", flush=True)
            srcs = gather_reddit_sources(client, occ, t["task_description"])
            print(f"    reddit sources: {len(srcs)}", flush=True)
            if not srcs:
                continue
            qa = generate_multi_qa(client, occ, t["task_description"], srcs, per_task)
            for q in qa:
                q["occupation"] = occ
                q["onet_code"] = t.get("onet_code", "")
                q["task_id"] = int(t.get("task_id", 0))
                q["task_description"] = t.get("task_description", "")
                q["correct_answers"] = [c.strip() for c in q["correct_answer"].split(",")]
                q["answer_type"] = "multi"
                q["hardening"] = "gap_v5_reddit_multi"
                all_new.append(q)
            produced += len(qa)
            print(f"    generated {len(qa)} (cum {produced})", flush=True)
            time.sleep(0.5)

    print(f"\nTotal new reddit-sourced multi questions: {len(all_new)}")
    with open(OUT_JSON, "w") as f:
        json.dump(all_new, f, indent=2)

    rows = []
    for i, q in enumerate(all_new):
        rows.append({
            "idx": i,
            "priority": "REDDIT",
            "occupation": q.get("occupation", ""),
            "answer_type": q.get("answer_type", ""),
            "question": q.get("question", ""),
            "options": " | ".join(f"{k}) {q.get('options', {}).get(k, '')}" for k in sorted(q.get("options", {}))),
            "labeled_answer": q.get("correct_answer", ""),
            "source_url": q.get("source_url", ""),
            "subreddit": q.get("source_subreddit", ""),
            "verdict": "",
            "corrected_answer": "",
            "notes": "",
        })
    pd.DataFrame(rows).to_excel(REVIEW_XLSX, index=False)
    print(f"Wrote {REVIEW_XLSX}")


if __name__ == "__main__":
    main()
