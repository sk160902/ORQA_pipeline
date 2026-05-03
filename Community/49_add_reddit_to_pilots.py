"""
Step 49: Rebalance pilot banks to 2 SE + 2 Reddit per (occupation, approach, type).

Strategy (side-steps SE rate-limit):
  - Keep existing SE items (already in qa_pilot_*.json) but sub-sample:
      per occupation per approach: 2 SE single + 2 SE multi  (approach1 & 3)
                                   2 SE single             (approach2 — single-only)
  - Generate fresh Reddit items to reach the matching 2 Reddit per bucket:
      per occupation per approach: +2 Reddit single + 2 Reddit multi  (approach1 & 3)
                                   +2 Reddit single             (approach2)

Final per approach = 24 items (3 occupations × 8 items for approach1/3; 12 for approach2).
Existing banks are backed up to *.bak_preReddit.json before overwrite.
"""
import os, json, re, time, html, ssl, random
from pathlib import Path
import urllib.request, urllib.error
from openai import OpenAI

ROOT = Path(__file__).parent
OUT = ROOT / "output"

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = ssl.create_default_context()
    SSL_CTX.check_hostname = False; SSL_CTX.verify_mode = ssl.CERT_NONE

USER_AGENT = "Mozilla/5.0 pilot-research-bot/0.1"
openai_client = OpenAI(api_key=(ROOT/"api_key.txt").read_text().strip())
GEN_MODEL = "gpt-4o"

PILOT = {
    "Travel Agents":     {"onet_code": "41-3041.00",  "subreddits": ["travel","solotravel","TravelHacks"]},
    "Credit Counselors": {"onet_code": "21-1012.00",  "subreddits": ["personalfinance","creditcards","debtfree"]},
    "Web Developers":    {"onet_code": "15-1254.00",  "subreddits": ["webdev","learnprogramming","javascript"]},
}

PER_BUCKET = 2
MIN_TOP = 5
MIN_RATIO = 3.0
SJT_WORST_FRAC = 1/3


# ─── HTTP + parsing ────────────────────────────────────────────────────────

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return r.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == 1: raise
            time.sleep(1.5)


def reddit_top(subreddit, t="year", limit=80, min_score=40):
    u = f"https://old.reddit.com/r/{subreddit}/top.json?t={t}&limit={limit}"
    try:
        d = json.loads(fetch(u))
    except Exception as e:
        print(f"  r/{subreddit} fetch fail: {e}", flush=True); return []
    out = []
    for c in d.get("data",{}).get("children",[]):
        x = c.get("data",{})
        if x.get("score",0) < min_score or x.get("num_comments",0) < 5: continue
        out.append({"subreddit": subreddit, "id": x.get("id"),
                    "permalink": x.get("permalink",""),
                    "title": x.get("title",""),
                    "body": x.get("selftext","") or "",
                    "score": x.get("score",0),
                    "link": f"https://www.reddit.com{x.get('permalink','')}"})
    return out


def reddit_thread(post):
    u = f"https://old.reddit.com{post['permalink']}.json?sort=top&limit=30"
    try:
        d = json.loads(fetch(u))
    except Exception: return None
    if not isinstance(d, list) or len(d) < 2: return None
    comments = []
    for c in d[1].get("data",{}).get("children",[]):
        if c.get("kind") != "t1": continue
        cd = c.get("data",{})
        txt = cd.get("body","") or ""
        if len(txt) < 40: continue  # skip short/one-line replies
        comments.append({"score": cd.get("score",0), "text": txt, "is_accepted": False})
    if len(comments) < 4: return None
    return {"platform":"reddit", "site":f"r/{post['subreddit']}", "qid": post["id"],
            "link": post["link"], "title": post["title"], "body": post["body"],
            "answers": comments}


def passes_consensus(thread):
    ans = thread["answers"]
    if len(ans) < 2: return False
    top, nxt = ans[0], ans[1]
    if top["score"] < MIN_TOP: return False
    if top["score"] < MIN_RATIO * max(1, nxt["score"]): return False
    # Reddit has no "accepted" — skip that requirement
    return True


def has_clear_worst(thread):
    ans = thread["answers"]
    if len(ans) < 4: return False
    return ans[-1]["score"] <= SJT_WORST_FRAC * ans[0]["score"]


# ─── LLM ──────────────────────────────────────────────────────────────────

def call_openai(prompt, max_tokens=800, temperature=0.3):
    for attempt in range(3):
        try:
            r = openai_client.chat.completions.create(
                model=GEN_MODEL, temperature=temperature, max_tokens=max_tokens,
                messages=[{"role":"user","content":prompt}])
            return (r.choices[0].message.content or "").strip()
        except Exception as e:
            if attempt == 2: return ""
            time.sleep(1.5 * (attempt+1))


SCENARIO_PROMPT = """You are writing an on-the-job scenario for a {occupation}. The scenario must be grounded in the Reddit question below. Retain every named system, tool, constraint, and specific fact — do NOT flatten to a generic one-liner.

Target length: 80-180 words.

Original question title: {title}
Original question body:
\"\"\"
{body}
\"\"\"

Write only the scenario paragraph. End with a single concrete question for the worker."""

PARAPHRASE_PROMPT = """Paraphrase each of the following Reddit answers into one option (single sentence, 20-35 words). Preserve technical specifics. Output exactly four lines:
A) <paraphrase of ANSWER 1>
B) <paraphrase of ANSWER 2>
C) <paraphrase of ANSWER 3>
D) <paraphrase of ANSWER 4>

ANSWER 1:
{a1}

ANSWER 2:
{a2}

ANSWER 3:
{a3}

ANSWER 4:
{a4}
"""

MULTI_PROMPT = """Given the scenario and the top community answer, decide which of the options A/B/C/D are simultaneously valid per the top answer. Return only the letters (comma-separated).

Scenario:
{scenario}

Top answer:
{top}

Options:
{options_block}
"""


def first_sentences(text, max_chars=220):
    t = re.sub(r"\s+", " ", text).strip()
    if len(t) <= max_chars: return t
    m = re.search(r'[.!?](\s|$)', t[60:max_chars+60])
    return (t[:60 + m.start() + 1] if m else t[:max_chars]).strip().rstrip("…")


def parse_letter_lines(s):
    opts = {}
    for letter in "ABCD":
        m = re.search(rf'^\s*{letter}\)\s*(.+)$', s, re.MULTILINE)
        if m: opts[letter] = m.group(1).strip()
    return opts


def make_scenario(occ, thread):
    s = call_openai(SCENARIO_PROMPT.format(
        occupation=occ, title=thread["title"], body=(thread["body"] or thread["title"])[:2500]),
        max_tokens=350, temperature=0.4)
    return s if s and len(s) > 40 else None


def gen_approach1(occ, thread, want_type):
    ans = thread["answers"]
    if len([a for a in ans if a["text"].strip()]) < 4: return None
    scenario = make_scenario(occ, thread)
    if not scenario: return None
    top = ans[0]
    pool = ans[1:]
    random.shuffle(pool)
    d = pool[:3]
    if len(d) < 3: return None
    opts_raw = call_openai(PARAPHRASE_PROMPT.format(
        a1=top["text"][:1200], a2=d[0]["text"][:1200],
        a3=d[1]["text"][:1200], a4=d[2]["text"][:1200]),
        max_tokens=400, temperature=0.3)
    opts = parse_letter_lines(opts_raw)
    if len(opts) < 4: return None
    opts["E"] = "All of the above"; opts["F"] = "None of the above"
    if want_type == "multi":
        letters_raw = call_openai(MULTI_PROMPT.format(
            scenario=scenario, top=top["text"][:1500],
            options_block="\n".join(f"{k}) {v}" for k,v in sorted(opts.items()))),
            max_tokens=30, temperature=0.1)
        letters = sorted(set(re.findall(r'[A-D]', (letters_raw or "").upper())))
        if len(letters) < 2: return None
        correct = ",".join(letters)
    else:
        correct = "A"
    return {"question": scenario, "options": opts, "correct_answer": correct,
            "answer_type": want_type}


def gen_approach2(occ, thread):
    ans = thread["answers"]
    if len([a for a in ans if a["text"].strip()]) < 4: return None
    scenario = make_scenario(occ, thread)
    if not scenario: return None
    opts = {
        "A": first_sentences(ans[0]["text"], 220),
        "B": first_sentences(ans[1]["text"], 220),
        "C": first_sentences(ans[2]["text"] if len(ans)>2 else ans[-1]["text"], 220),
        "D": first_sentences(ans[-1]["text"], 220),
        "E": "All of the above",
        "F": "None of the above",
    }
    return {"question": scenario, "options": opts, "correct_answer": "A",
            "answer_type": "single"}


def gen_approach3(occ, thread):
    ans = thread["answers"]
    if len(ans) < 4 or not has_clear_worst(thread): return None
    scenario = make_scenario(occ, thread)
    if not scenario: return None
    candidates = [("BEST", ans[0]), ("WORST", ans[-1]),
                  ("F1", ans[len(ans)//3]), ("F2", ans[len(ans)//2])]
    random.shuffle(candidates)
    letters = "ABCD"; opts = {}
    best_letter = worst_letter = None
    for letter, (role, a) in zip(letters, candidates):
        paraphrased = call_openai(
            f"Paraphrase into a 20-35 word option. Preserve technical specifics.\n\nInput:\n{a['text'][:1200]}\n\nOutput only the paraphrase.",
            max_tokens=120, temperature=0.3)
        opts[letter] = (paraphrased or first_sentences(a["text"], 220))[:250]
        if role == "BEST": best_letter = letter
        elif role == "WORST": worst_letter = letter
    opts["E"] = "All of the above"; opts["F"] = "None of the above"
    return {"question": scenario, "options": opts,
            "best_answer": best_letter, "worst_answer": worst_letter,
            "answer_type": "sjt_best_worst"}


# ─── main ─────────────────────────────────────────────────────────────────

def subsample_se(bank_items, occ, answer_type, n, seed):
    rng = random.Random(seed)
    pool = [q for q in bank_items
            if q.get("occupation")==occ and q.get("answer_type")==answer_type
            and q.get("source_platform","").startswith("stackexchange")]
    if len(pool) >= n:
        return rng.sample(pool, n)
    return pool[:]  # take what's available


def subsample_se_sjt(bank_items, occ, n, seed):
    rng = random.Random(seed)
    pool = [q for q in bank_items
            if q.get("occupation")==occ and q.get("answer_type")=="sjt_best_worst"
            and q.get("source_platform","").startswith("stackexchange")]
    if len(pool) >= n:
        return rng.sample(pool, n)
    return pool[:]


def main():
    random.seed(42)
    banks_paths = {
        "approach1": OUT/"qa_pilot_consensus.json",
        "approach2": OUT/"qa_pilot_verbatim.json",
        "approach3": OUT/"qa_pilot_sjt.json",
    }
    old_banks = {}
    for k, p in banks_paths.items():
        # backup
        bak = p.with_suffix(".bak_preReddit.json")
        if p.exists() and not bak.exists():
            bak.write_text(p.read_text())
        old_banks[k] = json.loads(p.read_text()) if p.exists() else []
        # Tag existing items with source_platform if missing
        for q in old_banks[k]:
            if not q.get("source_platform"):
                q["source_platform"] = "stackexchange"

    final = {k: [] for k in banks_paths}

    for occ, cfg in PILOT.items():
        print(f"\n=== {occ} ===", flush=True)

        # 1) keep SE subsamples
        for q in subsample_se(old_banks["approach1"], occ, "single", PER_BUCKET, seed=11):
            q = dict(q); q["_mix_role"] = "SE"; final["approach1"].append(q)
        for q in subsample_se(old_banks["approach1"], occ, "multi", PER_BUCKET, seed=12):
            q = dict(q); q["_mix_role"] = "SE"; final["approach1"].append(q)
        for q in subsample_se(old_banks["approach2"], occ, "single", PER_BUCKET, seed=13):
            q = dict(q); q["_mix_role"] = "SE"; final["approach2"].append(q)
        for q in subsample_se_sjt(old_banks["approach3"], occ, PER_BUCKET, seed=14):
            q = dict(q); q["_mix_role"] = "SE"; final["approach3"].append(q)
        print(f"  kept SE: a1={sum(1 for q in final['approach1'] if q['occupation']==occ)}, "
              f"a2={sum(1 for q in final['approach2'] if q['occupation']==occ)}, "
              f"a3={sum(1 for q in final['approach3'] if q['occupation']==occ)}", flush=True)

        # 2) gather Reddit candidate threads
        posts = []
        for sub in cfg["subreddits"]:
            ps = reddit_top(sub, t="year", limit=80, min_score=40)
            print(f"    r/{sub}: {len(ps)} posts", flush=True)
            posts.extend(ps)
        random.shuffle(posts)

        need = {
            ("approach1","single"): PER_BUCKET,
            ("approach1","multi"):  PER_BUCKET,
            ("approach2","single"): PER_BUCKET,
            ("approach3","sjt"):    PER_BUCKET,
        }

        for post in posts:
            if all(v==0 for v in need.values()): break
            thread = reddit_thread(post)
            time.sleep(0.8)
            if not thread or not passes_consensus(thread): continue

            if need[("approach1","single")] > 0:
                q = gen_approach1(occ, thread, "single")
                if q:
                    q.update({"occupation":occ, "onet_code":cfg["onet_code"],
                              "source_url":thread["link"],
                              "source_platform":"reddit", "source_type":"reddit",
                              "method":"approach1_paraphrased",
                              "_mix_role":"Reddit",
                              "consensus_meta":{"top_answer_score":thread["answers"][0]["score"],
                                                "next_answer_score":thread["answers"][1]["score"],
                                                "top_next_ratio":thread["answers"][0]["score"]/max(1,thread["answers"][1]["score"])},
                              "human_verified":False, "pilot_batch":"v2_reddit"})
                    final["approach1"].append(q); need[("approach1","single")] -= 1
                    print(f"    ✓ A1 single Reddit rem={need[('approach1','single')]}", flush=True)

            if need[("approach1","multi")] > 0:
                q = gen_approach1(occ, thread, "multi")
                if q and q.get("answer_type")=="multi":
                    q.update({"occupation":occ, "onet_code":cfg["onet_code"],
                              "source_url":thread["link"],
                              "source_platform":"reddit", "source_type":"reddit",
                              "method":"approach1_paraphrased",
                              "_mix_role":"Reddit",
                              "consensus_meta":{"top_answer_score":thread["answers"][0]["score"],
                                                "next_answer_score":thread["answers"][1]["score"],
                                                "top_next_ratio":thread["answers"][0]["score"]/max(1,thread["answers"][1]["score"])},
                              "human_verified":False, "pilot_batch":"v2_reddit"})
                    final["approach1"].append(q); need[("approach1","multi")] -= 1
                    print(f"    ✓ A1 multi  Reddit rem={need[('approach1','multi')]}", flush=True)

            if need[("approach2","single")] > 0:
                q = gen_approach2(occ, thread)
                if q:
                    q.update({"occupation":occ, "onet_code":cfg["onet_code"],
                              "source_url":thread["link"],
                              "source_platform":"reddit", "source_type":"reddit",
                              "method":"approach2_verbatim",
                              "_mix_role":"Reddit",
                              "consensus_meta":{"top_answer_score":thread["answers"][0]["score"],
                                                "next_answer_score":thread["answers"][1]["score"],
                                                "top_next_ratio":thread["answers"][0]["score"]/max(1,thread["answers"][1]["score"])},
                              "human_verified":False, "pilot_batch":"v2_reddit"})
                    final["approach2"].append(q); need[("approach2","single")] -= 1
                    print(f"    ✓ A2 verbatim Reddit rem={need[('approach2','single')]}", flush=True)

            if need[("approach3","sjt")] > 0 and has_clear_worst(thread):
                q = gen_approach3(occ, thread)
                if q:
                    q.update({"occupation":occ, "onet_code":cfg["onet_code"],
                              "source_url":thread["link"],
                              "source_platform":"reddit", "source_type":"reddit",
                              "method":"approach3_sjt",
                              "_mix_role":"Reddit",
                              "consensus_meta":{"top_answer_score":thread["answers"][0]["score"],
                                                "next_answer_score":thread["answers"][1]["score"],
                                                "top_next_ratio":thread["answers"][0]["score"]/max(1,thread["answers"][1]["score"]),
                                                "worst_score":thread["answers"][-1]["score"]},
                              "human_verified":False, "pilot_batch":"v2_reddit"})
                    final["approach3"].append(q); need[("approach3","sjt")] -= 1
                    print(f"    ✓ A3 SJT Reddit rem={need[('approach3','sjt')]}", flush=True)

            for k, p in banks_paths.items():
                p.write_text(json.dumps(final[k], indent=2))

        print(f"  remaining buckets: {[k for k,v in need.items() if v>0]}", flush=True)

    # Final save
    for k, p in banks_paths.items():
        p.write_text(json.dumps(final[k], indent=2))
        print(f"Saved {p.name}: {len(final[k])} items", flush=True)

    from collections import Counter
    for k in banks_paths:
        c = Counter()
        for q in final[k]:
            c[(q["occupation"], q.get("_mix_role","?"), q["answer_type"])] += 1
        print(f"\n{k}:")
        for (occ, role, at), n in sorted(c.items()):
            print(f"  {occ:25s} {role:7s} {at:18s} {n}")


if __name__ == "__main__":
    main()
