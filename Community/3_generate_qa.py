"""
Step 3: Generate verifiable Q&A for selected O*NET tasks.

Pipeline:
  1. Select top-scoring tasks from the grading step (or manually specify occupations)
  2. For each task, search the web for trusted sources
  3. Scrape/extract content from those sources
  4. Use an LLM to generate verifiable Q&A pairs with ground truth answers and citations
  5. Output a structured Q&A bank as JSON/CSV

This is the core deliverable: a pipeline that generates verifiable Q&A
from O*NET occupation tasks at scale.

Usage:
    export OPENAI_API_KEY=sk-...
    python3 3_generate_qa.py [--n-occupations 5] [--questions-per-task 5]
"""

import pandas as pd
import json
import os
import sys
import time
import argparse
import re
import requests
from openai import OpenAI

OUTPUT_DIR = "output"

# ─── Default occupations to target (diverse set) ───
DEFAULT_OCCUPATIONS = [
    "Dental Assistants",
    "Library Technicians",
    "Travel Agents",
    "Dietitians and Nutritionists",
    "Operations Research Analysts",
    "Bookkeeping, Accounting, and Auditing Clerks",
    "Customer Service Representatives",
    "Insurance Claims and Policy Processing Clerks",
    "Paralegals and Legal Assistants",
    "Medical Records Specialists",
    "Tax Preparers",
    "Tutors",
    "Financial Analysts",
    "Market Research Analysts",
    "Web Developers",
    "Technical Writers",
    "Human Resources Specialists",
    "Loan Officers",
    "Statisticians",
    "Logisticians",
]

# ─── Multi-source content gathering ───
# Strategy:
#   1. OpenAI web search (primary) — finds professional/gov sources with citations
#   2. PubMed API — for medical/health occupation tasks
#   3. Wikipedia API — fallback for general knowledge
# Key: each question should come from a DIFFERENT source to avoid repetition.

WIKI_HEADERS = {"User-Agent": "OccupationBenchmark/1.0 (research project)"}


def search_openai_web(client, query, model="gpt-4o-mini"):
    """Use OpenAI web search to FIND URLs (not for content — we fetch raw text separately)."""
    try:
        response = client.responses.create(
            model=model,
            tools=[{"type": "web_search_preview"}],
            input=f"Find professional sources, guidelines, or standards related to: {query}. "
                  f"List the most relevant URLs you find.",
        )
        urls = []
        for item in response.output:
            if hasattr(item, "content"):
                for c in item.content:
                    if hasattr(c, "annotations"):
                        for ann in c.annotations:
                            if hasattr(ann, "url"):
                                urls.append(ann.url)
            if hasattr(item, "url"):
                urls.append(item.url)
        # Clean URLs (remove utm params)
        clean_urls = []
        for u in urls:
            u = re.sub(r'\?utm_source=openai.*', '', u)
            if u not in clean_urls:
                clean_urls.append(u)
        return clean_urls
    except Exception as e:
        print(f"    OpenAI web search error: {e}")
        return []


def search_reddit(query, max_results=3):
    """Search Reddit for Q&A threads where practitioners discuss real work scenarios."""
    try:
        resp = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": query, "limit": 25, "sort": "relevance", "type": "link"},
            headers={"User-Agent": "OccupationBenchmark/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        posts = resp.json().get("data", {}).get("children", [])
        results = []
        for p in posts:
            d = p["data"]
            selftext = d.get("selftext", "")
            title = d.get("title", "")
            # Filter for Q&A style posts: must have substantive text,
            # comments with answers, and look like a work question
            is_question = any(w in title.lower() for w in
                            ["how", "what", "help", "advice", "question",
                             "should i", "can i", "anyone", "tips", "issue",
                             "problem", "struggling", "new to"])
            has_content = len(selftext) > 100
            has_answers = d.get("num_comments", 0) >= 2
            if is_question and has_content and has_answers:
                results.append({
                    "url": f"https://www.reddit.com{d['permalink']}",
                    "title": title,
                    "text": f"Question: {title}\n\n{selftext}",
                    "subreddit": d.get("subreddit", ""),
                })
            if len(results) >= max_results:
                break
        return results
    except Exception as e:
        print(f"    Reddit search error: {e}")
        return []


def fetch_reddit_thread(permalink, max_chars=6000):
    """Fetch a Reddit thread including top comments (the human reasoning answers)."""
    try:
        url = f"https://www.reddit.com{permalink}.json?limit=10"
        resp = requests.get(
            url,
            headers={"User-Agent": "OccupationBenchmark/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        # First element is the post, second is comments
        post_data = data[0]["data"]["children"][0]["data"]
        text = f"Question: {post_data.get('title', '')}\n\n{post_data.get('selftext', '')}\n\n"
        text += "--- Practitioner Answers ---\n\n"
        if len(data) > 1:
            comments = data[1]["data"]["children"]
            for c in comments[:5]:  # Top 5 answers
                if c["kind"] == "t1":
                    body = c["data"].get("body", "")
                    score = c["data"].get("score", 0)
                    if len(body) > 50:
                        text += f"[Upvotes: {score}] {body}\n\n"
        return text[:max_chars]
    except Exception as e:
        print(f"    Reddit thread fetch error: {e}")
        return ""


def search_stackexchange(query, site="workplace", max_results=3):
    """Search Stack Exchange for Q&A with accepted answers, then fetch the
    accepted-answer body so the generator sees actual authoritative text,
    not just the question body."""
    try:
        resp = requests.get(
            "https://api.stackexchange.com/2.3/search/advanced",
            params={
                "order": "desc", "sort": "relevance",
                "q": query, "site": site,
                "filter": "withbody", "pagesize": max_results,
                "accepted": "True",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        items = resp.json().get("items", [])
        # Collect accepted_answer_ids to fetch in one call
        answer_ids = [str(it.get("accepted_answer_id"))
                      for it in items if it.get("accepted_answer_id")]
        answers_by_id = {}
        if answer_ids:
            ans_resp = requests.get(
                f"https://api.stackexchange.com/2.3/answers/{';'.join(answer_ids)}",
                params={"site": site, "filter": "withbody"},
                timeout=10,
            )
            if ans_resp.status_code == 200:
                for a in ans_resp.json().get("items", []):
                    body = re.sub(r'<[^>]+>', '', a.get("body", ""))
                    answers_by_id[a.get("answer_id")] = {
                        "body": body,
                        "score": a.get("score", 0),
                    }

        results = []
        for item in items:
            q_body = re.sub(r'<[^>]+>', '', item.get("body", ""))
            aid = item.get("accepted_answer_id")
            ans = answers_by_id.get(aid, {})
            ans_body = ans.get("body", "")
            combined = (
                f"Question: {item.get('title','')}\n\n{q_body}\n\n"
                f"--- ACCEPTED ANSWER (score={ans.get('score', 0)}) ---\n{ans_body}"
            )
            results.append({
                "url": item.get("link", ""),
                "title": item.get("title", ""),
                "text": combined,
                "score": item.get("score", 0),
                "has_accepted_answer": bool(ans_body),
            })
        # Prefer threads where we actually got the accepted answer body
        results.sort(key=lambda r: (not r["has_accepted_answer"], -r["score"]))
        return results
    except Exception as e:
        print(f"    StackExchange search error: {e}")
        return []


def fetch_via_jina(url, max_chars=6000):
    """Fetch clean text from any URL via Jina Reader API.

    Jina Reader renders pages like a real browser and returns clean text.
    Free, no API key, bypasses bot-blocking.
    This gives us RAW SOURCE TEXT (not LLM-mediated summaries) — critical
    for ground truth integrity.

    Includes validation to detect error pages, 404s, redirects, and
    other non-content responses that would produce invalid ground truth.
    """
    try:
        jina_url = f"https://r.jina.ai/{url}"
        resp = requests.get(
            jina_url,
            headers={"Accept": "text/plain"},
            timeout=20,
        )
        if resp.status_code != 200 or len(resp.text) < 200:
            return ""

        text = resp.text

        # Detect error pages, 404s, and non-content responses
        error_indicators = [
            "page you're looking for was not found",
            "page not found",
            "404 not found",
            "404 error",
            "http error 404",
            "this page doesn't exist",
            "this page has been removed",
            "this page is no longer available",
            "this page can't be found",
            "this page cannot be found",
            "the page has moved",
            "content not available",
            "access denied",
            "403 forbidden",
            "sorry, we couldn't find",
            "the requested url was not found",
            "page has been archived",
            "this content has been removed",
            "bad gateway",
            "502 bad gateway",
            "503 service unavailable",
            "500 internal server error",
            "server error",
            "no web page was found",
            "web page not available",
        ]
        text_lower = text[:2000].lower()
        for indicator in error_indicators:
            if indicator in text_lower:
                print(f"      INVALID: detected error page ({indicator})")
                return ""

        # Check that the content is substantive, not just navigation/boilerplate/tracking
        # A real content page should have actual sentences, not just
        # headers, tracking pixels, URLs, and navigation elements
        stripped = text
        # Remove markdown image tags, links, URLs, and headers
        stripped = re.sub(r'!\[.*?\]\(.*?\)', '', stripped)
        stripped = re.sub(r'\[.*?\]\(.*?\)', '', stripped)
        stripped = re.sub(r'https?://\S+', '', stripped)
        stripped = re.sub(r'#{1,6}\s.*', '', stripped)
        stripped = re.sub(r'[|_*\-=]+', ' ', stripped)
        stripped = re.sub(r'\s+', ' ', stripped).strip()
        if len(stripped) < 500:
            print(f"      INVALID: content too thin after stripping boilerplate ({len(stripped)} chars)")
            return ""

        # Check for actual sentences (at least a few periods indicating real prose)
        sentence_count = stripped.count('.')
        if sentence_count < 3:
            print(f"      INVALID: no real sentences found ({sentence_count} periods)")
            return ""

        # Check for pages that are mostly navigation/site chrome
        nav_words = ["log in", "register", "membership", "search", "cookie",
                     "sign up", "subscribe", "newsletter", "menu", "navigation",
                     "currency", "cart", "checkout"]
        nav_count = sum(1 for w in nav_words if w in stripped.lower()[:500])
        if nav_count >= 4:
            print(f"      INVALID: appears to be site navigation/chrome ({nav_count} nav words)")
            return ""

        # Check for blocked/error responses from Jina
        if "you've been blocked" in text_lower or "target url returned error" in text_lower:
            print(f"      INVALID: Jina was blocked from fetching this URL")
            return ""

        return text[:max_chars]
    except Exception as e:
        print(f"      Jina fetch error for {url[:60]}: {e}")
        return ""


def search_pubmed(query, max_results=3):
    """Search PubMed for academic/medical sources. Free, no key needed."""
    try:
        # Search for paper IDs
        resp = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"db": "pubmed", "term": query, "retmax": max_results, "retmode": "json"},
            timeout=10,
        )
        ids = resp.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []

        # Fetch abstracts
        resp2 = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml"},
            timeout=10,
        )
        titles = re.findall(r"<ArticleTitle>(.*?)</ArticleTitle>", resp2.text)
        abstracts = re.findall(r"<AbstractText[^>]*>(.*?)</AbstractText>", resp2.text, re.DOTALL)

        sources = []
        for i, (pmid, title) in enumerate(zip(ids, titles)):
            abstract = abstracts[i] if i < len(abstracts) else ""
            # Clean XML tags from abstract
            abstract = re.sub(r"<[^>]+>", "", abstract)
            sources.append({
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "query": query,
                "text": f"Title: {title}\n\nAbstract: {abstract}",
                "source_type": "pubmed",
            })
        return sources
    except Exception as e:
        print(f"    PubMed search error: {e}")
        return []


def search_wikipedia(query, num_results=3):
    """Search Wikipedia via its public API."""
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query", "list": "search",
                "srsearch": query, "format": "json", "srlimit": num_results,
            },
            headers=WIKI_HEADERS, timeout=10,
        )
        results = resp.json().get("query", {}).get("search", [])
        sources = []
        for r in results:
            title = r["title"]
            text = fetch_wikipedia_content(title)
            if text:
                sources.append({
                    "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                    "query": query,
                    "text": text,
                    "source_type": "wikipedia",
                })
        return sources
    except Exception as e:
        print(f"    Wikipedia search error: {e}")
        return []


def fetch_wikipedia_content(title, max_chars=6000):
    """Fetch plain-text content of a Wikipedia article via API."""
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query", "titles": title,
                "prop": "extracts", "explaintext": True, "format": "json",
            },
            headers=WIKI_HEADERS, timeout=10,
        )
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            text = page.get("extract", "")
            if text:
                return text[:max_chars]
        return ""
    except Exception as e:
        return ""


def search_web(query, num_results=5):
    """Legacy interface — returns URLs. Used by gather_sources."""
    sources = search_wikipedia(query, num_results=num_results)
    return [s["url"] for s in sources]


def fetch_page_text(url, max_chars=8000):
    """Fetch and extract text content from a URL."""
    try:
        wiki_match = re.match(r"https?://en\.wikipedia\.org/wiki/(.+)", url)
        if wiki_match:
            title = wiki_match.group(1).replace("_", " ")
            text = fetch_wikipedia_content(title, max_chars=max_chars)
            if text:
                return text

        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        text = resp.text
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except Exception as e:
        print(f"    Fetch error for {url}: {e}")
        return ""


# ─── LLM-based Q&A generation ───

SOURCE_SEARCH_PROMPT = """You are helping create a benchmark for evaluating AI models on occupation-specific tasks.

Occupation: {occupation}
Task: {task}

I need to find verifiable, factual information related to this task. Generate 5 specific search queries that would find:
- Professional guidelines or standards for this task
- Textbook-level factual information
- Regulatory requirements or best practices
- Quantitative data (measurements, dosages, thresholds, procedures)
- Q&A forums where professionals discuss this task

Return ONLY a JSON array of 5 search query strings. Example:
["query 1", "query 2", "query 3", "query 4", "query 5"]"""


QA_GENERATION_PROMPT = """You are creating a benchmark to evaluate how well AI models can assist workers on the job.

**Occupation:** {occupation}
**Task:** {task}

**Source material** (from trusted web sources):
---
{source_text}
---

Your job is to find SPECIFIC TECHNICAL FACTS in the source material that are NOT common sense, and build a question around each fact.

First, read the source material carefully and identify a fact that meets ALL of these criteria:
- It is COUNTER-INTUITIVE or SURPRISING (someone without training would guess wrong)
- It involves a specific number, threshold, material, procedure, or exception
- A non-expert would likely pick a different answer that sounds equally reasonable

Then, construct a work scenario where a {occupation} encounters a situation that hinges on that fact.

Here is how to think about it:

STEP 1: Find a specific technical fact in the source. Examples of good facts:
- "Alginate sets faster in high humidity, so use cooler water to compensate" (counter-intuitive: you might think warmer water helps)
- "Biological indicators should be used at least weekly, not just when the chemical indicator fails" (surprising frequency requirement)
- "Flash sterilization should not be used for implantable devices" (specific exception)
- "The water-to-powder ratio for dental stone is 30 mL per 100 g, not the 50 mL ratio used for plaster" (specific number that is easy to confuse)

STEP 2: Build a realistic work scenario around that fact where a worker needs to make a decision.

STEP 3: Create four options where:
- The CORRECT answer reflects the specific technical fact from the source
- The WRONG answers reflect what someone with general knowledge but no specific training would choose
- All four options use professional language and sound equally plausible
- The wrong answers should be COMMON MISTAKES that less experienced workers actually make

GOOD example (dental assistant):
Fact from source: "Humidity accelerates alginate setting time. Use cooler water to compensate."
Question: "A patient's alginate impression keeps tearing when I remove it. The tray fits and I am using the correct water-to-powder ratio, but it is very humid today. What should I adjust?"
A) Use warmer water to speed up the setting time
B) Use cooler water to slow down the setting time [CORRECT]
C) Add more powder to make the mixture thicker
D) Switch to a larger tray to reduce pressure on the material
Why this works: Option A is the most intuitive wrong answer (warm = faster = better). You need to know the specific relationship between humidity and setting time.

GOOD example (tax preparer):
Fact from source: "Section 179 deductions are limited to the business-use percentage of the asset."
Question: "My client bought a $2,400 laptop she uses 70% for work. Can she deduct the full cost under Section 179?"
A) Yes, the full $2,400 qualifies
B) No, only $1,680 (70% business use) [CORRECT]
C) No, she must depreciate it over 5 years at $480/year
D) Yes, but only if total business deductions are under $1 million
Why this works: All options reference real tax concepts. Option A is tempting.

BAD example (solvable by common sense):
"Patient is anxious. What should I do?"
A) Encourage deep breaths [obviously correct]
B) Ignore their anxiety [obviously wrong]
C) Sedate them immediately [extreme]
D) Give them a stress ball [sounds nice but weak]

Generate {n_questions} questions following this approach. For each:
- The question text (a realistic work scenario)
- 4 options where ALL sound professional and plausible
- The correct answer letter
- Explanation: cite the specific fact from the source, and explain why each wrong option is a common mistake
- Difficulty: medium or hard only (no easy questions)

Return a JSON array:
[
  {{
    "question": "...",
    "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
    "correct_answer": "A",
    "explanation": "The source states [specific fact]. Option B is wrong because [common mistake]. Option C ...",
    "difficulty": "medium",
    "question_type": "situational" or "calculation" or "edge_case"
  }},
  ...
]

IMPORTANT: The correct answer must be verifiable from the source material. The question must NOT be solvable by common sense alone."""


def generate_search_queries(client, occupation, task, model="gpt-4o-mini"):
    """Generate targeted search queries for a specific task."""
    prompt = SOURCE_SEARCH_PROMPT.format(occupation=occupation, task=task)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        content = response.choices[0].message.content.strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        return json.loads(content)
    except Exception as e:
        print(f"    Error generating queries: {e}")
        # Fallback queries
        return [
            f"{occupation} {task} guidelines",
            f"{occupation} {task} best practices",
            f"{occupation} {task} procedures standards",
        ]


def gather_sources_multi(client, queries, occupation, task, max_sources=5):
    """Gather RAW source text from DIVERSE providers — one source per question.

    Goal: "from a given source generate a question, from a different
    source generate a different question" and "some sources should be Q&A
    where somebody asked a question requiring reasoning and a human gave
    an answer using human reasoning."

    Source types (aiming for one of each):
      1. Reddit/forums — real practitioner Q&A with human reasoning
      2. Professional guidelines — CDC, ADA, OSHA (lookup/standards)
      3. PubMed — academic papers with technical findings
      4. Stack Exchange — Q&A with accepted expert answers
      5. Wikipedia — general factual content (fallback)
    """
    sources = []
    seen_urls = set()

    # Step 1: Reddit — real practitioner Q&A with human reasoning
    # Map occupations to their specific subreddits for relevant Q&A
    OCCUPATION_SUBREDDITS = {
        "dental": ["DentalAssistant", "dentistry", "DentalHygiene", "OralHealth"],
        "library": ["Libraries", "librarians", "Library"],
        "travel": ["TravelAgents", "travel", "Flights", "TravelHacks"],
        "dietit": ["dietetics", "nutrition", "EatCheapAndHealthy", "MealPrepSunday"],
        "nutritionist": ["dietetics", "nutrition", "EatCheapAndHealthy"],
        "operations research": ["OperationsResearch", "analytics", "datascience"],
        "tax": ["tax", "taxpros", "IRS", "Accounting"],
        "bookkeeping": ["Bookkeeping", "Accounting", "smallbusiness"],
        "accounting": ["Accounting", "Bookkeeping", "CPA"],
        "customer service": ["CustomerService", "TalesFromRetail", "callcentres"],
        "insurance": ["Insurance", "InsuranceProfessional"],
        "paralegal": ["paralegal", "LawFirm"],
        "medical record": ["HealthIT", "MedicalCoding", "CodingandBilling"],
        "tutor": ["Tutoring", "teaching", "education"],
        "financial analyst": ["FinancialCareers", "finance", "CFA"],
        "market research": ["marketing", "MarketResearch", "analytics"],
        "web develop": ["webdev", "Frontend", "learnprogramming"],
        "technical writ": ["technicalwriting", "TechWriting"],
        "human resources": ["humanresources", "AskHR"],
        "loan officer": ["MortgageProfessionals", "RealEstate", "personalfinance"],
        "statistician": ["statistics", "AskStatistics", "datascience"],
        "logistic": ["logistics", "supplychain", "SupplyChainManagement"],
        "counseling": ["therapists", "psychotherapy", "counseling"],
        "nurse": ["nursing", "StudentNurse"],
        "pharmac": ["pharmacy", "PharmacyTechnician"],
    }

    # Find matching subreddits for this occupation
    target_subs = []
    occ_lower = occupation.lower()
    for key, subs in OCCUPATION_SUBREDDITS.items():
        if key in occ_lower:
            target_subs = subs
            break
    if not target_subs:
        # Fallback: use occupation name as subreddit search
        target_subs = [occupation.replace(" ", "")]

    reddit_queries = []
    for sub in target_subs[:2]:
        reddit_queries.append(f"subreddit:{sub} help advice how question")
    reddit_queries.append(f"{occupation} work problem what should I do")
    for rq in reddit_queries:
        if len(sources) >= 2:  # Get up to 2 Reddit sources
            break
        print(f"    [Reddit] Searching: {rq[:60]}...")
        reddit_results = search_reddit(rq, max_results=2)
        for r in reddit_results:
            if len(sources) >= max_sources:
                break
            if r["url"] not in seen_urls:
                # Fetch full thread with comments (human reasoning answers)
                permalink = r["url"].replace("https://www.reddit.com", "").replace("https://reddit.com", "")
                full_text = fetch_reddit_thread(permalink)
                if len(full_text) > 300:
                    sources.append({
                        "url": r["url"],
                        "query": rq,
                        "text": full_text[:4000],
                        "source_type": "reddit_qa",
                    })
                    seen_urls.add(r["url"])
                    print(f"      Got Reddit Q&A: r/{r['subreddit']} - {r['title'][:50]}...")
        time.sleep(1)

    # Step 2: Professional sources via OpenAI web search + Jina Reader
    all_found_urls = []
    for query in queries[:2]:
        if len(sources) >= max_sources:
            break
        print(f"    [URL discovery] Searching: {query[:60]}...")
        urls = search_openai_web(client, query)
        for u in urls:
            if u not in seen_urls and "wikipedia" not in u and "reddit" not in u:
                all_found_urls.append((query, u))
                seen_urls.add(u)
        time.sleep(0.5)

    for query, url in all_found_urls:
        if len(sources) >= max_sources:
            break
        print(f"    [Jina Reader] Fetching: {url[:70]}...")
        text = fetch_via_jina(url)
        if len(text) > 300:
            sources.append({
                "url": url,
                "query": query,
                "text": text[:4000],
                "source_type": "web_jina",
            })
            print(f"      Got {len(text)} chars of raw text")
        time.sleep(1)

    # Step 3: PubMed — academic papers
    if len(sources) < max_sources:
        pubmed_query = f"{occupation} {task}"
        print(f"    [PubMed] Searching: {pubmed_query[:60]}...")
        pubmed_results = search_pubmed(pubmed_query, max_results=2)
        for s in pubmed_results:
            if len(sources) >= max_sources:
                break
            if s["url"] not in seen_urls and len(s["text"]) > 100:
                sources.append(s)
                seen_urls.add(s["url"])
                print(f"      Got PubMed article: {s['url']}")

    # Step 4: Stack Exchange — Q&A with expert reasoning
    if len(sources) < max_sources:
        se_query = f"{occupation} {task[:40]}"
        for site in ["workplace", "health", "money", "academia"]:
            if len(sources) >= max_sources:
                break
            print(f"    [StackExchange/{site}] Searching: {se_query[:50]}...")
            se_results = search_stackexchange(se_query, site=site, max_results=1)
            for s in se_results:
                if s["url"] not in seen_urls and len(s["text"]) > 100:
                    sources.append({
                        "url": s["url"],
                        "query": se_query,
                        "text": s["text"][:4000],
                        "source_type": "stackexchange_qa",
                    })
                    seen_urls.add(s["url"])
                    print(f"      Got StackExchange: {s['title'][:50]}...")
                    break

    # Step 5: Wikipedia — fallback
    if len(sources) < max_sources:
        for query in queries[:2]:
            if len(sources) >= max_sources:
                break
            wiki_results = search_wikipedia(query, num_results=1)
            for s in wiki_results:
                if s["url"] not in seen_urls:
                    sources.append(s)
                    seen_urls.add(s["url"])
                    print(f"      Got Wikipedia: {s['url']}")

    reddit_n = sum(1 for s in sources if s.get("source_type") == "reddit_qa")
    web_n = sum(1 for s in sources if s.get("source_type") == "web_jina")
    pm_n = sum(1 for s in sources if s.get("source_type") == "pubmed")
    se_n = sum(1 for s in sources if s.get("source_type") == "stackexchange_qa")
    wiki_n = sum(1 for s in sources if s.get("source_type") == "wikipedia")
    print(f"    Total: {len(sources)} sources "
          f"(reddit: {reddit_n}, web: {web_n}, pubmed: {pm_n}, "
          f"stackexchange: {se_n}, wiki: {wiki_n})")
    return sources


def gather_sources(queries, max_sources=5):
    """Legacy wrapper — used when client is not available."""
    sources = []
    seen = set()
    for query in queries:
        urls = search_web(query, num_results=3)
        for url in urls:
            if url not in seen and len(sources) < max_sources:
                seen.add(url)
                text = fetch_page_text(url)
                if len(text) > 200:
                    sources.append({"url": url, "query": query, "text": text[:4000]})
                    print(f"    Got source: {url[:80]}... ({len(text)} chars)")
        time.sleep(1)
    return sources


def generate_qa_from_sources(client, occupation, task, sources, n_questions=5, model="gpt-4o-mini"):
    """Generate verifiable Q&A pairs — ONE question per source.

    Goal: "from a given source generate a question, and then from
    a different source generate a different question."
    Each question gets its own distinct source and citation.
    """
    all_questions = []

    for i, source in enumerate(sources[:n_questions]):
        source_type = source.get("source_type", "unknown")
        source_url = source.get("url", "unknown")
        source_text = source["text"]

        print(f"      Generating Q from source {i+1}/{min(len(sources), n_questions)} "
              f"[{source_type}]: {source_url[:60]}...")

        prompt = QA_GENERATION_PROMPT.format(
            occupation=occupation,
            task=task,
            source_text=f"[Source: {source_url}]\n[Source type: {source_type}]\n\n{source_text}",
            n_questions=1
        )

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1000,
            )
            content = response.choices[0].message.content.strip()
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            questions = json.loads(content)
            if isinstance(questions, list):
                for q in questions[:1]:  # Take only 1 question per source
                    q["source_url"] = source_url
                    q["source_type"] = source_type
                    all_questions.append(q)
            elif isinstance(questions, dict):
                questions["source_url"] = source_url
                questions["source_type"] = source_type
                all_questions.append(questions)
        except Exception as e:
            print(f"        Error: {e}")

        time.sleep(0.5)

    return all_questions


def select_tasks(df, occupations, n_per_occupation=1):
    """Select the best task per occupation based on grading scores."""
    selected = []
    for occ in occupations:
        occ_tasks = df[df["occupation_title"].str.contains(occ, case=False, na=False)]
        if len(occ_tasks) == 0:
            # Try partial match
            for word in occ.split():
                occ_tasks = df[df["occupation_title"].str.contains(word, case=False, na=False)]
                if len(occ_tasks) > 0:
                    break
        if len(occ_tasks) == 0:
            print(f"  WARNING: No tasks found for '{occ}'")
            continue

        # If graded, sort by composite; otherwise by importance
        if "composite_score" in occ_tasks.columns:
            occ_tasks = occ_tasks.sort_values("composite_score", ascending=False)
        else:
            occ_tasks = occ_tasks.sort_values("importance_score", ascending=False)

        top = occ_tasks.head(n_per_occupation)
        selected.append(top)
        for _, r in top.iterrows():
            print(f"  Selected: [{r['occupation_title']}] {r['task_description'][:80]}...")

    if selected:
        return pd.concat(selected, ignore_index=True)
    return pd.DataFrame()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-occupations", type=int, default=5,
                        help="Number of occupations to process")
    parser.add_argument("--questions-per-task", type=int, default=5,
                        help="Number of Q&A pairs per task")
    parser.add_argument("--model", type=str, default="gpt-4o-mini",
                        help="OpenAI model for Q&A generation")
    parser.add_argument("--occupations", type=str, nargs="*", default=None,
                        help="Specific occupation names to use")
    args = parser.parse_args()

    client = OpenAI()

    # Load tasks — always use full parsed dataset for occupation matching,
    # since the graded set may only be a small sample
    parsed_path = os.path.join(OUTPUT_DIR, "onet_tasks_parsed.csv")

    if not os.path.exists(parsed_path):
        print("ERROR: Run 1_parse_onet.py first")
        sys.exit(1)

    df = pd.read_csv(parsed_path)
    print(f"Loaded {len(df)} tasks from full O*NET dataset")

    # Select occupations
    occupations = args.occupations or DEFAULT_OCCUPATIONS[:args.n_occupations]
    print(f"\nTarget occupations: {occupations}")

    # Select tasks
    print(f"\nSelecting tasks...")
    selected = select_tasks(df, occupations)
    if len(selected) == 0:
        print("ERROR: No tasks selected")
        sys.exit(1)

    # Generate Q&A for each selected task
    all_qa = []
    for idx, (_, task_row) in enumerate(selected.iterrows()):
        occupation = task_row["occupation_title"]
        task = task_row["task_description"]
        task_id = task_row["task_id"]

        print(f"\n{'='*80}")
        print(f"[{idx+1}/{len(selected)}] {occupation}: {task[:80]}...")
        print(f"{'='*80}")

        # Step 1: Generate search queries
        print("  Generating search queries...")
        queries = generate_search_queries(client, occupation, task, model=args.model)
        print(f"  Queries: {queries}")

        # Step 2: Gather web sources (multi-source: OpenAI web + PubMed + Wikipedia)
        print("  Gathering sources (OpenAI web search + PubMed + Wikipedia)...")
        sources = gather_sources_multi(client, queries, occupation, task)
        if not sources:
            print("  WARNING: No sources found, falling back to Wikipedia only")
            sources = gather_sources(queries)
        if not sources:
            print("  WARNING: No sources at all, using LLM knowledge only")
            sources = [{"url": "llm_knowledge", "query": "fallback", "text": f"Task: {task}"}]

        # Step 3: Generate Q&A
        print(f"  Generating {args.questions_per_task} Q&A pairs...")
        questions = generate_qa_from_sources(
            client, occupation, task, sources,
            n_questions=args.questions_per_task, model=args.model
        )

        # Attach metadata
        for q in questions:
            q["occupation"] = occupation
            q["onet_code"] = task_row["onet_code"]
            q["task_id"] = task_id
            q["task_description"] = task
            q["source_urls"] = [s["url"] for s in sources]

        all_qa.extend(questions)
        print(f"  Generated {len(questions)} questions")

        # Save progress after each task
        qa_path = os.path.join(OUTPUT_DIR, "verifiable_qa_bank.json")
        with open(qa_path, "w") as f:
            json.dump(all_qa, f, indent=2)

    # Also save as CSV for easy viewing
    if all_qa:
        qa_flat = []
        for q in all_qa:
            flat = {
                "occupation": q["occupation"],
                "onet_code": q["onet_code"],
                "task_id": q["task_id"],
                "task_description": q["task_description"],
                "question": q["question"],
                "option_a": q["options"]["A"],
                "option_b": q["options"]["B"],
                "option_c": q["options"]["C"],
                "option_d": q["options"]["D"],
                "correct_answer": q["correct_answer"],
                "explanation": q["explanation"],
                "difficulty": q.get("difficulty", ""),
                "question_type": q.get("question_type", ""),
            }
            qa_flat.append(flat)

        csv_path = os.path.join(OUTPUT_DIR, "verifiable_qa_bank.csv")
        pd.DataFrame(qa_flat).to_csv(csv_path, index=False)

        print(f"\n{'='*80}")
        print(f"Q&A GENERATION COMPLETE")
        print(f"  Total questions: {len(all_qa)}")
        print(f"  Occupations covered: {len(set(q['occupation'] for q in all_qa))}")
        print(f"  JSON output: {qa_path}")
        print(f"  CSV output: {csv_path}")
        print(f"\nBreakdown by occupation:")
        for occ in set(q["occupation"] for q in all_qa):
            n = sum(1 for q in all_qa if q["occupation"] == occ)
            print(f"  {occ}: {n} questions")
    else:
        print("\nERROR: No questions generated")


if __name__ == "__main__":
    main()
