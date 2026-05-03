"""
Step 37: v6 — generate more multi-correct verified questions from Reddit +
AskMetaFilter (Stack Exchange is still rate-limited). Targets occupations
still under 4 verified multi after v5. Uses expanded subreddit map and
natural-phrasing query generation so reddit search finds threads that
match how practitioners actually describe problems.

Writes:
  - output/qa_gap_v6.json
  - output/ground_truth_review_v6.xlsx
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
OUT_JSON = os.path.join(OUTPUT_DIR, "qa_gap_v6.json")
REVIEW_XLSX = os.path.join(OUTPUT_DIR, "ground_truth_review_v6.xlsx")

# Expanded subreddit map — includes general career/work subreddits as fallbacks
OCC_SUBREDDITS = {
    "Bookkeeping, Accounting, and Auditing Clerks": ["Accounting", "Bookkeeping", "smallbusiness", "QuickBooks"],
    "Tax Preparers": ["tax", "Accounting", "taxpros", "personalfinance"],
    "Loan Officers": ["mortgages", "personalfinance", "CreditCards", "FirstTimeHomeBuyer"],
    "Loan Interviewers and Clerks": ["mortgages", "CreditCards", "personalfinance"],
    "Credit Counselors": ["personalfinance", "CRedit", "CreditCards", "debtfree"],
    "Credit Analysts": ["Accounting", "FinancialCareers", "CreditCards"],
    "Securities, Commodities, and Financial Services Sales Agents": ["investing", "financialindependence", "FinancialCareers", "stocks"],
    "Insurance Sales Agents": ["Insurance", "InsuranceAgent", "personalfinance", "LifeInsurance", "HealthInsurance"],
    "Insurance Claims and Policy Processing Clerks": ["Insurance", "InsuranceClaims", "personalfinance", "CarInsurance"],
    "Tellers": ["Banking", "TalesFromTheFrontDesk", "TellerLife", "personalfinance"],
    "Billing and Posting Clerks": ["medicalbilling", "Accounting", "HealthInsurance"],
    "Payroll and Timekeeping Clerks": ["Payroll", "Accounting", "HumanResources", "smallbusiness"],
    "Procurement Clerks": ["procurement", "supplychain", "logistics"],
    "Legal Secretaries and Administrative Assistants": ["legaladvice", "LawFirm", "paralegal", "LawSchool"],
    "Human Resources Specialists": ["AskHR", "HumanResources", "recruitinghell", "jobs", "careerguidance"],
    "Executive Secretaries and Executive Administrative Assistants": ["AdministrativeAssistants", "AskHR", "jobs", "work"],
    "Web Developers": ["webdev", "web_design", "Frontend", "learnprogramming"],
    "Computer Network Support Specialists": ["sysadmin", "networking", "ITCareerQuestions", "techsupport"],
    "Search Marketing Strategists": ["SEO", "bigseo", "PPC", "marketing", "juststart"],
    "Technical Writers": ["technicalwriting", "writing", "freelanceWriters"],
    "Tutors": ["tutor", "Teachers", "GradSchool", "college"],
    "Mathematicians": ["math", "academia", "PhD", "AskStatistics"],
    "Operations Research Analysts": ["datascience", "OperationsResearch", "learnmath", "AskStatistics"],
    "Statistical Assistants": ["statistics", "datascience", "AskStatistics", "rstats"],
    "Library Technicians": ["Libraries", "librarians"],
    "Dental Assistants": ["Dentistry", "DentalAssistants", "Dentalschool", "askdentists"],
    "Dietitians and Nutritionists": ["Nutrition", "dietetics", "ScienceBasedParenting"],
    "Travel Agents": ["travelagents", "travel", "TravelHacks", "solotravel"],
    "Reservation and Transportation Ticket Agents and Travel Clerks": ["flightattendants", "travelagents", "travel", "airlines"],
    "Hotel, Motel, and Resort Desk Clerks": ["TalesFromTheFrontDesk", "hospitality", "hotels"],
    "Customer Service Representatives": ["TalesFromCallCenters", "callcentres", "CustomerService", "retail"],
    "Telemarketers": ["TalesFromCallCenters", "sales", "callcentres"],
    "Online Merchants": ["Entrepreneur", "Etsy", "ecommerce", "smallbusiness", "FulfillmentByAmazon"],
    "Food Service Managers": ["KitchenConfidential", "restaurateur", "restaurantowners", "bartenders"],
    "Order Clerks": ["customerservice", "retail", "smallbusiness"],
    "File Clerks": ["administrative", "OfficeWorkers", "work"],
    "Data Entry Keyers": ["dataentry", "WorkOnline", "WorkFromHome"],
    "Word Processors and Typists": ["writing", "WorkOnline"],
    "Interviewers, Except Eligibility and Loan": ["AskHR", "recruiting", "jobs"],
    "Logistics Analysts": ["supplychain", "logistics", "Warehousing"],
}

# Always include these broad human-reasoning subreddits as additional fallbacks
GENERAL_WORK_SUBS = ["jobs", "careerguidance", "AskReddit", "askmanagers", "work"]

MULTI_PROMPT = """You are creating a MULTI-CORRECT benchmark that tests occupational reasoning grounded in human practitioner discussion.

**Occupation:** {occupation}
**Task context:** {task}

**Source material** — practitioner discussions (Reddit threads, AskMetaFilter):
---
{source_text}
---

Generate {n_questions} MULTIPLE-CHOICE questions where EACH has EXACTLY 2 or 3 CORRECT options out of A-D. Correct answers must be supported by practitioner reasoning in the sources (top-voted comments, accepted answers, "here's what worked" advice). Single-correct questions are NOT allowed in this batch.

Each question should:
1. Pose a realistic "select all that apply" scenario a {occupation} might face.
2. Have 4 options: 2-3 supported by practitioner reasoning, 1-2 plausible but wrong (common mistakes).
3. Test applied reasoning in ambiguous real situations, NOT trivia.
4. Each correct option must trace to a distinct line of reasoning in the source.

Format: "A,C" or "A,B,D".

Return a JSON array:
[
  {{"question": "...",
    "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
    "correct_answer": "A,C",
    "explanation": "brief",
    "difficulty": "medium" | "hard",
    "question_type": "situational"}}
]

Skip (generate fewer) rather than fabricate. Questions MUST be multi-correct."""


def search_subreddit(subreddit, query, max_results=2):
    try:
        resp = requests.get(
            f"https://www.reddit.com/r/{subreddit}/search.json",
            params={"q": query, "restrict_sr": "1", "sort": "top",
                    "t": "all", "limit": max_results},
            headers={"User-Agent": "OccupationBench/1.0"},
            timeout=10,
        )
        if resp.status_code != 200: return []
        items = resp.json().get("data", {}).get("children", [])
        out = []
        for it in items:
            d = it.get("data", {})
            if d.get("num_comments", 0) < 3: continue
            out.append({"permalink": d.get("permalink", ""),
                        "title": d.get("title", ""),
                        "score": d.get("score", 0),
                        "subreddit": subreddit})
        return out
    except Exception:
        return []


def search_askmefi(query, max_results=3):
    """Search AskMetaFilter via their JSON endpoint (via Jina reader)."""
    try:
        # AskMefi has a search at https://www.metafilter.com/cse/ (Google CSE based)
        # Simpler: use Jina reader on a search URL
        url = f"https://www.metafilter.com/search/index.cfm?q={requests.utils.quote(query)}&exact=0&site=ask"
        txt = gen3.fetch_via_jina(url, max_chars=8000)
        if not txt or len(txt) < 300: return []
        # Extract URLs to ask.metafilter.com threads
        links = re.findall(r'https?://ask\.metafilter\.com/\d+/[^\s)\]"\']+', txt)
        # Dedup
        seen = set()
        uniq = []
        for l in links:
            if l not in seen:
                uniq.append(l); seen.add(l)
            if len(uniq) >= max_results: break
        return uniq
    except Exception:
        return []


def fetch_askmefi(url, max_chars=5000):
    """Fetch an AskMetaFilter thread and include top-rated comments."""
    txt = gen3.fetch_via_jina(url, max_chars=max_chars)
    return txt if txt and len(txt) > 500 else None


def make_queries(client, occupation, task):
    prompt = f"""Generate 5 short search queries (2-3 words each, no full sentences) that a {occupation} might actually search on Reddit or AskMetaFilter when facing this work scenario. Use natural practitioner phrasing, NOT O*NET task language.

Task (in formal O*NET language): {task}

Examples of good queries for different occupations:
- For "rejecting candidates": ["telling candidates no", "candidate rejection email", "ghosting applicants"]
- For "alginate impressions": ["alginate tearing", "dental impression bubbles", "tray fit issues"]

Return ONLY a JSON array of 5 strings, each 2-3 words."""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5, max_tokens=200,
        )
        content = resp.choices[0].message.content.strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"): content = content[4:]
        qs = json.loads(content)
        return [q for q in qs if isinstance(q, str) and 1 <= len(q.split()) <= 5][:5]
    except Exception:
        return [task[:40]]


SE_SITES_BY_OCC = {
    "Bookkeeping, Accounting, and Auditing Clerks": ["money"],
    "Tax Preparers": ["money", "law"],
    "Loan Officers": ["money"],
    "Loan Interviewers and Clerks": ["money"],
    "Credit Counselors": ["money"],
    "Credit Analysts": ["money"],
    "Securities, Commodities, and Financial Services Sales Agents": ["money"],
    "Insurance Sales Agents": ["money", "law"],
    "Insurance Claims and Policy Processing Clerks": ["money", "law"],
    "Tellers": ["money"],
    "Billing and Posting Clerks": ["money"],
    "Payroll and Timekeeping Clerks": ["money"],
    "Procurement Clerks": ["money", "workplace"],
    "Legal Secretaries and Administrative Assistants": ["law", "workplace"],
    "Human Resources Specialists": ["workplace", "law"],
    "Executive Secretaries and Executive Administrative Assistants": ["workplace"],
    "Web Developers": ["stackoverflow", "ux"],
    "Computer Network Support Specialists": ["serverfault", "superuser"],
    "Search Marketing Strategists": ["webmasters"],
    "Technical Writers": ["writing", "workplace"],
    "Tutors": ["academia"],
    "Mathematicians": ["math", "academia"],
    "Operations Research Analysts": ["stats", "academia"],
    "Statistical Assistants": ["stats"],
    "Library Technicians": ["academia"],
    "Dental Assistants": ["health"],
    "Dietitians and Nutritionists": ["health"],
    "Travel Agents": ["travel"],
    "Reservation and Transportation Ticket Agents and Travel Clerks": ["travel"],
    "Hotel, Motel, and Resort Desk Clerks": ["travel", "workplace"],
    "Customer Service Representatives": ["workplace"],
    "Telemarketers": ["workplace"],
    "Online Merchants": ["money", "webmasters"],
    "Food Service Managers": ["workplace"],
    "Order Clerks": ["workplace"],
    "File Clerks": ["workplace"],
    "Data Entry Keyers": ["workplace"],
    "Word Processors and Typists": ["writing", "workplace"],
    "Interviewers, Except Eligibility and Loan": ["workplace"],
    "Logistics Analysts": ["workplace"],
}


def search_stackexchange_with_answers(query, site, max_results=2):
    """Search SE and fetch accepted answer bodies (fixed from v2 bug)."""
    try:
        resp = requests.get(
            "https://api.stackexchange.com/2.3/search/advanced",
            params={"order": "desc", "sort": "relevance", "q": query,
                    "site": site, "filter": "withbody",
                    "pagesize": max_results, "accepted": "True"},
            timeout=10,
        )
        if resp.status_code != 200: return []
        items = resp.json().get("items", [])
        answer_ids = [str(it.get("accepted_answer_id")) for it in items
                      if it.get("accepted_answer_id")]
        answers_by_id = {}
        if answer_ids:
            ar = requests.get(
                f"https://api.stackexchange.com/2.3/answers/{';'.join(answer_ids)}",
                params={"site": site, "filter": "withbody"}, timeout=10,
            )
            if ar.status_code == 200:
                for a in ar.json().get("items", []):
                    body = re.sub(r'<[^>]+>', '', a.get("body", ""))
                    answers_by_id[a.get("answer_id")] = (body, a.get("score", 0))
        results = []
        for item in items:
            q_body = re.sub(r'<[^>]+>', '', item.get("body", ""))
            aid = item.get("accepted_answer_id")
            ans_body, ans_score = answers_by_id.get(aid, ("", 0))
            if not ans_body or len(ans_body) < 200: continue
            combined = (f"Question: {item.get('title','')}\n\n{q_body[:2000]}\n\n"
                        f"--- ACCEPTED ANSWER (score={ans_score}) ---\n{ans_body[:3000]}")
            results.append({"url": item.get("link",""), "title": item.get("title",""),
                            "text": combined, "type": "stackexchange",
                            "subreddit": f"se-{site}",
                            "score": item.get("score",0)})
        return results
    except Exception as e:
        print(f"    SE error: {e}")
        return []


def gather_sources(client, occupation, task, max_sources=5):
    queries = make_queries(client, occupation, task)
    sources = []

    # 1) Stack Exchange — highest-quality accepted answers
    se_sites = SE_SITES_BY_OCC.get(occupation, ["workplace"])
    for site in se_sites[:2]:
        if len(sources) >= max_sources: break
        for q in queries[:3]:
            sources.extend(search_stackexchange_with_answers(q, site, max_results=2))
            if len(sources) >= max_sources: break
        time.sleep(0.2)  # stay under SE rate limit

    # 2) Reddit subreddits + general work subs
    subs = OCC_SUBREDDITS.get(occupation, []) + GENERAL_WORK_SUBS
    for sub in subs[:5]:
        if len(sources) >= max_sources: break
        for q in queries:
            for hit in search_subreddit(sub, q, max_results=2):
                permalink = hit.get("permalink") or ""
                if not permalink: continue
                txt = gen3.fetch_reddit_thread(permalink, max_chars=5000)
                if txt and len(txt) > 800:
                    sources.append({"url": f"https://reddit.com{permalink}",
                                    "title": hit.get("title", ""), "text": txt,
                                    "type": "reddit", "subreddit": sub,
                                    "score": hit.get("score", 0)})
            if len(sources) >= max_sources: break

    # 3) AskMetaFilter tertiary
    if len(sources) < 3:
        for q in queries[:3]:
            for url in search_askmefi(q, max_results=2):
                txt = fetch_askmefi(url)
                if txt:
                    sources.append({"url": url, "title": "", "text": txt,
                                    "type": "askmefi", "subreddit": "askmefi",
                                    "score": 0})
                if len(sources) >= max_sources: break
            if len(sources) >= max_sources: break

    return sources[:max_sources]


def generate_multi(client, occupation, task, sources, n_questions):
    if not sources: return []
    src_text = "\n\n---\n\n".join(
        f"[Source {i+1}: {s['type']} r/{s.get('subreddit','?')} | {s['url']}]\n{s['text']}"
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
            if content.startswith("json"): content = content[4:]
        items = json.loads(content)
        items = [q for q in items if "," in q.get("correct_answer", "")]
        primary = sources[0]
        for q in items:
            q["source_url"] = primary["url"]
            q["source_type"] = primary.get("type", "reddit")
            q["source_subreddit"] = primary.get("subreddit", "")
        return items
    except Exception as e:
        print(f"    QA gen error: {e}")
        return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-per-occ", type=int, default=4)
    args = parser.parse_args()

    client = OpenAI(api_key=open("api_key.txt").read().strip())

    with open(HUMAN_QA) as f:
        verified = json.load(f)
    df = pd.DataFrame([{"occ": q["occupation"], "type": q.get("answer_type","?")} for q in verified])
    multi_per = df[df["type"]=="multi"].groupby("occ").size()

    tasks_df = pd.read_csv(TASKS_CSV)
    with open(os.path.join(OUTPUT_DIR, "qa_40occ_balanced.json")) as f:
        balanced = json.load(f)
    all_occ = sorted({q["occupation"] for q in balanced})

    gaps = []
    for occ in all_occ:
        have = int(multi_per.get(occ, 0))
        gap = max(0, args.target_per_occ - have)
        if gap > 0: gaps.append((occ, have, gap))
    gaps.sort(key=lambda x: -x[2])

    print(f"Occupations needing more multi: {len(gaps)} (target {args.target_per_occ})")
    all_new = []
    for occ, cur, gap in gaps:
        n_to_gen = gap * 3
        occ_tasks = tasks_df[tasks_df["occupation_title"] == occ]
        if occ_tasks.empty: continue
        top = occ_tasks.sort_values("composite_score", ascending=False).head(2)
        per_task = max(3, (n_to_gen + len(top) - 1) // len(top))
        print(f"\n=== {occ} (have {cur} multi, need {gap}, gen ~{n_to_gen}) ===", flush=True)
        produced = 0
        for _, t in top.iterrows():
            if produced >= n_to_gen: break
            print(f"  Task: {t['task_description'][:80]}", flush=True)
            srcs = gather_sources(client, occ, t["task_description"])
            src_breakdown = (f"{sum(1 for s in srcs if s['type']=='stackexchange')}SE/"
                             f"{sum(1 for s in srcs if s['type']=='reddit')}R/"
                             f"{sum(1 for s in srcs if s['type']=='askmefi')}AMF")
            print(f"    sources: {len(srcs)} ({src_breakdown})", flush=True)
            if not srcs: continue
            qa = generate_multi(client, occ, t["task_description"], srcs, per_task)
            for q in qa:
                q["occupation"] = occ
                q["onet_code"] = t.get("onet_code", "")
                q["task_id"] = int(t.get("task_id", 0))
                q["task_description"] = t.get("task_description", "")
                q["correct_answers"] = [c.strip() for c in q["correct_answer"].split(",")]
                q["answer_type"] = "multi"
                q["hardening"] = "gap_v6_reddit_amf"
                all_new.append(q)
            produced += len(qa)
            print(f"    generated {len(qa)} (cum {produced})", flush=True)
            time.sleep(0.4)

    print(f"\nTotal v6 multi questions: {len(all_new)}")
    with open(OUT_JSON, "w") as f:
        json.dump(all_new, f, indent=2)

    rows = []
    for i, q in enumerate(all_new):
        rows.append({
            "idx": i,
            "priority": q.get("source_type", "unknown").upper(),
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
