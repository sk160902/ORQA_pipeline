"""
Step 65: Auto-source prototype v3. Multi-venue source finding that mirrors the
316-question pipeline (Reddit + Stack Exchange + PubMed + OpenAI web_search
+ Wikipedia), with single-answer items whose wrong distractors come from the
SAME source.

Per-source distractor strategies:
  - Community thread (Reddit, SE): non-top answers classified WRONG vs the top
    answer by a judge LLM.
  - Authoritative document (web_search result, PubMed, Wikipedia): the generator
    LLM extracts the top-recommended approach as the correct option and three
    alternative approaches the document describes as incorrect, outdated,
    common-misconception, or explicitly-not-recommended.

All items are single-answer. Scenario framing is first-person help-seeking:
"I'm stuck on X, what should I do?"

Output: output/qa_auto_source_v3.json + output/auto_source_v3.log
"""
import os, re, json, time, html, ssl, random
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
SE_KEY = (ROOT/"api_key_stackexchange.txt").read_text().strip() if (ROOT/"api_key_stackexchange.txt").exists() else ""
oa = OpenAI(api_key=(ROOT/"api_key.txt").read_text().strip())
GEN_MODEL = "gpt-4o"
JUDGE_MODEL = "gpt-4o"
SEARCH_MODEL = "gpt-4o-mini"


TASKS = [
    {"occupation": "Credit Counselors",
     "task": "Advise clients on effective strategies for paying off multiple debts with different interest rates.",
     "subreddits": ["personalfinance", "debtfree", "creditcards"],
     "se_sites": ["money"]},
    {"occupation": "Plumbers",
     "task": "Diagnose a slow-draining bathroom sink where the homeowner reports a foul odor.",
     "subreddits": ["Plumbing", "HomeImprovement", "DIY"],
     "se_sites": ["diy"]},
    {"occupation": "Phlebotomists",
     "task": "Collect a blood sample from a patient who has reported fainting during previous blood draws.",
     "subreddits": ["nursing", "phlebotomy", "medicine"],
     "se_sites": ["medicalsciences"]},
    {"occupation": "Food Service Managers",
     "task": "Handle a customer complaint where a diner claims to have found a foreign object in their food.",
     "subreddits": ["KitchenConfidential", "restaurateur"],
     "se_sites": ["workplace"]},
    {"occupation": "Dental Hygienists",
     "task": "Advise a patient on appropriate oral-hygiene care after receiving dental implants.",
     "subreddits": ["askdentists", "Dentistry"],
     "se_sites": ["medicalsciences"]},
]


# ─── HTTP ─────────────────────────────────────────────────────────────────

def fetch(url, timeout=20):
    import gzip as _g
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept":"*/*"})
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


def html_to_text(s):
    if isinstance(s, bytes): s = s.decode("utf-8", errors="replace")
    s = re.sub(r"<script[\s\S]*?</script>", "", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ─── Source gatherers (one per venue) ─────────────────────────────────────

def reddit_search(query, subreddits=None, limit=15):
    """Search Reddit globally + within preferred subreddits."""
    out = []
    # Global search
    q = urllib.parse.quote(query)
    u = f"https://old.reddit.com/search.json?q={q}&sort=top&t=year&limit={limit}"
    try:
        d = json.loads(fetch(u))
        for c in d.get("data",{}).get("children",[]):
            x = c.get("data",{})
            if x.get("num_comments",0) < 5: continue
            out.append({
                "subreddit": x.get("subreddit",""),
                "permalink": x.get("permalink",""),
                "title": x.get("title",""),
                "body": x.get("selftext","") or "",
                "score": x.get("score",0),
                "link": f"https://www.reddit.com{x.get('permalink','')}",
            })
    except Exception: pass
    # Subreddit-scoped search for biased results
    for sub in (subreddits or []):
        q2 = urllib.parse.quote(query)
        u2 = f"https://old.reddit.com/r/{sub}/search.json?q={q2}&restrict_sr=1&sort=top&t=all&limit=10"
        try:
            d = json.loads(fetch(u2))
            for c in d.get("data",{}).get("children",[]):
                x = c.get("data",{})
                if x.get("num_comments",0) < 5: continue
                out.append({
                    "subreddit": x.get("subreddit",""),
                    "permalink": x.get("permalink",""),
                    "title": x.get("title",""),
                    "body": x.get("selftext","") or "",
                    "score": x.get("score",0),
                    "link": f"https://www.reddit.com{x.get('permalink','')}",
                })
        except Exception: pass
    # dedup by permalink
    seen, uniq = set(), []
    for p in out:
        if p["permalink"] not in seen:
            seen.add(p["permalink"]); uniq.append(p)
    return uniq


def reddit_thread(post):
    u = f"https://old.reddit.com{post['permalink']}.json?sort=top&limit=25"
    try:
        d = json.loads(fetch(u))
    except Exception:
        return None
    if not isinstance(d, list) or len(d) < 2: return None
    comments = []
    for c in d[1].get("data",{}).get("children",[]):
        if c.get("kind") != "t1": continue
        cd = c.get("data",{}); txt = cd.get("body","") or ""
        if len(txt) < 80: continue
        comments.append({"score": cd.get("score",0), "text": txt, "is_accepted": False})
    if len(comments) < 4: return None
    return {"platform":"reddit","site":f"r/{post['subreddit']}",
            "link":post["link"],"title":post["title"],"body":post["body"],
            "answers":comments,"source_type":"community"}


def _se(url): return url + (f"&key={SE_KEY}" if SE_KEY else "")

def se_search(site, query, pagesize=10):
    q = urllib.parse.quote(query)
    u = _se(f"https://api.stackexchange.com/2.3/search/advanced?q={q}&site={site}&order=desc&sort=votes&pagesize={pagesize}")
    try:
        d = json.loads(fetch(u))
    except Exception:
        return []
    return [{"site":site,"qid":it["question_id"],"score":it.get("score",0),
             "title":it.get("title",""),"link":it.get("link","")}
            for it in d.get("items",[]) if it.get("score",0) >= 3]


def se_qa(site, qid):
    qu = _se(f"https://api.stackexchange.com/2.3/questions/{qid}?site={site}&filter=withbody")
    au = _se(f"https://api.stackexchange.com/2.3/questions/{qid}/answers?site={site}&order=desc&sort=votes&pagesize=12&filter=withbody")
    try:
        qd = json.loads(fetch(qu))["items"][0]
        ad = json.loads(fetch(au))["items"]
    except Exception:
        return None
    return {"platform":"stackexchange","site":site,
            "link":qd.get("link",""),"title":qd.get("title",""),
            "body":html_to_text(qd.get("body","")),
            "answers":[{"score":a.get("score",0),"is_accepted":a.get("is_accepted",False),
                        "text":html_to_text(a.get("body",""))} for a in ad],
            "source_type":"community"}


def pubmed_search(query, limit=3):
    q = urllib.parse.quote(query)
    u = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term={q}&retmax={limit}&retmode=json"
    try:
        d = json.loads(fetch(u))
    except Exception:
        return []
    ids = d.get("esearchresult",{}).get("idlist",[])
    if not ids: return []
    u2 = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={','.join(ids)}&retmode=xml"
    try:
        xml = fetch(u2).decode("utf-8", errors="replace")
    except Exception:
        return []
    results = []
    for pmid, block in zip(ids, xml.split("</PubmedArticle>")[:len(ids)]):
        title_m = re.search(r"<ArticleTitle>(.+?)</ArticleTitle>", block, re.DOTALL)
        abs_m = re.search(r"<AbstractText[^>]*>(.+?)</AbstractText>", block, re.DOTALL)
        if not abs_m: continue
        results.append({
            "title": html_to_text(title_m.group(1)) if title_m else "",
            "abstract": html_to_text(abs_m.group(1)),
            "link": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "source_type": "authoritative",
            "source_venue": "PubMed",
        })
    return results


def wiki_search(query, limit=3):
    """Wikipedia opensearch + summary."""
    q = urllib.parse.quote(query)
    u = f"https://en.wikipedia.org/w/api.php?action=opensearch&search={q}&limit={limit}&format=json"
    try:
        d = json.loads(fetch(u))
    except Exception:
        return []
    titles = d[1] if len(d) > 1 else []
    links = d[3] if len(d) > 3 else []
    results = []
    for title, link in zip(titles, links):
        sum_u = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
        try:
            s = json.loads(fetch(sum_u))
            if s.get("extract"):
                results.append({"title": title, "body": s["extract"], "link": link,
                                "source_type":"authoritative","source_venue":"Wikipedia"})
        except Exception: pass
    return results


def web_search_authoritative(query):
    """Use OpenAI web_search_preview tool to find agency / professional guidelines."""
    prompt = (f"Find professional guidelines, clinical protocols, agency regulations, or "
              f"trade association publications that specify best practices for: {query}. "
              "Prefer pages from government agencies (.gov), professional associations (.org), "
              "licensing bodies, or published standards.")
    try:
        resp = oa.responses.create(
            model=SEARCH_MODEL,
            tools=[{"type": "web_search_preview"}],
            input=prompt,
        )
    except Exception:
        return []
    results, seen = [], set()
    for item in getattr(resp, "output", []):
        for c in getattr(item, "content", None) or []:
            for a in getattr(c, "annotations", None) or []:
                u = getattr(a, "url", None)
                if u and u not in seen:
                    seen.add(u)
                    results.append({"link": u, "title": getattr(a,"title","") or "",
                                    "source_type":"authoritative","source_venue":"web"})
    return results


def fetch_authoritative_body(r):
    """Fetch the actual page body for an authoritative URL."""
    try:
        raw = fetch(r["link"])
        body = html_to_text(raw)
        return body if len(body) > 500 else None
    except Exception:
        return None


# ─── Judge + generation ───────────────────────────────────────────────────

JUDGE_PROMPT = """You are judging whether a secondary answer on a community thread is GENUINELY INCORRECT compared to the top/accepted answer, or whether it is merely less detailed but broadly consistent.

Return only one word: "WRONG" if the secondary answer contradicts, misleads about, or gives advice the top answer would call incorrect. "OK" if it is on the same general track, just shorter or partial.

TOP/ACCEPTED ANSWER:
\"\"\"
{top}
\"\"\"

SECONDARY ANSWER:
\"\"\"
{other}
\"\"\"

Label (WRONG or OK):"""


SCENARIO_PROMPT = """Write a first-person on-the-job help-seeking scenario, in the voice of a {occupation}. The scenario MUST be grounded in the real situation from the source below (keep named systems, tools, constraints, specifics). The scenario should read like the worker is stuck or unsure and asking for help, not a polished textbook question. Target length: 80-180 words. End with a single concrete question ("what should I do?" or similar).

SOURCE TITLE: {title}
SOURCE BODY:
\"\"\"
{body}
\"\"\"

Output only the scenario paragraph. Do not say "according to the source"."""


COMMUNITY_PARAPHRASE_PROMPT = """Paraphrase each of the four community responses below into a single 20-40 word option for a multiple-choice question. Preserve the specific advice or mistaken claim in each. Output exactly four lines:
1) <paraphrase of RESPONSE 1>
2) <paraphrase of RESPONSE 2>
3) <paraphrase of RESPONSE 3>
4) <paraphrase of RESPONSE 4>

RESPONSE 1 (the correct, accepted / top-voted answer):
{r1}

RESPONSE 2 (wrong):
{r2}

RESPONSE 3 (wrong):
{r3}

RESPONSE 4 (wrong):
{r4}
"""


AUTHORITATIVE_OPTIONS_PROMPT = """You are building multiple-choice options for a workplace question for a {occupation}. The authoritative source below describes the recommended approach along with alternative approaches it considers inferior, outdated, common-misconception, or explicitly-not-recommended.

Produce exactly FOUR options, each 20-40 words:
- The FIRST option is a paraphrase of the recommended approach as stated in the source (this will be the correct answer).
- The NEXT THREE options paraphrase three distinct alternative approaches that the source explicitly calls out as incorrect, outdated, less effective, or common misconceptions. Each must be tied to specific content in the source, not invented. If the source does not contain three distinct incorrect alternatives, output only the line "INSUFFICIENT".

AUTHORITATIVE SOURCE ({venue}):
TITLE: {title}
BODY:
\"\"\"
{body}
\"\"\"

Output format (exactly four lines, numbered 1-4):
1) <recommended approach>
2) <incorrect alternative 1>
3) <incorrect alternative 2>
4) <incorrect alternative 3>

Or, if insufficient, output exactly: INSUFFICIENT
"""


def first_sentences(t, max_chars=220):
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) <= max_chars: return t
    m = re.search(r'[.!?](\s|$)', t[60:max_chars+60])
    return (t[:60 + m.start() + 1] if m else t[:max_chars]).strip().rstrip("…")


def parse_numbered(s):
    out = {}
    for num in "1234":
        m = re.search(rf'^\s*{num}\)\s*(.+?)(?=\n\s*\d\)|\Z)', s, re.MULTILINE | re.DOTALL)
        if m: out[num] = first_sentences(m.group(1).strip(), 260)
    return out


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


def judge_wrong(top_text, other_text):
    raw = call_openai(JUDGE_PROMPT.format(top=top_text[:1500], other=other_text[:1500]),
                      max_tokens=10, temperature=0.0, model=JUDGE_MODEL)
    up = (raw or "").upper()
    return "WRONG" in up


# ─── Item builders ────────────────────────────────────────────────────────

def passes_consensus(thread):
    ans = thread["answers"]
    if len(ans) < 4: return False
    top, nxt = ans[0], ans[1]
    if top["score"] < 5: return False
    if top["score"] < 3 * max(1, nxt["score"]): return False
    if thread["platform"] == "stackexchange" and not top.get("is_accepted"): return False
    return True


def gen_scenario(occ, title, body):
    s = call_openai(SCENARIO_PROMPT.format(
        occupation=occ, title=title, body=(body or title)[:2500]),
        max_tokens=400, temperature=0.4)
    return s if s and len(s) > 60 else None


def build_item_community(occ, task, thread, log):
    if not passes_consensus(thread): return None, "consensus_fail"
    top = thread["answers"][0]
    wrong = []
    for a in thread["answers"][1:]:
        if len(wrong) >= 6: break
        if not a["text"].strip(): continue
        if judge_wrong(top["text"], a["text"]):
            wrong.append(a)
    if len(wrong) < 3: return None, f"not_enough_wrong({len(wrong)})"
    random.shuffle(wrong); distractors = wrong[:3]

    scenario = gen_scenario(occ, thread["title"], thread.get("body",""))
    if not scenario: return None, "no_scenario"

    raw = call_openai(COMMUNITY_PARAPHRASE_PROMPT.format(
        r1=top["text"][:1500], r2=distractors[0]["text"][:1500],
        r3=distractors[1]["text"][:1500], r4=distractors[2]["text"][:1500]),
        max_tokens=500, temperature=0.3)
    paras = parse_numbered(raw)
    if len(paras) < 4: return None, "paraphrase_fail"

    return _assemble_item(occ, task, scenario, paras, thread["link"],
                          thread["platform"], "community", thread.get("site","")), None


def build_item_authoritative(occ, task, source, log):
    body = source.get("body") or source.get("abstract")
    if not body:
        body = fetch_authoritative_body(source)
    if not body or len(body) < 500:
        return None, "no_body"
    scenario = gen_scenario(occ, source.get("title",""), body)
    if not scenario: return None, "no_scenario"
    raw = call_openai(AUTHORITATIVE_OPTIONS_PROMPT.format(
        occupation=occ, venue=source.get("source_venue","authoritative"),
        title=source.get("title",""), body=body[:3500]),
        max_tokens=500, temperature=0.3)
    if "INSUFFICIENT" in raw.upper(): return None, "insufficient_alternatives"
    paras = parse_numbered(raw)
    if len(paras) < 4: return None, "paraphrase_fail"
    return _assemble_item(occ, task, scenario, paras, source["link"],
                          "authoritative", source.get("source_type","authoritative"),
                          source.get("source_venue","")), None


def _assemble_item(occ, task, scenario, paras, link, platform, src_type, venue):
    letters = list("ABCD")
    random.shuffle(letters)
    options = {}
    for i, letter in enumerate(letters):
        options[letter] = paras[str(i+1)]
    correct_letter = letters[0]
    options["E"] = "All of the above"
    options["F"] = "None of the above"
    options = {k: options[k] for k in sorted(options)}
    return {
        "occupation": occ, "onet_task": task,
        "question": scenario,
        "options": options,
        "correct_answer": correct_letter,
        "answer_type": "single",
        "source_url": link,
        "source_platform": platform,
        "source_type": src_type,
        "source_venue": venue,
        "distractor_source_method": "same_source",
    }


# ─── orchestration ────────────────────────────────────────────────────────

def process_task(task_cfg, log):
    occ = task_cfg["occupation"]; task = task_cfg["task"]
    log(f"\n=== {occ} ===  Task: {task}")

    # Gather from all venues, round-robin
    community_threads = []
    auth_sources = []

    # 1. Reddit (global + preferred subreddits)
    posts = reddit_search(task, subreddits=task_cfg.get("subreddits"), limit=10)
    log(f"  Reddit search: {len(posts)} posts")
    for p in posts[:8]:
        t = reddit_thread(p); time.sleep(0.5)
        if t and passes_consensus(t):
            community_threads.append(t)

    # 2. Stack Exchange (selected sites)
    for site in task_cfg.get("se_sites", []):
        cands = se_search(site, task, pagesize=8)
        log(f"  SE[{site}] search: {len(cands)} candidates")
        for c in cands[:4]:
            t = se_qa(site, c["qid"]); time.sleep(0.3)
            if t and passes_consensus(t):
                community_threads.append(t)

    # 3. PubMed
    pm = pubmed_search(task, limit=3)
    log(f"  PubMed: {len(pm)} results")
    auth_sources.extend(pm)

    # 4. OpenAI web_search for authoritative content
    ws = web_search_authoritative(task)
    log(f"  Web (authoritative) search: {len(ws)} URLs")
    auth_sources.extend(ws)

    # 5. Wikipedia fallback
    wk = wiki_search(task, limit=3)
    log(f"  Wikipedia: {len(wk)} results")
    auth_sources.extend(wk)

    log(f"  consensus-passing community threads: {len(community_threads)}")
    log(f"  authoritative source candidates: {len(auth_sources)}")

    # Try community threads first, then authoritative
    random.shuffle(community_threads)
    for thread in community_threads[:6]:
        item, err = build_item_community(occ, task, thread, log)
        if item:
            log(f"  ✓ community item from {thread['link'][:90]}")
            return item
        else:
            log(f"    community fail ({err})")

    random.shuffle(auth_sources)
    for source in auth_sources[:8]:
        item, err = build_item_authoritative(occ, task, source, log)
        if item:
            log(f"  ✓ authoritative item from {source.get('link','')[:90]}")
            return item
        else:
            log(f"    authoritative fail ({err}) src={source.get('link','')[:60]}")

    log("  SKIP: no valid item from any venue")
    return None


def main():
    random.seed(42)
    out_path = OUT / "qa_auto_source_v3.json"
    log_path = OUT / "auto_source_v3.log"
    log_f = open(log_path, "w", buffering=1)
    def log(msg): print(msg, flush=True); log_f.write(msg + "\n")

    items = []
    for task_cfg in TASKS:
        rec = process_task(task_cfg, log)
        if rec: items.append(rec)
        out_path.write_text(json.dumps(items, indent=2))

    log(f"\nGenerated {len(items)} items; saved to {out_path}")
    log_f.close()


if __name__ == "__main__":
    main()
