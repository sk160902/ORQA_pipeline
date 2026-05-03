"""
Step 64: Auto-source prototype v2 (single-answer only, same-thread wrong
options that are actually incorrect, first-person help-seeking scenario).

Design:
  - For each O*NET task, search Reddit and Stack Exchange directly (via their
    own search APIs, not the generic web_search tool, to avoid 403/404 churn).
  - Keep threads with an accepted/top-voted answer plus at least 3 other
    answers, and with the top answer clearly dominant.
  - For each non-top answer in the thread, have a classifier LLM decide
    whether its advice is INCORRECT relative to the accepted answer (not just
    "less detailed"). Keep only answers flagged as incorrect.
  - Need at least 3 same-thread "definitely wrong" answers to proceed.
  - Generate a single-choice MCQ where:
      * Scenario reads as first-person, help-seeking ("I'm stuck on X, what
        should I do?").
      * Option A = paraphrase of accepted/top answer (correct).
      * Options B, C, D = paraphrases of three "definitely wrong" same-thread
        answers.
      * Letter mapping is scrambled so the correct letter is not always A.
      * E = "All of the above", F = "None of the above".
  - Run on 5 tasks spanning coverage levels.

Output: output/qa_auto_source_v2.json + output/auto_source_v2.log
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


TASKS = [
    {"occupation": "Credit Counselors",
     "task": "Advise clients on effective strategies for paying off multiple debts with different interest rates.",
     "se_sites": ["money", "personalfinance"]},
    {"occupation": "Plumbers",
     "task": "Diagnose a slow-draining bathroom sink where the homeowner reports a foul odor.",
     "se_sites": ["diy", "home"]},
    {"occupation": "Phlebotomists",
     "task": "Collect a blood sample from a patient who has reported fainting during previous blood draws.",
     "se_sites": ["medicalsciences", "health"]},
    {"occupation": "Food Service Managers",
     "task": "Handle a customer complaint where a diner claims to have found a foreign object in their food.",
     "se_sites": ["workplace", "hospitality"]},
    {"occupation": "Dental Hygienists",
     "task": "Advise a patient on appropriate oral-hygiene care after receiving dental implants.",
     "se_sites": ["medicalsciences", "health"]},
]


# ─── HTTP ─────────────────────────────────────────────────────────────────

def fetch(url, timeout=20):
    import gzip as _g
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
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


# ─── Reddit search ─────────────────────────────────────────────────────────

def reddit_search(query, limit=15):
    """Search Reddit globally via the search.json endpoint."""
    q = urllib.parse.quote(query)
    u = f"https://old.reddit.com/search.json?q={q}&sort=top&t=year&limit={limit}"
    try:
        d = json.loads(fetch(u))
    except Exception as e:
        return []
    out = []
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
    return out


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
    return {"platform":"reddit","site":f"r/{post['subreddit']}","qid":post.get("permalink","").split("/")[-2] if post.get("permalink") else "",
            "link":post["link"],"title":post["title"],"body":post["body"],"answers":comments}


# ─── Stack Exchange search ────────────────────────────────────────────────

def _se(url): return url + (f"&key={SE_KEY}" if SE_KEY else "")

def se_search(site, query, pagesize=15):
    """Search a specific SE site for relevant questions."""
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
    return {"platform":"stackexchange","site":site,"qid":qid,
            "link":qd.get("link",""),"title":qd.get("title",""),
            "body":html_to_text(qd.get("body","")),
            "answers":[{"score":a.get("score",0),"is_accepted":a.get("is_accepted",False),
                        "text":html_to_text(a.get("body",""))} for a in ad]}


def passes_consensus(thread):
    ans = thread["answers"]
    if len(ans) < 4: return False
    top, nxt = ans[0], ans[1]
    if top["score"] < 5: return False
    if top["score"] < 3 * max(1, nxt["score"]): return False
    if thread["platform"] == "stackexchange" and not top.get("is_accepted"): return False
    return True


# ─── LLM ──────────────────────────────────────────────────────────────────

def call_openai(prompt, max_tokens=800, temperature=0.2):
    for attempt in range(3):
        try:
            r = oa.chat.completions.create(
                model=GEN_MODEL, temperature=temperature, max_tokens=max_tokens,
                messages=[{"role":"user","content":prompt}])
            return (r.choices[0].message.content or "").strip()
        except Exception:
            if attempt == 2: return ""
            time.sleep(1.5*(attempt+1))


# --- Judge: classify each non-top answer as wrong or merely less-detailed ---

JUDGE_PROMPT = """You are judging whether a secondary answer on a community thread is GENUINELY INCORRECT compared to the top/accepted answer, or whether it is merely less detailed but still broadly consistent.

Return only one word: "WRONG" if the secondary answer contradicts, misleads about, or gives advice the top answer would call incorrect. "OK" if it is on the same general track but shorter or partial.

TOP/ACCEPTED ANSWER:
\"\"\"
{top}
\"\"\"

SECONDARY ANSWER:
\"\"\"
{other}
\"\"\"

Label (WRONG or OK):"""


def judge_wrong(top_text, other_text):
    raw = call_openai(JUDGE_PROMPT.format(top=top_text[:1500], other=other_text[:1500]),
                      max_tokens=10, temperature=0.0)
    up = (raw or "").upper()
    return "WRONG" in up and "OK" not in up


# --- Generation: scenario + paraphrase 4 options ---

SCENARIO_PROMPT = """Write a first-person on-the-job help-seeking scenario, in the voice of a {occupation}. The scenario MUST be grounded in the real situation from the community thread below (keep named systems, tools, constraints, specifics). The scenario should read like the worker is stuck or unsure and asking for help, not a polished textbook question. Target length: 80-180 words. End with a single concrete question ("what should I do?" or similar).

ORIGINAL COMMUNITY THREAD TITLE: {title}
ORIGINAL COMMUNITY THREAD BODY:
\"\"\"
{body}
\"\"\"

Output only the scenario paragraph. Do not say "according to the source" or "according to the top answer"."""


PARAPHRASE_FOUR_PROMPT = """Paraphrase each of the four community responses below into a single 20-40 word option for a multiple-choice question. Preserve the specific advice or the specific mistaken claim in each. Output exactly four lines:
1) <paraphrase of RESPONSE 1>
2) <paraphrase of RESPONSE 2>
3) <paraphrase of RESPONSE 3>
4) <paraphrase of RESPONSE 4>

RESPONSE 1:
{r1}

RESPONSE 2:
{r2}

RESPONSE 3:
{r3}

RESPONSE 4:
{r4}
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


def gen_scenario(occ, thread):
    s = call_openai(SCENARIO_PROMPT.format(
        occupation=occ, title=thread["title"], body=(thread["body"] or thread["title"])[:2500]),
        max_tokens=400, temperature=0.4)
    return s if s and len(s) > 60 else None


def gen_item(occ, thread, log):
    """Build a single-answer MCQ with 3 same-thread wrong options judged wrong."""
    ans = thread["answers"]
    if not passes_consensus(thread): return None, "consensus_fail"

    top = ans[0]
    # Classify each non-top answer
    wrong_candidates = []
    for a in ans[1:]:
        if not a["text"].strip(): continue
        if judge_wrong(top["text"], a["text"]):
            wrong_candidates.append(a)
        if len(wrong_candidates) >= 6: break  # don't judge more than needed

    if len(wrong_candidates) < 3:
        return None, f"not_enough_wrong({len(wrong_candidates)})"

    # Pick 3 wrong distractors
    random.shuffle(wrong_candidates)
    distractors = wrong_candidates[:3]

    # Generate scenario
    scenario = gen_scenario(occ, thread)
    if not scenario: return None, "no_scenario"

    # Paraphrase top + 3 wrong into options 1-4
    raw = call_openai(PARAPHRASE_FOUR_PROMPT.format(
        r1=top["text"][:1500],
        r2=distractors[0]["text"][:1500],
        r3=distractors[1]["text"][:1500],
        r4=distractors[2]["text"][:1500]),
        max_tokens=500, temperature=0.3)
    paras = parse_numbered(raw)
    if len(paras) < 4: return None, "paraphrase_fail"

    # Scramble letter mapping so the correct letter isn't always A
    letters = list("ABCD")
    random.shuffle(letters)
    # letters[0] corresponds to response 1 (the top/correct)
    # letters[1:4] correspond to responses 2,3,4 (wrong)
    options = {}
    for i, letter in enumerate(letters):
        options[letter] = paras[str(i+1)]
    correct_letter = letters[0]
    options["E"] = "All of the above"
    options["F"] = "None of the above"
    # Reorder options dict alphabetically for presentation
    options = {k: options[k] for k in sorted(options)}

    return {
        "occupation": occ,
        "onet_task": None,  # set by caller
        "question": scenario,
        "options": options,
        "correct_answer": correct_letter,
        "answer_type": "single",
        "source_url": thread["link"],
        "source_platform": thread["platform"],
        "consensus_meta": {
            "top_answer_score": top["score"],
            "next_answer_score": ans[1]["score"],
            "top_next_ratio": top["score"] / max(1, ans[1]["score"]),
            "n_wrong_candidates_found": len(wrong_candidates),
        },
        "distractor_source_method": "same_thread_judged_wrong",
    }, None


# ─── orchestration ────────────────────────────────────────────────────────

def process_task(task_cfg, log):
    occ = task_cfg["occupation"]; task = task_cfg["task"]
    log(f"\n=== {occ} ===  Task: {task}")

    threads = []
    # Reddit search
    posts = reddit_search(task, limit=12)
    log(f"  Reddit search: {len(posts)} posts")
    for p in posts[:6]:
        t = reddit_thread(p)
        time.sleep(0.6)
        if t and passes_consensus(t):
            threads.append(t)

    # SE search across listed sites
    for site in task_cfg.get("se_sites", []):
        cands = se_search(site, task, pagesize=10)
        log(f"  SE[{site}] search: {len(cands)} candidates")
        for c in cands[:5]:
            t = se_qa(site, c["qid"])
            time.sleep(0.3)
            if t and passes_consensus(t):
                threads.append(t)

    log(f"  consensus-passing threads: {len(threads)}")
    if not threads:
        log("  SKIP: no consensus-passing thread"); return None

    random.shuffle(threads)
    for thread in threads[:6]:
        item, err = gen_item(occ, thread, log)
        if item:
            item["onet_task"] = task
            log(f"  ✓ generated from {thread['link'][:90]}")
            return item
        else:
            log(f"  try next: {err} from {thread['link'][:80]}")
    log("  SKIP: no thread produced a valid item")
    return None


def main():
    random.seed(42)
    out_path = OUT / "qa_auto_source_v2.json"
    log_path = OUT / "auto_source_v2.log"
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
