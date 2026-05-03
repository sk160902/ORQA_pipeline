"""Step 57: Reddit-only backfill for Approach 2 multi, targeting the 5 missing slots."""
import sys, json, random, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import importlib.util
spec = importlib.util.spec_from_file_location("m56", str(Path(__file__).parent / "56_approach2_multi.py"))
m56 = importlib.util.module_from_spec(spec); spec.loader.exec_module(m56)

OUT = Path(__file__).parent / "output"

# Gaps after step 56: (occupation, number of Reddit multis still needed)
GAPS = {
    "Travel Agents":     2,
    "Credit Counselors": 1,
    "Web Developers":    2,
}

def main():
    random.seed(7)
    bank_path = OUT / "qa_pilot_verbatim.json"
    bank = json.loads(bank_path.read_text())
    used_urls = {q.get("source_url") for q in bank}

    for occ, need in GAPS.items():
        if need == 0: continue
        cfg = m56.PILOT[occ]
        print(f"\n=== {occ} (need {need} Reddit multi) ===", flush=True)
        posts = []
        for sub in cfg["subreddits"]:
            ps = m56.reddit_top(sub, limit=80, min_score=30)
            posts.extend(ps)
            print(f"  r/{sub}: {len(ps)} posts", flush=True)
            time.sleep(0.5)
        random.shuffle(posts)

        rem = need
        for post in posts:
            if rem == 0: break
            thread = m56.reddit_thread(post)
            time.sleep(0.8)
            if not thread or not m56.passes_consensus(thread): continue
            if thread["link"] in used_urls: continue
            item = m56.gen_multi_item(occ, thread)
            if not item: continue
            bank.append(m56.build_record(item, occ, cfg, thread, "reddit"))
            used_urls.add(thread["link"])
            rem -= 1
            print(f"    ✓ Reddit multi  rem={rem}  src={thread['link'][:70]}", flush=True)
            bank_path.write_text(json.dumps(bank, indent=2))

        print(f"  {occ} remaining: {rem}", flush=True)

    from collections import Counter
    c = Counter((q["occupation"], q.get("answer_type")) for q in bank)
    print(f"\nFinal bank: {len(bank)} items")
    for (o, at), n in sorted(c.items()):
        print(f"  {o:22s} {at:7s} {n}")

if __name__ == "__main__":
    main()
