"""
Step 63: Prototype auto-source hybrid MCQ generation.

Goal: given an O*NET task, find sources automatically (no hardcoded subreddit,
SE site, or framework URL list) and generate a hybrid MCQ whose scenario comes
from a practitioner community thread and whose answer is anchored to a
professional guideline or manual.

Two parallel searches per task:
  A. "practitioner Q&A" search — finds Reddit/SE/AskMe/niche-forum threads
     where humans reasoned through the situation.
  B. "authoritative guideline" search — finds government/professional/
     trade-association publications covering the same task.

Each returned URL is domain-classified into community vs authoritative, one
of each is picked, content is fetched, and the item is generated with:
  - scenario = paraphrased from the community thread (retain specifics)
  - correct answer = derivable from the authoritative document
  - answerability-checked against the authoritative document alone

Run on 5 tasks chosen to span coverage: heavy Reddit (Credit Counselors),
thin Reddit (Plumbers, Phlebotomists), medium (Food Service Managers, Dental
Hygienists).

Output: output/qa_auto_source_pilot.json and output/auto_source.log
"""
import os, re, json, time, html, ssl, random
from pathlib import Path
import urllib.request, urllib.error
from openai import OpenAI
import anthropic

ROOT = Path(__file__).parent
OUT = ROOT / "output"

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = ssl.create_default_context()
    SSL_CTX.check_hostname = False; SSL_CTX.verify_mode = ssl.CERT_NONE

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15"
oa = OpenAI(api_key=(ROOT/"api_key.txt").read_text().strip())
cla = anthropic.Anthropic(api_key=(ROOT/"api_key_anthropic.txt").read_text().strip())

SEARCH_MODEL = "gpt-4o-mini"      # cheap + has web_search_preview tool
GEN_MODEL    = "claude-opus-4-6"
CHECK_MODEL  = "claude-sonnet-4-6"


# ─── O*NET tasks to pilot on ────────────────────────────────────────────────

TASKS = [
    {
        "occupation": "Credit Counselors",
        "task": "Advise clients on effective strategies for paying off multiple debts with different interest rates.",
    },
    {
        "occupation": "Plumbers",
        "task": "Diagnose and repair a slow-draining bathroom sink where the homeowner reports a foul odor.",
    },
    {
        "occupation": "Phlebotomists",
        "task": "Collect a blood sample from a patient who has reported fainting during previous blood draws.",
    },
    {
        "occupation": "Food Service Managers",
        "task": "Handle a customer complaint where a diner claims to have found a foreign object in their food.",
    },
    {
        "occupation": "Dental Hygienists",
        "task": "Advise a patient on appropriate oral-hygiene care after receiving dental implants.",
    },
]


# ─── Domain classification (community vs authoritative) ────────────────────

COMMUNITY_DOMAINS = [
    "reddit.com", "stackexchange.com", "stackoverflow.com", "serverfault.com",
    "superuser.com", "askubuntu.com", "metafilter.com", "quora.com",
    "allnurses.com", "truckersreport.com", "electriciantalk.com",
    "plumbingzone.com", "plumbersforums.net", "hvac-talk.com", "weldingweb.com",
    "contractortalk.com", "garagejournal.com", "avsforum.com",
    "ycombinator.com", "bogleheads.org",
]

AUTHORITATIVE_DOMAIN_PATTERNS = [
    r"\.gov$", r"\.gov\.", r"\.mil$", r"\.edu$",
    # Known authoritative bodies / trade associations / publishers
    r"\bada\.org$", r"\bama-assn\.org$", r"\baicpa\.org$", r"\basce\.org$",
    r"\bashrae\.org$", r"\biee\.org$", r"\bieee\.org$", r"\biso\.org$",
    r"\bnfpa\.org$", r"\bnrl\.navy\.mil$", r"\bowasp\.org$",
    r"\bw3\.org$", r"\bwho\.int$", r"\bservsafe\.com$",
    r"developer\.mozilla\.org$", r"datatracker\.ietf\.org$",
    r"\bnih\.gov$", r"\bcdc\.gov$", r"\bfda\.gov$", r"\bosha\.gov$",
    r"\birs\.gov$", r"\bftc\.gov$", r"\btsa\.gov$", r"\bdot\.gov$",
    r"\bhud\.gov$",
]

def classify_url(url):
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower().strip()
    # community
    for d in COMMUNITY_DOMAINS:
        if host == d or host.endswith("." + d):
            return "community"
    # authoritative
    for pat in AUTHORITATIVE_DOMAIN_PATTERNS:
        if re.search(pat, host):
            return "authoritative"
    # professional / industry association heuristic
    if host.endswith(".org"):
        return "authoritative"
    return "other"


# ─── Web search via OpenAI `web_search_preview` ────────────────────────────

def web_search(query):
    """Return a list of {url, title, snippet} from OpenAI's web search tool."""
    try:
        resp = oa.responses.create(
            model=SEARCH_MODEL,
            tools=[{"type": "web_search_preview"}],
            input=query,
        )
    except Exception as e:
        print(f"  web_search error: {e}", flush=True)
        return []
    urls = []
    seen = set()
    for item in getattr(resp, "output", []):
        content = getattr(item, "content", None) or []
        for c in content:
            anns = getattr(c, "annotations", None) or []
            for a in anns:
                url = getattr(a, "url", None)
                if url and url not in seen:
                    seen.add(url)
                    urls.append({
                        "url": url,
                        "title": getattr(a, "title", "") or "",
                    })
    return urls


COMMUNITY_QUERY = (
    "Find threads, forum posts, or community Q&A pages where practitioners, "
    "workers, or knowledgeable members of the public have discussed and reasoned "
    "through the following workplace situation. Prefer pages from Reddit, Stack "
    "Exchange, Ask MetaFilter, or niche profession forums (allnurses, "
    "truckersreport, electriciantalk, plumbingzone, hvac-talk, and similar). "
    "Task / situation: {task}"
)

AUTHORITATIVE_QUERY = (
    "Find professional guidelines, training manuals, agency regulations, "
    "clinical practice protocols, trade-association publications, or certification "
    "reference material that specifies the recommended best practices or rules "
    "for the following task. Prefer pages from government agencies (.gov), "
    "professional associations (.org), licensing bodies, or published standards. "
    "Task: {task}"
)


# ─── HTTP fetch + cleaning ─────────────────────────────────────────────────

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                data = r.read()
            import gzip as _g
            if data[:2] == bytes([0x1f, 0x8b]):
                data = _g.decompress(data)
            return data.decode("utf-8", errors="replace")
        except Exception as e:
            if attempt == 1: raise
            time.sleep(1.5)


def html_to_text(s):
    s = re.sub(r"<script[\s\S]*?</script>", "", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def trim(text, max_chars=4000):
    return text[:max_chars]


# ─── Generation ────────────────────────────────────────────────────────────

def call_claude(model, prompt, max_tokens=2000, temperature=0.2):
    for attempt in range(3):
        try:
            r = cla.messages.create(
                model=model, max_tokens=max_tokens, temperature=temperature,
                messages=[{"role":"user","content":prompt}])
            return "".join(b.text for b in r.content if getattr(b,"type","")=="text").strip()
        except Exception as e:
            if attempt == 2: return f"ERR:{type(e).__name__}"
            time.sleep(2 * (attempt + 1))


GEN_PROMPT = """You are writing a multiple-choice question for an experienced {occupation}. The workplace scenario must be derived from the COMMUNITY THREAD below (preserving every named specific, system, tool, or constraint from the poster's situation). The correct answer must be defined by, and derivable from, the AUTHORITATIVE DOCUMENT below.

OCCUPATION: {occupation}
ONET TASK: {task}

COMMUNITY THREAD (from {community_url}):
\"\"\"
{community_text}
\"\"\"

AUTHORITATIVE DOCUMENT (from {auth_url}):
\"\"\"
{auth_text}
\"\"\"

Return ONE JSON object (no markdown):
{{
  "question": "<80-180 word workplace scenario for a {occupation}, grounded in the community thread's specifics, ending in a concrete question>",
  "options": {{
    "A": "<option A text>",
    "B": "<option B text>",
    "C": "<option C text>",
    "D": "<option D text>",
    "E": "All of the above",
    "F": "None of the above"
  }},
  "correct_answer": "<single letter A-D, or comma-separated letters for multi>",
  "answer_type": "<single|multi>",
  "community_scenario_source": "<1-sentence summary of what the poster's situation was>",
  "authoritative_anchor_quote": "<the exact sentence(s) from the AUTHORITATIVE DOCUMENT that determine the correct answer>",
  "reasoning": "<2-3 sentence explanation tying the scenario to the authoritative source>"
}}

Rules:
- Do not name the authoritative framework in the question stem; the item should read as a clean workplace scenario.
- Correctness is determined by the AUTHORITATIVE DOCUMENT, not by what the community thread recommends.
- If the community thread and the authoritative document disagree, the authoritative document wins.
- Distractors must be plausible to someone who has only read the community thread."""


ANSWERABILITY_PROMPT = """You have ONLY the authoritative document below. Using only that document, answer the multiple-choice question.

AUTHORITATIVE DOCUMENT:
\"\"\"
{auth_text}
\"\"\"

QUESTION: {question}

{option_lines}

Reply with ONLY the letter(s) of the correct answer(s), comma-separated if multiple (e.g. "A,C")."""


def extract_letters(raw):
    up = (raw or "").upper()
    m = list(re.finditer(r'(?:final\s+answer|answer)\s*(?:is|:|-)?\s*\*?\*?\(?([A-F](?:\s*,\s*[A-F])*)', up))
    if m: return set(re.findall(r'[A-F]', m[-1].group(1)))
    end = re.search(r'([A-F](?:\s*,\s*[A-F])*)\s*\.?\s*$', up)
    if end: return set(re.findall(r'[A-F]', end.group(1)))
    return set(re.findall(r'(?<![A-Za-z])([A-F])(?![A-Za-z])', up))


def normalize(letters):
    s = set(l for l in letters if l in "ABCDEF")
    if s == {"E"}: return {"A","B","C","D"}
    if s == {"F"}: return set()
    return s


def parse_json_obj(s):
    m = re.search(r'\{[\s\S]*\}', s)
    if not m: return None
    try: return json.loads(m.group(0))
    except Exception: return None


# ─── Main pipeline per task ────────────────────────────────────────────────

def process_task(task_cfg, log):
    occ = task_cfg["occupation"]; task = task_cfg["task"]
    log(f"\n=== {occ} ===")
    log(f"Task: {task}")

    # 1. Community search
    log("Community search...")
    community_urls = web_search(COMMUNITY_QUERY.format(task=task))
    log(f"  returned {len(community_urls)} URLs")
    comm_sorted = []
    for u in community_urls:
        cls = classify_url(u["url"])
        if cls == "community":
            comm_sorted.append(u)
    log(f"  {len(comm_sorted)} classified as community")
    for u in comm_sorted[:5]:
        log(f"    - {u['url']}")

    # 2. Authoritative search
    log("Authoritative search...")
    auth_urls = web_search(AUTHORITATIVE_QUERY.format(task=task))
    log(f"  returned {len(auth_urls)} URLs")
    auth_sorted = []
    for u in auth_urls:
        if classify_url(u["url"]) == "authoritative":
            auth_sorted.append(u)
    log(f"  {len(auth_sorted)} classified as authoritative")
    for u in auth_sorted[:5]:
        log(f"    - {u['url']}")

    if not comm_sorted or not auth_sorted:
        log("  SKIP: missing community or authoritative source")
        return None

    # 3. Fetch content for the top candidate in each bucket
    community_text, auth_text = None, None
    community_url, auth_url = None, None
    for u in comm_sorted[:4]:
        try:
            raw = fetch(u["url"])
            txt = html_to_text(raw)
            if len(txt) > 500:
                community_text = txt; community_url = u["url"]; break
        except Exception as e:
            log(f"    community fetch fail {u['url']}: {e}")
    for u in auth_sorted[:4]:
        try:
            raw = fetch(u["url"])
            txt = html_to_text(raw)
            if len(txt) > 500:
                auth_text = txt; auth_url = u["url"]; break
        except Exception as e:
            log(f"    authoritative fetch fail {u['url']}: {e}")

    if not community_text or not auth_text:
        log("  SKIP: couldn't fetch both sources")
        return None

    log(f"  community source: {community_url}  ({len(community_text)} chars)")
    log(f"  authoritative source: {auth_url}  ({len(auth_text)} chars)")

    # 4. Generate hybrid item
    raw = call_claude(GEN_MODEL,
                       GEN_PROMPT.format(
                           occupation=occ, task=task,
                           community_url=community_url, community_text=trim(community_text, 4000),
                           auth_url=auth_url, auth_text=trim(auth_text, 4000)),
                       max_tokens=2000, temperature=0.2)
    if raw.startswith("ERR"):
        log(f"  gen error: {raw}"); return None
    obj = parse_json_obj(raw)
    if not obj or "options" not in obj:
        log("  gen parse fail"); return None

    expected = normalize(set(re.findall(r'[A-F]', str(obj.get("correct_answer","")).upper())))
    if not expected:
        log("  no correct answer parsed"); return None

    # 5. Answerability check against authoritative doc alone
    opts = obj["options"]
    lines = "\n".join(f"{k}) {v}" for k, v in sorted(opts.items()))
    check = call_claude(CHECK_MODEL,
                        ANSWERABILITY_PROMPT.format(
                            auth_text=trim(auth_text, 4000),
                            question=obj["question"],
                            option_lines=lines),
                        max_tokens=30, temperature=0.0)
    got = normalize(extract_letters(check))
    if got != expected:
        log(f"  answerability fail: expected={sorted(expected)} got={sorted(got)} raw={check[:80]!r}")
        return None

    record = {
        "occupation": occ,
        "onet_task": task,
        "question": obj["question"],
        "options": opts,
        "correct_answer": ",".join(sorted(expected)),
        "answer_type": obj.get("answer_type", "single"),
        "community_scenario_source": obj.get("community_scenario_source",""),
        "authoritative_anchor_quote": obj.get("authoritative_anchor_quote",""),
        "reasoning": obj.get("reasoning",""),
        "community_url": community_url,
        "authoritative_url": auth_url,
        "source_method": "auto_open_search_hybrid",
        "pilot_batch": "auto_source_prototype",
    }
    log(f"  ✓ generated item: correct={record['correct_answer']} type={record['answer_type']}")
    return record


def main():
    out_path = OUT / "qa_auto_source_pilot.json"
    log_path = OUT / "auto_source.log"
    log_f = open(log_path, "w", buffering=1)
    def log(msg):
        print(msg, flush=True); log_f.write(msg + "\n")

    items = []
    for task_cfg in TASKS:
        rec = process_task(task_cfg, log)
        if rec:
            items.append(rec)
        out_path.write_text(json.dumps(items, indent=2))

    log(f"\nGenerated {len(items)} items; saved to {out_path}")
    log_f.close()


if __name__ == "__main__":
    main()
