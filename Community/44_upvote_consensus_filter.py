"""
Step 44: Consensus filter for the human-verified bank.

For each question sourced from Reddit or Stack Exchange, fetch the thread and
record:
  - top_answer_score    (highest-voted answer)
  - next_answer_score   (second-highest)
  - top_next_ratio      (top / max(1, next))
  - has_accepted        (SE only)
  - n_answers

Then classify each question:
  consensus = True if
      (Reddit)        top_answer_score >= MIN_TOP_ABS AND top_answer_score >= RATIO_MIN * max(1, next_answer_score)
      (StackExchange) has_accepted AND top_answer_score >= MIN_TOP_ABS AND top/next >= RATIO_MIN

Non-Reddit/SE questions (government/textbook/academic) are passed through as
"consensus=unknown" — they can be kept or dropped separately. For Abhishek's
filter we'd typically keep them (they have institutional authority in lieu of
crowd consensus).

Outputs:
  output/consensus_metadata.json    — per-question enrichment
  output/qa_consensus_filtered.json — questions that pass the filter
"""
import json, re, time
from pathlib import Path
from urllib.parse import urlparse
import urllib.request, urllib.error

ROOT = Path(__file__).parent
OUT = ROOT / "output"
BANK = OUT / "qa_40occ_human_verified.json"
META = OUT / "consensus_metadata.json"
FILTERED = OUT / "qa_consensus_filtered.json"

# Thresholds (tunable)
MIN_TOP_ABS = 5        # top answer must have at least this many upvotes
RATIO_MIN   = 3.0      # top must be >= 3x the next answer
ACCEPT_REQUIRED_SE = False  # if True, require SE has an accepted answer

USER_AGENT = "Mozilla/5.0 (compatible; occ-research-bot/0.1)"


def fetch_url(url, retries=2):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                "Accept": "application/json"})
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(1.5)
    raise last


# ─── Reddit ───────────────────────────────────────────────────────────────

REDDIT_PATH_RE = re.compile(r'/r/[^/]+/comments/([a-z0-9]+)', re.I)

def reddit_scores(url):
    """Fetch top-level comment scores for a Reddit submission URL."""
    # Normalise to .json endpoint (old.reddit is slightly more predictable)
    u = url.split("?")[0].rstrip("/")
    if not u.endswith(".json"):
        u = u + ".json"
    # old.reddit works more reliably for non-auth requests
    u = u.replace("www.reddit.com", "old.reddit.com").replace("//reddit.com", "//old.reddit.com")
    try:
        data = json.loads(fetch_url(u))
    except Exception as e:
        return {"error": f"reddit_fetch:{type(e).__name__}"}
    if not isinstance(data, list) or len(data) < 2:
        return {"error": "reddit_shape"}
    comments_listing = data[1].get("data", {}).get("children", [])
    scores = []
    for c in comments_listing:
        if c.get("kind") != "t1":
            continue
        s = c.get("data", {}).get("score", 0)
        scores.append(int(s))
    scores.sort(reverse=True)
    if not scores:
        return {"error": "no_comments", "n_answers": 0}
    top = scores[0]
    nxt = scores[1] if len(scores) > 1 else 0
    return {
        "top_answer_score": top,
        "next_answer_score": nxt,
        "top_next_ratio": top / max(1, nxt),
        "n_answers": len(scores),
        "has_accepted": None,
        "source_platform": "reddit",
    }


# ─── Stack Exchange ──────────────────────────────────────────────────────

SE_HOST_TO_SITE = {
    "money.stackexchange.com": "money",
    "workplace.stackexchange.com": "workplace",
    "travel.stackexchange.com": "travel",
    "writing.stackexchange.com": "writing",
    "academia.stackexchange.com": "academia",
    "stats.stackexchange.com": "stats",
    "stackoverflow.com": "stackoverflow",
    "serverfault.com": "serverfault",
    "math.stackexchange.com": "math",
    "medicalsciences.stackexchange.com": "medicalsciences",
    # catch-alls added on the fly
}

SE_Q_RE = re.compile(r'/questions/(\d+)')

def se_scores(url):
    host = urlparse(url).netloc.lower()
    site = SE_HOST_TO_SITE.get(host)
    if site is None and host.endswith(".stackexchange.com"):
        site = host.split(".")[0]
    if site is None:
        return {"error": f"unknown_se_host:{host}"}
    m = SE_Q_RE.search(url)
    if not m:
        return {"error": "no_se_qid"}
    qid = m.group(1)
    api = (f"https://api.stackexchange.com/2.3/questions/{qid}/answers"
           f"?site={site}&order=desc&sort=votes&pagesize=20")
    try:
        data = json.loads(fetch_url(api))
    except Exception as e:
        return {"error": f"se_fetch:{type(e).__name__}"}
    items = data.get("items", [])
    if not items:
        return {"error": "no_answers", "n_answers": 0}
    scores = [it.get("score", 0) for it in items]
    scores.sort(reverse=True)
    top = scores[0]
    nxt = scores[1] if len(scores) > 1 else 0
    accepted = any(it.get("is_accepted") for it in items)
    return {
        "top_answer_score": top,
        "next_answer_score": nxt,
        "top_next_ratio": top / max(1, nxt),
        "n_answers": len(items),
        "has_accepted": accepted,
        "source_platform": "stackexchange",
    }


# ─── Classification ───────────────────────────────────────────────────────

def classify(meta):
    if not meta or "error" in meta:
        return "unknown"
    top = meta.get("top_answer_score", 0) or 0
    ratio = meta.get("top_next_ratio", 0) or 0
    plat = meta.get("source_platform")
    if plat == "stackexchange" and ACCEPT_REQUIRED_SE and not meta.get("has_accepted"):
        return "no_consensus"
    if top < MIN_TOP_ABS:
        return "no_consensus"
    if ratio < RATIO_MIN:
        return "no_consensus"
    return "consensus"


def platform(url):
    h = urlparse(url).netloc.lower()
    if "reddit.com" in h:
        return "reddit"
    if h.endswith(".stackexchange.com") or h in ("stackoverflow.com", "serverfault.com"):
        return "stackexchange"
    return "other"


def main():
    bank = json.loads(BANK.read_text())
    existing = {}
    if META.exists():
        existing = json.loads(META.read_text())

    results = {}
    counts = {"reddit": 0, "stackexchange": 0, "other": 0}

    for i, q in enumerate(bank):
        url = q.get("source_url", "")
        plat = platform(url)
        counts[plat] += 1
        cache_key = url
        if cache_key in existing:
            meta = existing[cache_key]
        else:
            if plat == "reddit":
                meta = reddit_scores(url)
                time.sleep(1.0)      # Reddit rate limit courtesy
            elif plat == "stackexchange":
                meta = se_scores(url)
                time.sleep(0.3)
            else:
                meta = {"source_platform": "other"}
            existing[cache_key] = meta
            META.write_text(json.dumps(existing, indent=2))
            if plat != "other":
                print(f"  [{i+1}/{len(bank)}] {plat} -> {meta}", flush=True)
        results[cache_key] = meta

    META.write_text(json.dumps(existing, indent=2))
    print(f"\nFetched metadata for {len(existing)} unique URLs.")

    # Classify and filter
    kept = []
    dropped_by_reason = {"no_consensus": [], "unknown": [], "error": []}
    for q in bank:
        url = q.get("source_url", "")
        meta = results.get(url, {})
        cls = classify(meta) if platform(url) != "other" else "other"
        q_out = dict(q)
        q_out["consensus_meta"] = meta
        q_out["consensus_class"] = cls
        if cls in ("consensus", "other"):
            kept.append(q_out)
        else:
            dropped_by_reason.setdefault(cls, []).append(q_out)

    FILTERED.write_text(json.dumps(kept, indent=2))

    # Summaries
    from collections import Counter
    print("\nConsensus classes:")
    all_cls = Counter()
    per_plat = {}
    for q in bank:
        url = q.get("source_url", "")
        meta = results.get(url, {})
        p = platform(url)
        cls = classify(meta) if p != "other" else "other"
        all_cls[cls] += 1
        per_plat.setdefault(p, Counter())[cls] += 1
    for cls, n in all_cls.most_common():
        print(f"  {cls:15s} {n}")
    print("\nPer-platform breakdown:")
    for p, c in per_plat.items():
        print(f"  {p:15s} {dict(c)}")

    # Per-occupation retention
    from collections import defaultdict
    per_occ = defaultdict(lambda: [0, 0])  # kept, total
    for q in bank:
        per_occ[q["occupation"]][1] += 1
    for q in kept:
        per_occ[q["occupation"]][0] += 1
    print(f"\nKept {len(kept)} / {len(bank)} questions")
    print("\nPer-occupation kept/total (drops > 0 only):")
    for occ, (k, t) in sorted(per_occ.items(), key=lambda x: x[1][0]/max(1,x[1][1])):
        if k < t:
            print(f"  {occ:65s} {k:3d}/{t:<3d}")


if __name__ == "__main__":
    main()
