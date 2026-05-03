"""
Step 71: Occupation-level auto-source pipeline v7 — v4 + alt-titles discovery.

Identical to v4 (66_auto_source_v4_occupation.py) except the discovery layer
expands each search query with up to ALT_TITLE_LIMIT alternate titles from
O*NET's 'Sample of Reported Titles.txt' (curated, practitioner-phrased) and
falls back to 'Alternate Titles.txt' when Sample is empty. This improves
recall on occupations whose formal SOC title differs from the name real
practitioners use online (e.g. 'Accountants and Auditors' -> 'CPA',
'Internal Auditor').

All downstream quality checks (judge_consensus, judge_wrong, scenario +
paraphrase generation, and the separate Pass 1 / Pass 2 / Pass 3 filter
scripts) are unchanged from v4. Alt-titles do NOT create new occupation
rows — every item is tagged with the original O*NET SOC title.

Output:
  output/qa_auto_source_v7.json         — item bank (per-worker via CLI)
  output/coverage_map_v7.csv            — per-occupation coverage transparency
  output/auto_source_v7.log             — detailed pipeline log
  output/v7_ckpt.json                   — {occupation: item_list} checkpoint

Usage:
  python3 71_auto_source_v7_alttitles.py --from-csv output/scale_up_1016_occupations.csv
"""
import os, re, json, time, html, ssl, random, argparse, csv
from pathlib import Path
import urllib.request, urllib.error, urllib.parse
from openai import OpenAI

ROOT = Path(__file__).parent
OUT = ROOT / "output"

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = ssl.create_default_context()
    SSL_CTX.check_hostname = False; SSL_CTX.verify_mode = ssl.CERT_NONE

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15"
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
SE_KEY = (ROOT/"api_key_stackexchange.txt").read_text().strip() if (ROOT/"api_key_stackexchange.txt").exists() else ""
SCRAPINGBEE_KEY = (ROOT/"api_key_scrapingbee.txt").read_text().strip() if (ROOT/"api_key_scrapingbee.txt").exists() else ""
oa = OpenAI(api_key=(ROOT/"api_key.txt").read_text().strip())
SEARCH_MODEL = "gpt-4o-mini"   # has web_search_preview tool
EXTRACT_MODEL = "gpt-4o"        # thread extraction + judgement
GEN_MODEL = "gpt-4o"            # scenario + option paraphrase

TARGET_ITEMS = 8
MAX_FAILURES = 10   # give up on occupation after this many consecutive failures
ALT_TITLE_LIMIT = 4  # max alt titles to append to discovery queries per occupation


# ─── Alt-title lookup (O*NET Sample of Reported Titles + Alternate Titles) ─

_ALT_TITLES_CACHE = None

def _normalise_title(t):
    # Drop parenthetical abbreviations like "Certified Public Accountant (CPA)"
    # because they inflate query length without adding recall
    t = re.sub(r"\s*\([^)]{1,40}\)\s*", " ", t).strip()
    # Collapse whitespace
    t = re.sub(r"\s+", " ", t)
    return t

def load_alt_titles():
    """Return {occupation_title: [alt_title_1, alt_title_2, ...]} keyed by the
    formal O*NET title. Prefers 'Sample of Reported Titles' (curated, short)
    over 'Alternate Titles' (full, noisier)."""
    global _ALT_TITLES_CACHE
    if _ALT_TITLES_CACHE is not None:
        return _ALT_TITLES_CACHE
    import pandas as pd
    data_dir = ROOT / "data" / "db_29_1_text"
    occ = pd.read_csv(data_dir / "Occupation Data.txt", sep="\t")
    soc_to_title = dict(zip(occ["O*NET-SOC Code"], occ["Title"]))

    by_title = {}
    # Primary source: Sample of Reported Titles
    srt_path = data_dir / "Sample of Reported Titles.txt"
    if srt_path.exists():
        srt = pd.read_csv(srt_path, sep="\t")
        for soc, grp in srt.groupby("O*NET-SOC Code"):
            title = soc_to_title.get(soc)
            if not title: continue
            alts = []
            for t in grp["Reported Job Title"].dropna().tolist():
                nt = _normalise_title(str(t))
                if nt and nt.lower() != title.lower() and nt not in alts:
                    alts.append(nt)
            if alts:
                by_title[title] = alts

    # Fallback source: Alternate Titles
    alt_path = data_dir / "Alternate Titles.txt"
    if alt_path.exists():
        alt = pd.read_csv(alt_path, sep="\t")
        for soc, grp in alt.groupby("O*NET-SOC Code"):
            title = soc_to_title.get(soc)
            if not title or title in by_title: continue
            alts = []
            for t in grp["Alternate Title"].dropna().tolist():
                nt = _normalise_title(str(t))
                if nt and nt.lower() != title.lower() and nt not in alts:
                    alts.append(nt)
            if alts:
                by_title[title] = alts

    _ALT_TITLES_CACHE = by_title
    return by_title


def _search_terms(occupation):
    """Return a list [main_title, alt1, alt2, ...] truncated to ALT_TITLE_LIMIT+1."""
    alts = load_alt_titles().get(occupation, [])
    return [occupation] + alts[:ALT_TITLE_LIMIT]


def _occupation_or_terms(occupation):
    """Quoted OR-join of the main title with its alt titles, for use inside
    prose prompts. Keeps the main title as the anchor."""
    terms = _search_terms(occupation)
    return " OR ".join(f'"{t}"' for t in terms)


# ─── HTTP ─────────────────────────────────────────────────────────────────

def fetch(url, timeout=20, headers=None):
    import gzip as _g
    h = headers if headers is not None else BROWSER_HEADERS
    req = urllib.request.Request(url, headers=h)
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                data = r.read()
            if data[:2] == bytes([0x1f, 0x8b]):
                data = _g.decompress(data)
            return data
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == 1: raise
            time.sleep(1.5)


def scrapingbee_fetch(url, timeout=60, try_premium_on_fail=True):
    """Fetch a URL through ScrapingBee. Returns raw bytes or raises on failure.

    Starts with the cheapest settings (render_js=False, premium_proxy=False,
    1 credit each). If that returns a 4xx/5xx status, retries once with
    premium_proxy=True (10 credits) to handle Cloudflare-protected sites.
    """
    if not SCRAPINGBEE_KEY:
        raise RuntimeError("SCRAPINGBEE_KEY not configured")
    # strip tracker params from the target URL
    clean = url.split("?utm")[0].split("#")[0]
    def _call(premium):
        params = {
            "api_key": SCRAPINGBEE_KEY,
            "url": clean,
            "render_js": "False",
            "premium_proxy": "True" if premium else "False",
        }
        api = "https://app.scrapingbee.com/api/v1/?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(api, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            status = r.headers.get("Spb-Initial-Status-Code")
            cost = r.headers.get("Spb-Cost")
            data = r.read()
        return data, status, cost
    try:
        data, status, cost = _call(premium=False)
        try:
            status_int = int(status) if status else 200
        except Exception:
            status_int = 200
        if status_int >= 400 and try_premium_on_fail:
            data, status, cost = _call(premium=True)
        return data
    except urllib.error.HTTPError as e:
        if try_premium_on_fail:
            try:
                data, status, cost = _call(premium=True)
                return data
            except Exception:
                raise e
        raise


def html_to_clean(s):
    if isinstance(s, bytes): s = s.decode("utf-8", errors="replace")
    s = re.sub(r"<script[\s\S]*?</script>", "", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", "", s, flags=re.I)
    s = re.sub(r"<nav[\s\S]*?</nav>", "", s, flags=re.I)
    s = re.sub(r"<footer[\s\S]*?</footer>", "", s, flags=re.I)
    s = re.sub(r"<header[\s\S]*?</header>", "", s, flags=re.I)
    s = re.sub(r"<aside[\s\S]*?</aside>", "", s, flags=re.I)
    s = re.sub(r"<!--[\s\S]*?-->", "", s)
    # strip common sidebar / menu / tag-cloud regions
    s = re.sub(r'<div[^>]*(?:sidebar|menu|breadcrumb|tag-cloud|related|advert|cookie)[^>]*>[\s\S]*?</div>', "", s, flags=re.I)
    return s


# ─── Venue-agnostic discovery via web_search_preview ──────────────────────

DISCOVERY_PROMPTS = [
    # Venue-biased Reddit (expanded with profession-specific subreddits)
    'Find 10-15 Reddit threads where someone working as {occupation_terms} asked a concrete on-the-job question and received multiple community responses with visible upvotes. Any of the listed job titles count as the same occupation. Consider profession-specific subreddits (e.g. r/Accounting, r/personalfinance, r/AskLawyers, r/LawSchool, r/Teachers, r/Professors, r/medicine, r/finance, r/architecture, r/rehabtherapy, r/REprofessional, r/humanresources, r/socialwork, r/therapists, r/counseling, r/InsuranceAgent, r/Realtors, r/PropertyManagement, r/librarians, r/academia) as well as general ones. Return only reddit.com URLs.',
    # Venue-biased Stack Exchange (expanded site list)
    'Find 10-15 Stack Exchange Q&A pages where someone working as {occupation_terms} (or someone asking about the same work) received substantive answers with votes. Any of the listed job titles count as the same occupation. Consider all SE sites including workplace, money, travel, diy, academia, law, history, biology, cooking, politics, economics, philosophy, physics, space, datascience, ai, expatriates, writers, skeptics, astronomy, engineering, english-language-learners, medicalsciences, and others. Return only stackexchange.com or stackoverflow.com URLs.',
    # Niche profession forums + AskMe (expanded)
    'Find 10-15 community forum threads where practitioners of {occupation_terms} discussed on-the-job problems. Any of the listed job titles count as the same occupation. Candidate sites include plumbingzone.com, truckersreport.com, electriciantalk.com, allnurses.com, hvac-talk.com, dentaltown.com, ask.metafilter.com, taxprotalk.com, accountantforums.com, accountingweb.com community, architectjobforums, psychology forums, justia lawyer forums, and any other specialty profession forum. Prefer threads with community-validated answers (upvotes, accepted-answer flags, best-answer markers). Return only URLs.',
    # LinkedIn Collaborative Articles and Quora
    'Find 10-15 LinkedIn Collaborative Articles (linkedin.com/advice/ paths) or Quora threads (quora.com) where professionals working as {occupation_terms} contributed practical on-the-job insights or practitioners asked concrete work-situation questions. Any of the listed job titles count as the same occupation. For LinkedIn, prefer articles with multiple contributor responses. For Quora, prefer threads with multiple upvoted answers and answerers whose bios mention relevant occupation experience. Return only linkedin.com/advice or quora.com URLs.',
    # Open practitioner Q&A (catch-all)
    'Find online threads or Q&A pages where someone working as {occupation_terms} asked a concrete question about their work and received multiple community-written responses with some form of community validation (upvotes, best-answer flags, accepted-solution markers). Any of the listed job titles count as the same occupation. Include any relevant site: profession-specific forums, SE sites, subreddits, LinkedIn Collaborative Articles, Quora threads. Prefer threaded discussions. Return only URLs.',
]


REDDIT_BLACKLIST_SUBS = {
    # AITA / Redditor-update / drama-story subs
    "BestofRedditorUpdates", "BORUpdates", "AITA", "AmItheAsshole",
    "AmItheAssholeDaily", "AITAH", "AITA_JUSTNO", "AITAFiltered",
    "TrueOffMyChest", "offmychest", "confession", "confessions",
    "tifu", "TwoHotTakes", "EntitledPeople", "EntitledParents",
    "ChoosingBeggars", "coworkerstories", "CharlotteDobreYouTube",
    "pettyrevenge", "ProRevenge", "NuclearRevenge", "MaliciousCompliance",
    # Relationship / personal-drama subs
    "relationships", "relationship_advice", "BreakUps", "survivinginfidelity",
    "JUSTNOMIL", "JUSTNOFAMILY", "raisedbynarcissists", "Divorce",
    # Feel-good / rant / misc aggregator subs (no occupational signal)
    "TheQuarrySupermassive", "MadeMeSmile", "mildlyinfuriating",
    "bald", "TwoXChromosomes", "LifeProTips", "YouShouldKnow",
    "explainlikeimfive", "NoStupidQuestions", "ask", "AskReddit",
    "OutOfTheLoop", "unpopularopinion", "rant", "Rants", "TrueUnpopularOpinion",
    # Location-specific drift subs
    "centuryhomes", "longbeach",
}


def _is_blacklisted_reddit(url: str) -> bool:
    """Return True if the URL is a reddit.com thread in a blacklisted sub.
    Used to filter out drama / AITA / aggregator subs from *every* discovery
    pathway (web_search_preview hits, direct Reddit API search, etc.)."""
    if "reddit.com/r/" not in url:
        return False
    m = re.search(r"reddit\.com/r/([^/]+)/", url)
    if not m:
        return False
    return m.group(1) in REDDIT_BLACKLIST_SUBS


def _reddit_discover_subreddits(occupation, limit=6):
    """Use Reddit's subreddits/search.json to find subreddits relevant to the
    occupation, so we can search within them rather than across all of Reddit."""
    q = urllib.parse.quote(occupation.split(",")[0].split(" and ")[0])
    u = f"https://old.reddit.com/subreddits/search.json?q={q}&limit={limit}"
    try:
        data = json.loads(fetch(u, timeout=10))
    except Exception:
        return []
    out = []
    for c in data.get("data", {}).get("children", []):
        x = c.get("data", {})
        name = x.get("display_name", "")
        subs = x.get("subscribers", 0) or 0
        if subs < 500: continue
        out.append(name)
    return out


def _reddit_search_urls(occupation, limit=15):
    """Direct Reddit global search + subreddit-scoped search.

    Combines two calls:
      1. Global search with Q&A-leaning query keywords.
      2. Search within the top subreddits returned by subreddit-discovery for
         this occupation. Catches practitioner threads the web_search_preview
         tool fails to surface.
    """
    out = []
    seen = set()

    # Global Reddit search, Q&A-biased — aggregator / drama subs filtered out.
    # Build OR-joined search over main title + alt titles so practitioner-
    # language threads surface even when they never use the formal SOC name.
    terms = _search_terms(occupation)
    terms_or = " OR ".join(f'"{t}"' for t in terms)
    q_phrase = f'({terms_or}) ("how do i" OR "what should i" OR "advice" OR "help")'
    q = urllib.parse.quote(q_phrase)
    global_url = f"https://old.reddit.com/search.json?q={q}&sort=top&t=year&limit={limit}"
    try:
        data = json.loads(fetch(global_url, timeout=15))
        for c in data.get("data", {}).get("children", []):
            x = c.get("data", {})
            if x.get("num_comments", 0) < 5: continue
            sub = x.get("subreddit", "")
            if sub in REDDIT_BLACKLIST_SUBS: continue
            permalink = x.get("permalink", "")
            if not permalink or permalink in seen: continue
            seen.add(permalink)
            out.append({
                "url": f"https://www.reddit.com{permalink}",
                "clean_url": f"https://www.reddit.com{permalink}",
                "title": x.get("title", ""),
            })
    except Exception:
        pass

    # Subreddit-scoped search — but first validate each discovered subreddit
    # is actually topically related to the occupation (subreddit discovery
    # over-matches on keyword similarity, e.g. "Counselor" matched a video-
    # game subreddit for "Credit Counselors").
    subs = _reddit_discover_subreddits(occupation, limit=6)
    subs = [s for s in subs if s not in REDDIT_BLACKLIST_SUBS]
    subs = _validate_subreddits_relevance(occupation, subs)
    for sub in subs[:4]:
        scoped_q = urllib.parse.quote(terms_or)
        scoped_url = (f"https://old.reddit.com/r/{sub}/search.json"
                      f"?q={scoped_q}&restrict_sr=1&sort=top&t=all&limit=10")
        try:
            data = json.loads(fetch(scoped_url, timeout=10))
            for c in data.get("data", {}).get("children", []):
                x = c.get("data", {})
                if x.get("num_comments", 0) < 5: continue
                sub_name = x.get("subreddit", "")
                if sub_name in REDDIT_BLACKLIST_SUBS: continue
                permalink = x.get("permalink", "")
                if not permalink or permalink in seen: continue
                seen.add(permalink)
                out.append({
                    "url": f"https://www.reddit.com{permalink}",
                    "clean_url": f"https://www.reddit.com{permalink}",
                    "title": x.get("title", ""),
                })
        except Exception:
            continue
    return out


def _validate_subreddits_relevance(occupation, subreddits):
    """Drop subreddits that aren't topically about the occupation's work.
    Uses a quick LLM call to reject keyword-matched false positives."""
    if not subreddits: return []
    prompt = (f"For each subreddit below, answer YES if it is a community where "
              f"practitioners of the occupation '{occupation}' (or closely related "
              f"occupational work) discuss on-the-job topics. Answer NO if the "
              f"subreddit only shares a keyword but is unrelated to the occupation "
              f"(e.g. a video game, a city, a TV show, or a meme community).\n\n"
              + "\n".join(f"{i+1}. r/{s}" for i, s in enumerate(subreddits))
              + "\n\nRespond with one line per subreddit in format: '<number>. <YES or NO>'.")
    raw = call_openai(prompt, max_tokens=200, temperature=0.0, model="gpt-4o-mini")
    ok = []
    for i, s in enumerate(subreddits):
        m = re.search(rf'^\s*{i+1}\.\s*(YES|NO)', raw or "", re.MULTILINE | re.IGNORECASE)
        if m and m.group(1).upper() == "YES":
            ok.append(s)
    return ok


def discover_threads(occupation):
    """Run all discovery queries, dedupe URLs, return combined list.

    Combines: 4 web_search_preview queries (different venue biases) + 1 direct
    Reddit search via reddit.com/search.json. The direct Reddit call is
    necessary because web_search_preview often fails to surface Reddit URLs
    even when asked to.
    """
    urls, seen = [], set()

    # 5 web-search queries with different venue biases; each is expanded with
    # O*NET alt-titles so practitioner-language threads are discoverable even
    # when the formal SOC title is bureaucratic.
    occ_terms = _occupation_or_terms(occupation)
    for q_tmpl in DISCOVERY_PROMPTS:
        prompt = q_tmpl.format(occupation_terms=occ_terms)
        try:
            resp = oa.responses.create(model=SEARCH_MODEL,
                                        tools=[{"type": "web_search_preview"}],
                                        input=prompt)
        except Exception:
            continue
        for item in getattr(resp, "output", []):
            for c in getattr(item, "content", None) or []:
                for a in getattr(c, "annotations", None) or []:
                    u = getattr(a, "url", None)
                    if not u: continue
                    u_clean = u.split("?")[0]
                    if u_clean in seen: continue
                    seen.add(u_clean)
                    urls.append({"url": u, "clean_url": u_clean,
                                 "title": getattr(a, "title", "") or ""})

    # Direct Reddit API search as a supplement — web_search_preview often misses Reddit
    for u in _reddit_search_urls(occupation, limit=15):
        if u["clean_url"] in seen: continue
        seen.add(u["clean_url"])
        urls.append(u)

    # Universal Reddit-blacklist enforcement: web_search_preview hits bypass
    # the per-pathway blacklists applied inside _reddit_search_urls, so drop
    # any reddit.com URL in a blacklisted sub here regardless of source.
    urls = [u for u in urls if not _is_blacklisted_reddit(u.get("url", ""))]

    return urls, None


# ─── Venue-aware fetch routing ─────────────────────────────────────────────

def _is_reddit(url):
    return "reddit.com/r/" in url and "/comments/" in url

def _is_stackexchange(url):
    return (".stackexchange.com/questions/" in url or
            "stackoverflow.com/questions/" in url or
            "serverfault.com/questions/" in url or
            "superuser.com/questions/" in url or
            "askubuntu.com/questions/" in url)


def _reddit_fetch_structured(url):
    """Use Reddit's .json endpoint to fetch full thread structure directly."""
    u = url.split("?")[0].rstrip("/") + ".json?sort=top&limit=25"
    u = u.replace("www.reddit.com", "old.reddit.com").replace("//reddit.com", "//old.reddit.com")
    try:
        data = json.loads(fetch(u))
    except Exception:
        return None
    if not isinstance(data, list) or len(data) < 2: return None
    post = data[0].get("data",{}).get("children",[{}])[0].get("data",{})
    responses = []
    for c in data[1].get("data",{}).get("children",[]):
        if c.get("kind") != "t1": continue
        cd = c.get("data",{})
        txt = cd.get("body","") or ""
        if len(txt) < 40: continue
        responses.append({
            "text": txt,
            "validation_score": cd.get("score", 0),
            "is_accepted": False,
        })
    if len(responses) < 4: return None
    responses.sort(key=lambda r: -r["validation_score"])
    return {
        "url": url,
        "title": post.get("title", ""),
        "question_body": post.get("selftext","") or post.get("title",""),
        "responses": responses,
        "confidence": 0.95,
    }


def _stackexchange_fetch_structured(url):
    """Use SE API to fetch question + answers for a /questions/ URL."""
    m = re.search(r"://([^/]+)/questions/(\d+)", url)
    if not m: return None
    host = m.group(1).lower()
    qid = m.group(2)
    # Map host to SE site param
    if host.endswith(".stackexchange.com"):
        site = host.split(".")[0]
    elif host == "stackoverflow.com": site = "stackoverflow"
    elif host == "serverfault.com":   site = "serverfault"
    elif host == "superuser.com":     site = "superuser"
    elif host == "askubuntu.com":     site = "askubuntu"
    else: return None
    key_q = f"&key={SE_KEY}" if SE_KEY else ""
    qu = f"https://api.stackexchange.com/2.3/questions/{qid}?site={site}&filter=withbody{key_q}"
    au = f"https://api.stackexchange.com/2.3/questions/{qid}/answers?site={site}&order=desc&sort=votes&pagesize=12&filter=withbody{key_q}"
    try:
        qd = json.loads(fetch(qu))["items"][0]
        ad = json.loads(fetch(au))["items"]
    except Exception:
        return None
    def _strip(html_s):
        s = re.sub(r"<[^>]+>", " ", html_s or "")
        s = html.unescape(s)
        return re.sub(r"\s+", " ", s).strip()
    responses = []
    for a in ad:
        text = _strip(a.get("body",""))
        if len(text) < 40: continue
        responses.append({
            "text": text,
            "validation_score": a.get("score", 0),
            "is_accepted": a.get("is_accepted", False),
        })
    if len(responses) < 4: return None
    # Sort: accepted first, then by score
    responses.sort(key=lambda r: (0 if r.get("is_accepted") else 1, -r["validation_score"]))
    return {
        "url": url,
        "title": qd.get("title", ""),
        "question_body": _strip(qd.get("body","")),
        "responses": responses,
        "confidence": 0.95,
    }


def route_and_extract(url, log):
    """Route each URL to the appropriate fetch path, then extract thread structure."""
    if _is_reddit(url):
        t = _reddit_fetch_structured(url)
        if t: return t
        log(f"    reddit structured fetch failed: {url[:80]}")
        return None
    if _is_stackexchange(url):
        t = _stackexchange_fetch_structured(url)
        if t: return t
        log(f"    SE structured fetch failed: {url[:80]}")
        return None
    # Generic HTML + LLM extraction for any other venue.
    # Try direct fetch first (free); if it fails, fall back to ScrapingBee.
    try:
        raw = fetch(url)
    except Exception as direct_err:
        if not SCRAPINGBEE_KEY:
            log(f"    fetch fail (no ScrapingBee) {url[:80]}: {type(direct_err).__name__}")
            return None
        try:
            raw = scrapingbee_fetch(url)
        except Exception as sb_err:
            log(f"    fetch fail {url[:80]}: direct={type(direct_err).__name__} sb={type(sb_err).__name__}")
            return None
    return extract_thread(url, raw)


# ─── LLM-driven thread extraction ─────────────────────────────────────────

EXTRACT_PROMPT = """You are given the raw HTML (cleaned of scripts and styles) of a web page that may be a community Q&A thread. Decide whether this page is a genuine threaded discussion where one person asked a question and others responded, then extract structured data.

Output ONE JSON object:
{{
  "is_thread": <true|false>,
  "confidence": <0.0-1.0>,
  "title": "<thread title>",
  "question_body": "<first-post / question body text, plain, no HTML>",
  "responses": [
    {{"text": "<response body plain text>",
      "validation_score": <integer: upvotes or thanks or reply count; 0 if unknown>,
      "is_accepted": <true|false|null>
    }},
    ...
  ]
}}

Rules:
- If not a genuine Q&A thread, return {{"is_thread": false, "confidence": <0.0-1.0>, "title":"", "question_body":"", "responses":[]}}.
- Order responses highest validation_score first (or is_accepted first, then by score).
- Skip responses shorter than 40 characters.
- Skip non-response content (ads, nav, related-links).

PAGE URL: {url}
PAGE HTML (trimmed):
\"\"\"
{html_trimmed}
\"\"\"
"""


def extract_thread(url, raw_html):
    cleaned = html_to_clean(raw_html)
    # Try to focus on main content blocks that typical forums expose
    # (articles, posts, messages). Fall back to cleaned page if none found.
    candidate_blocks = re.findall(r"<article[\s\S]*?</article>", cleaned, flags=re.I)
    if len(candidate_blocks) >= 3:
        condensed = "\n\n".join(candidate_blocks[:30])
    else:
        condensed = cleaned
    trimmed = re.sub(r"\s+", " ", condensed).strip()[:60000]
    raw = call_openai(EXTRACT_PROMPT.format(url=url, html_trimmed=trimmed),
                      max_tokens=3000, temperature=0.1, model=EXTRACT_MODEL)
    obj = parse_json_obj(raw)
    if not obj: return None
    # Loosen: accept confidence >= 0.5 OR is_thread=true, as long as >=3 responses
    responses = obj.get("responses", []) or []
    conf = obj.get("confidence", 0.0) or 0.0
    is_thread = obj.get("is_thread", False)
    if not (is_thread or conf >= 0.5):
        return None
    if len(responses) < 3:
        return None
    # If no validation scores visible, use chronological (list-position) as weak signal
    if all((r.get("validation_score", 0) or 0) == 0 for r in responses):
        for i, r in enumerate(responses):
            r["validation_score"] = max(1, len(responses) - i)
    return {
        "url": url,
        "title": obj.get("title", ""),
        "question_body": obj.get("question_body", ""),
        "responses": responses,
        "confidence": conf,
    }


# ─── Consensus + distractor judgement ─────────────────────────────────────

CONSENSUS_PROMPT = """Given the thread below, decide whether the TOP response (the first one in the list) is a reasonable community-preferred answer. Accept if ANY of these hold:
  (a) the asker marked it best / accepted,
  (b) it has a noticeably higher upvote or thanks count than the others,
  (c) there is clear community agreement it is correct/preferred,
  (d) no explicit vote signal is available but the TOP response is substantively more useful, specific, or correct than the others based on content quality.

Respond with one word only: ACCEPT or REJECT.

QUESTION:
{question}

TOP RESPONSE (validation_score={top_score}, is_accepted={top_accepted}):
{top}

OTHER RESPONSES (summarised):
{others}
"""


def judge_consensus(thread):
    top = thread["responses"][0]
    others = "\n\n".join(
        f"- score={r.get('validation_score',0)}, accepted={r.get('is_accepted')}, text={r.get('text','')[:200]}"
        for r in thread["responses"][1:6]
    )
    raw = call_openai(CONSENSUS_PROMPT.format(
        question=thread["question_body"][:1200],
        top_score=top.get("validation_score",0),
        top_accepted=top.get("is_accepted"),
        top=top.get("text","")[:1200],
        others=others),
        max_tokens=10, temperature=0.0, model=EXTRACT_MODEL)
    return "ACCEPT" in (raw or "").upper()


JUDGE_WRONG_PROMPT = """You are judging whether a secondary response on a community thread would make a good WRONG distractor in a multiple-choice question, given the TOP response is the correct answer.

A secondary response is a good WRONG distractor if ANY of these are true:
  - It contradicts, misleads about, or gives advice the top response would call incorrect.
  - It recommends a materially different action than the top response (not just the same advice in fewer words).
  - It addresses a tangential aspect rather than the main question the top response answers.

Return one word: "WRONG" if it is a suitable distractor per any of the above, "OK" if it is on the same track as the top response and would be too similar.

TOP/ACCEPTED RESPONSE:
{top}

SECONDARY RESPONSE:
{other}

Label (WRONG or OK):"""


def judge_wrong(top_text, other_text):
    raw = call_openai(JUDGE_WRONG_PROMPT.format(top=top_text[:1500], other=other_text[:1500]),
                      max_tokens=10, temperature=0.0, model=EXTRACT_MODEL)
    return "WRONG" in (raw or "").upper()


# ─── Scenario + option paraphrase ─────────────────────────────────────────

SCENARIO_PROMPT = """Write a first-person on-the-job help-seeking scenario, in the voice of a {occupation}. The scenario MUST be grounded in the real situation from the community thread below, preserving named systems, tools, constraints, and specifics. The scenario should read like the worker is stuck or unsure and asking for help, not a polished textbook question. Target length: 80-180 words. End with a single concrete question (e.g., "what should I do?").

ORIGINAL QUESTION TITLE: {title}
ORIGINAL QUESTION BODY:
\"\"\"
{body}
\"\"\"

Output only the scenario paragraph. Do not say "according to the thread" or reference the source."""


PARAPHRASE_PROMPT = """Paraphrase each of the four community responses into a single 20-40 word option. Preserve the specific advice or the specific mistaken claim in each. Output exactly four lines:
1) <paraphrase of RESPONSE 1>
2) <paraphrase of RESPONSE 2>
3) <paraphrase of RESPONSE 3>
4) <paraphrase of RESPONSE 4>

RESPONSE 1 (correct):
{r1}

RESPONSE 2 (wrong):
{r2}

RESPONSE 3 (wrong):
{r3}

RESPONSE 4 (wrong):
{r4}
"""


# ─── utilities ────────────────────────────────────────────────────────────

def call_openai(prompt, max_tokens=600, temperature=0.2, model=None):
    model = model or GEN_MODEL
    for attempt in range(3):
        try:
            r = oa.chat.completions.create(
                model=model, temperature=temperature, max_tokens=max_tokens,
                messages=[{"role":"user","content":prompt}])
            return (r.choices[0].message.content or "").strip()
        except Exception:
            if attempt == 2: return ""
            time.sleep(1.5*(attempt+1))


def parse_json_obj(s):
    m = re.search(r"\{[\s\S]*\}", s or "")
    if not m: return None
    try: return json.loads(m.group(0))
    except Exception: return None


def first_sentences(t, max_chars=220):
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= max_chars: return t
    m = re.search(r"[.!?](\s|$)", t[60:max_chars+60])
    return (t[:60 + m.start() + 1] if m else t[:max_chars]).strip().rstrip("…")


def parse_numbered(s):
    out = {}
    for num in "1234":
        m = re.search(rf'^\s*{num}\)\s*(.+?)(?=\n\s*\d\)|\Z)', s, re.MULTILINE | re.DOTALL)
        if m: out[num] = first_sentences(m.group(1).strip(), 260)
    return out


# ─── Build one item from one thread ───────────────────────────────────────

def build_item(occupation, thread, log):
    responses = thread.get("responses", [])
    # Need at least 1 correct + 3 wrong candidates pre-filter, i.e. 4 total
    if len(responses) < 4: return None, "too_few_responses"
    if not judge_consensus(thread): return None, "consensus_reject"

    top = responses[0]
    wrong = []
    for r in responses[1:]:
        if len(wrong) >= 6: break
        if not r.get("text","").strip(): continue
        if judge_wrong(top["text"], r["text"]):
            wrong.append(r)

    if len(wrong) < 3:
        return None, f"not_enough_wrong({len(wrong)})"

    random.shuffle(wrong)
    distractors = wrong[:3]

    # Scenario
    scenario = call_openai(SCENARIO_PROMPT.format(
        occupation=occupation, title=thread["title"],
        body=(thread.get("question_body") or thread["title"])[:2500]),
        max_tokens=400, temperature=0.4)
    if not scenario or len(scenario) < 60: return None, "no_scenario"

    # Paraphrase options
    raw = call_openai(PARAPHRASE_PROMPT.format(
        r1=top["text"][:1500], r2=distractors[0]["text"][:1500],
        r3=distractors[1]["text"][:1500], r4=distractors[2]["text"][:1500]),
        max_tokens=500, temperature=0.3)
    paras = parse_numbered(raw)
    if len(paras) < 4: return None, "paraphrase_fail"

    # Scramble letters: 1 -> correct; 2,3,4 -> wrong
    letters = list("ABCD"); random.shuffle(letters)
    options = {}
    for i, letter in enumerate(letters):
        options[letter] = paras[str(i+1)]
    correct_letter = letters[0]
    options["E"] = "All of the above"
    options["F"] = "None of the above"
    options = {k: options[k] for k in sorted(options)}

    return {
        "occupation": occupation,
        "question": scenario,
        "options": options,
        "correct_answer": correct_letter,
        "answer_type": "single",
        "source_url": thread["url"],
        "source_method": "automated_multi_venue",
        "distractor_source": "same_thread_judged_wrong",
    }, None


# ─── Per-occupation orchestration ─────────────────────────────────────────

def _filter_urls_by_relevance(occupation, urls, log, batch=15):
    """Drop URLs whose titles are obviously off-topic for the occupation.
    Uses a single batched LLM call to keep cost low."""
    if not urls: return urls
    kept = []
    for start in range(0, len(urls), batch):
        chunk = urls[start:start+batch]
        numbered = "\n".join(f"{i+1}. TITLE: {c.get('title','')[:120]}\n   URL: {c.get('url','')[:150]}"
                              for i, c in enumerate(chunk))
        prompt = (f"For each numbered item below, decide whether the thread is clearly about "
                  f"the PROFESSIONAL WORK, on-the-job situations, client/customer interactions, "
                  f"workflows, or practice areas of someone working as '{occupation}'. The thread "
                  f"must present an occupational task or work situation where occupation-specific "
                  f"knowledge is required to answer.\n\n"
                  f"Answer NO if the thread is:\n"
                  f"  - a personal/relationship story, marital dispute, family drama, or workplace-"
                  f"    drama venting where the occupation is only background context (e.g. 'my "
                  f"    wife is a manager and I'm upset with her', 'my coworker got promoted');\n"
                  f"  - an AITA / 'am I the asshole' / 'update on my ex' / entitled-people / "
                  f"    petty-revenge style story;\n"
                  f"  - a career-choice / should-I-switch-jobs / salary-negotiation / resume-advice "
                  f"    thread (those are career questions, not occupational-task questions);\n"
                  f"  - about video games, TV shows, celebrities, memes, politics, current events, "
                  f"    or religious/philosophical debate;\n"
                  f"  - a thread where the occupation is mentioned only in passing.\n\n"
                  f"Answer YES only if resolving the thread's question would require occupation-"
                  f"specific task knowledge that a practitioner of '{occupation}' would have.\n\n"
                  f"{numbered}\n\n"
                  f"Respond with one line per item: '<number>. <YES or NO>'.")
        raw = call_openai(prompt, max_tokens=400, temperature=0.0, model="gpt-4o-mini")
        for i, c in enumerate(chunk):
            m = re.search(rf'^\s*{i+1}\.\s*(YES|NO)', raw or "", re.MULTILINE | re.IGNORECASE)
            if m and m.group(1).upper() == "YES":
                kept.append(c)
    log(f"  topical-relevance filter: kept {len(kept)}/{len(urls)}")
    return kept


def process_occupation(occupation, ckpt, log):
    existing = ckpt.get(occupation, [])
    if len(existing) >= TARGET_ITEMS:
        log(f"[{occupation}] already at {len(existing)} items; skipping.")
        return existing

    log(f"\n=== {occupation} ===  (have {len(existing)} / target {TARGET_ITEMS})")

    # 1. Discovery
    urls, err = discover_threads(occupation)
    log(f"  discovered {len(urls)} candidate URLs ({err or 'ok'})")
    if not urls:
        return existing

    # 2. Topical-relevance filter — drop off-topic URLs before expensive fetch+LLM
    urls = _filter_urls_by_relevance(occupation, urls, log)
    if not urls:
        log("  all URLs filtered as off-topic; skipping occupation")
        return existing

    used_urls = {item["source_url"] for item in existing}
    consecutive_fails = 0
    for cand in urls:
        if len(existing) >= TARGET_ITEMS: break
        if consecutive_fails >= MAX_FAILURES:
            log(f"  giving up after {consecutive_fails} consecutive failures")
            break
        url = cand["url"]
        if url in used_urls:
            continue
        used_urls.add(url)

        # Venue-aware fetch + extract (Reddit/SE use native APIs; others use LLM)
        thread = route_and_extract(url, log)
        time.sleep(0.4)
        if not thread:
            consecutive_fails += 1
            continue

        # Build item
        item, err = build_item(occupation, thread, log)
        if not item:
            log(f"    build fail ({err})  {url[:80]}")
            consecutive_fails += 1
            continue

        existing.append(item)
        consecutive_fails = 0
        log(f"  ✓ item {len(existing)}/{TARGET_ITEMS} from {url[:90]}")

    ckpt[occupation] = existing
    save_checkpoint(ckpt)
    return existing


# ─── Checkpoint + coverage map ────────────────────────────────────────────

CKPT_PATH = OUT / "v7_ckpt.json"
BANK_PATH = OUT / "qa_auto_source_v7.json"
COVERAGE_PATH = OUT / "coverage_map_v7.csv"


def _override_paths(ckpt=None, bank=None, coverage=None):
    """Allow CLI to override default output paths (enables parallel workers)."""
    global CKPT_PATH, BANK_PATH, COVERAGE_PATH
    if ckpt: CKPT_PATH = Path(ckpt)
    if bank: BANK_PATH = Path(bank)
    if coverage: COVERAGE_PATH = Path(coverage)


def load_checkpoint():
    if CKPT_PATH.exists():
        return json.loads(CKPT_PATH.read_text())
    return {}


def save_checkpoint(ckpt):
    CKPT_PATH.write_text(json.dumps(ckpt, indent=2))
    all_items = []
    for occ, items in ckpt.items():
        all_items.extend(items)
    BANK_PATH.write_text(json.dumps(all_items, indent=2))
    # Coverage map
    with COVERAGE_PATH.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["occupation", "n_items", "source_urls_sample"])
        for occ, items in sorted(ckpt.items()):
            sample = "; ".join(i["source_url"] for i in items[:3])
            w.writerow([occ, len(items), sample])


# ─── CLI ──────────────────────────────────────────────────────────────────

def load_occupations_from_csv(path, limit=None):
    import pandas as pd
    df = pd.read_csv(path)
    col = "occupation_title" if "occupation_title" in df.columns else df.columns[0]
    occs = df[col].drop_duplicates().tolist()
    return occs[:limit] if limit else occs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--occupations", nargs="+", default=None,
                        help="explicit occupation names")
    parser.add_argument("--from-csv", default=None,
                        help="load occupations from a CSV column")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--ckpt-path", default=None,
                        help="override checkpoint path (for parallel workers)")
    parser.add_argument("--bank-path", default=None,
                        help="override raw bank path")
    parser.add_argument("--coverage-path", default=None,
                        help="override coverage map path")
    parser.add_argument("--log-path", default=None,
                        help="override log path")
    args = parser.parse_args()
    _override_paths(ckpt=args.ckpt_path, bank=args.bank_path,
                    coverage=args.coverage_path)

    if args.occupations:
        occupations = args.occupations
    elif args.from_csv:
        occupations = load_occupations_from_csv(args.from_csv, limit=args.limit)
    else:
        occupations = ["Plumbers", "Phlebotomists", "Dental Hygienists",
                       "Credit Counselors", "Food Service Managers"]

    random.seed(42)
    log_path = Path(args.log_path) if args.log_path else OUT / "auto_source_v7.log"
    log_f = open(log_path, "a", buffering=1)
    def log(msg):
        print(msg, flush=True); log_f.write(msg + "\n")

    ckpt = load_checkpoint()
    log(f"\n\n===== RUN START: {len(occupations)} occupations =====")
    log(f"Target: {TARGET_ITEMS} items per occupation")
    for occ in occupations:
        try:
            process_occupation(occ, ckpt, log)
        except Exception as e:
            log(f"  OCC ERROR {occ}: {type(e).__name__}: {e}")
            save_checkpoint(ckpt)

    save_checkpoint(ckpt)
    total = sum(len(v) for v in ckpt.values())
    covered = sum(1 for v in ckpt.values() if len(v) >= TARGET_ITEMS)
    log(f"\n===== RUN END =====  total={total}  full-coverage occupations={covered}/{len(occupations)}")
    log_f.close()


if __name__ == "__main__":
    main()
