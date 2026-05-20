"""Orchestrator that scales the benchmark to a higher per-occupation target.

Reads the current question bank, computes per-occupation gaps relative to the
requested target, splits the work across N community workers and M
authoritative-document workers, and writes a launchable shell script. The
community pass runs first; the authoritative pass fills any remaining gap.

Usage
-----
    # dry run, prints what it would do
    python launch_scale_up.py --target 18 --community-workers 8 --authdoc-workers 4 --dry-run

    # 50-occupation pilot
    python launch_scale_up.py --target 18 --pilot 50 --community-workers 4 --authdoc-workers 2

    # full overnight run
    python launch_scale_up.py --target 18 --community-workers 8 --authdoc-workers 4

After running this script, kick off the generated shell script:

    bash output/launch_scale_up.sh

Monitor with the printed tail commands. When all workers exit, run the merge
script (which this orchestrator also writes) to produce the new combined bank.
"""
import argparse
import csv
import json
import os
import shutil
import textwrap
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
PY = "/usr/local/bin/python3"

CURRENT_BANK_CSV = OUT / "Final_csv" / "question_model_responses.csv"
CURRENT_BANK_JSON = OUT / "qa_auto_source_v4_expanded_final.json"  # source-of-truth bank items

OCCS_CSV_DEFAULT = OUT / "scale_up_1016_occupations.csv"  # has occupation_title + soc_code
RUN_DIR_NAME = "scaleup"


def load_current_counts() -> Counter:
    if not CURRENT_BANK_CSV.exists():
        raise FileNotFoundError(f"current bank not found at {CURRENT_BANK_CSV}")
    with CURRENT_BANK_CSV.open() as f:
        rows = list(csv.DictReader(f))
    return Counter(r["occupation"] for r in rows)


def load_occupation_table() -> list:
    """Return list of (occupation_title, soc_code) tuples from the canonical CSV."""
    if not OCCS_CSV_DEFAULT.exists():
        raise FileNotFoundError(f"occupation list not found at {OCCS_CSV_DEFAULT}")
    out = []
    with OCCS_CSV_DEFAULT.open() as f:
        for r in csv.DictReader(f):
            out.append((r["occupation_title"], r.get("soc_code", "")))
    return out


def split_into_chunks(items, n_chunks):
    """Round-robin split so chunks are balanced even if items are pre-sorted."""
    chunks = [[] for _ in range(n_chunks)]
    for i, x in enumerate(items):
        chunks[i % n_chunks].append(x)
    return chunks


def write_chunk_csv(path: Path, chunk):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["occupation_title", "soc_code"])
        for occ, soc in chunk:
            w.writerow([occ, soc])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", type=int, default=15,
                   help="target items per occupation (default 15, hard cap 20)")
    p.add_argument("--cap", type=int, default=20,
                   help="hard cap on items per occupation after merge (default 20)")
    p.add_argument("--community-workers", type=int, default=8,
                   help="number of community-pass workers (default 8)")
    p.add_argument("--authdoc-workers", type=int, default=4,
                   help="number of authoritative-document workers (default 4)")
    p.add_argument("--max-failures-community", type=int, default=14,
                   help="bumped MAX_FAILURES for community pass (default 14)")
    p.add_argument("--max-failures-authdoc", type=int, default=10,
                   help="bumped MAX_FAILURES for authdoc pass (default 10)")
    p.add_argument("--pilot", type=int, default=None,
                   help="if set, run on only this many occupations (lowest item-count first)")
    p.add_argument("--stagger-sec", type=int, default=20,
                   help="seconds to wait between worker launches to avoid rate-limit collisions")
    p.add_argument("--dry-run", action="store_true",
                   help="print run details but do not write any chunk CSVs or launcher")
    args = p.parse_args()

    run_dir = OUT / RUN_DIR_NAME
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. inventory
    counts = load_current_counts()
    occupations = load_occupation_table()
    occ_set_in_csv = {occ for occ, _ in occupations}
    # Anything in the bank but not the occupation table still needs filling.
    extra = [occ for occ in counts if occ not in occ_set_in_csv]
    for e in extra:
        occupations.append((e, ""))

    # Compute gap per occupation
    gaps = []
    for occ, soc in occupations:
        have = counts.get(occ, 0)
        need = max(0, args.target - have)
        if need > 0:
            gaps.append((occ, soc, have, need))
    gaps.sort(key=lambda r: r[2])  # lowest current count first

    if args.pilot:
        gaps = gaps[: args.pilot]

    print(f"Current bank: {sum(counts.values())} items across {len(counts)} occupations")
    print(f"Target per occupation: {args.target}")
    print(f"Occupations needing more items: {len(gaps)} of {len(occupations)}")
    print(f"Total items to generate (upper bound): "
          f"{sum(g[3] for g in gaps):,}")

    if not gaps:
        print("Nothing to do.")
        return

    # 2. split into chunks for community workers (each chunk = list of occupations to work)
    occ_only = [(occ, soc) for occ, soc, _, _ in gaps]
    community_chunks = split_into_chunks(occ_only, args.community_workers)
    authdoc_chunks = split_into_chunks(occ_only, args.authdoc_workers)

    print(f"\nCommunity workers: {args.community_workers}, "
          f"avg {len(occ_only)//args.community_workers} occs each")
    print(f"Authdoc workers:   {args.authdoc_workers}, "
          f"avg {len(occ_only)//args.authdoc_workers} occs each")

    if args.dry_run:
        print("\n[dry run] not writing chunk CSVs or launcher.")
        return

    # 3. write chunk CSVs
    for i, chunk in enumerate(community_chunks):
        write_chunk_csv(run_dir / f"community_chunk_{i}.csv", chunk)
    for i, chunk in enumerate(authdoc_chunks):
        write_chunk_csv(run_dir / f"authdoc_chunk_{i}.csv", chunk)

    # 4. write launcher shell script
    lines = [
        "#!/bin/bash",
        "# Auto-generated by launch_scale_up.py",
        f'cd "$(dirname "$0")/.."',
        f'PY={PY}',
        f'export TARGET_ITEMS={args.target}',
        f'export MAX_FAILURES={args.max_failures_community}',
        "",
        f"echo 'Launching {args.community_workers} community workers + "
        f"{args.authdoc_workers} authdoc workers'",
        "echo 'TARGET_ITEMS='$TARGET_ITEMS",
        "",
    ]

    for i in range(args.community_workers):
        if i > 0:
            lines.append(f"sleep {args.stagger_sec}")
        lines.append(textwrap.dedent(f"""
            nohup $PY -u 66_auto_source_v4_occupation.py \\
                --from-csv output/{RUN_DIR_NAME}/community_chunk_{i}.csv \\
                --target {args.target} \\
                --ckpt-path output/{RUN_DIR_NAME}/community_ckpt_{i}.json \\
                --bank-path output/{RUN_DIR_NAME}/community_bank_{i}.json \\
                --coverage-path output/{RUN_DIR_NAME}/community_coverage_{i}.csv \\
                --log-path output/{RUN_DIR_NAME}/community_worker_{i}.log \\
                > output/{RUN_DIR_NAME}/community_worker_{i}.stdout 2>&1 &
            echo "  community worker {i} PID: $!"
        """).strip())

    # Authdoc pass uses its own MAX_FAILURES default; export overridden value here
    lines.append("")
    lines.append(f"export MAX_FAILURES={args.max_failures_authdoc}")
    for i in range(args.authdoc_workers):
        if i > 0:
            lines.append(f"sleep {args.stagger_sec}")
        lines.append(textwrap.dedent(f"""
            nohup $PY -u 94_authdoc_extended.py \\
                --from-csv output/{RUN_DIR_NAME}/authdoc_chunk_{i}.csv \\
                --target {args.target} \\
                --ckpt-path output/{RUN_DIR_NAME}/authdoc_ckpt_{i}.json \\
                --bank-path output/{RUN_DIR_NAME}/authdoc_bank_{i}.json \\
                --log-path output/{RUN_DIR_NAME}/authdoc_worker_{i}.log \\
                > output/{RUN_DIR_NAME}/authdoc_worker_{i}.stdout 2>&1 &
            echo "  authdoc worker {i} PID: $!"
        """).strip())

    lines.append("")
    lines.append("echo")
    lines.append("echo 'All workers launched. Monitor with:'")
    lines.append(f"echo '  tail -f output/{RUN_DIR_NAME}/community_worker_*.log'")
    lines.append(f"echo '  tail -f output/{RUN_DIR_NAME}/authdoc_worker_*.log'")
    lines.append("echo 'When all workers have exited (jobs -l shows none), run:'")
    lines.append(f"echo '  python merge_scale_up.py --target {args.target}'")

    launcher = run_dir / "launch_scale_up.sh"
    launcher.write_text("\n".join(lines) + "\n")
    launcher.chmod(0o755)

    # 5. write the merge script (append-only with dedup against existing bank)
    merge = run_dir / "merge_scale_up.py"
    merge.write_text(textwrap.dedent(f"""
        '''Append-only merge of newly generated items into the existing bank.

        Behavior
        --------
        The existing 1,444 rows in Final_csv/question_model_responses.csv are
        treated as locked. New items from community and authdoc workers are
        appended, with two layers of dedup applied per occupation so that no
        existing question is replaced and no near-duplicate scenario is added.

        Dedup layers, applied in order:
          1. Exact match on source_url, then on question text.
          2. Fuzzy match: token Jaccard similarity >= 0.6 against any existing
             question in the same occupation.

        Priority for filling each occupation up to the cap:
          existing (locked)  ->  community_new  ->  authdoc_new
        '''
        import csv, json, re
        from collections import Counter, defaultdict
        from pathlib import Path

        ROOT = Path(__file__).resolve().parents[2]
        OUT = ROOT / "output"
        RUN = OUT / "{RUN_DIR_NAME}"
        TARGET = {args.target}
        CAP = {args.cap}

        # ---- 1. existing bank (LOCKED) -----------------------------------
        bank_csv = OUT / "Final_csv" / "question_model_responses.csv"
        existing_rows = defaultdict(list)
        with bank_csv.open() as f:
            for r in csv.DictReader(f):
                existing_rows[r["occupation"]].append(r)
        n_existing = sum(len(v) for v in existing_rows.values())
        print(f"existing rows (locked): {{n_existing}}")

        # ---- 2. collect candidates from worker checkpoints ----------------
        def load_ckpt(prefix):
            out = defaultdict(list)
            for p in RUN.glob(f"{{prefix}}_ckpt_*.json"):
                d = json.loads(p.read_text())
                for occ, items in d.items():
                    out[occ].extend(items)
            return out
        community_new = load_ckpt("community")
        authdoc_new   = load_ckpt("authdoc")
        print(f"community candidates: {{sum(len(v) for v in community_new.values())}}")
        print(f"authdoc candidates:   {{sum(len(v) for v in authdoc_new.values())}}")

        # ---- 3. dedup + lint helpers --------------------------------------
        WORD = re.compile(r"[a-z0-9]+")
        def tokens(s): return set(WORD.findall((s or '').lower()))
        def jaccard(a, b):
            if not a or not b: return 0.0
            return len(a & b) / len(a | b)

        # Lint: reject items whose options break the no-pronoun / no-question
        # rule, or whose question scenario is too short to match the #3/#4
        # quality bar. These are belt-and-suspenders checks layered on top of
        # the generator prompt.
        FORBIDDEN_OPTION = re.compile(
            r"\\b(?:i|i'm|i've|we|we're|our|ours|us)\\b",
            re.IGNORECASE,
        )
        def lint_item(it):
            opts = it.get("options") or {{}}
            for letter in "ABCD":
                opt = (opts.get(letter) or "").strip()
                if not opt:
                    return f"option_{{letter}}_missing"
                if opt.endswith("?"):
                    return f"option_{{letter}}_trailing_question_mark"
                if FORBIDDEN_OPTION.search(opt):
                    return f"option_{{letter}}_forbidden_pronoun"
            q = (it.get("question") or "").strip()
            wc = len(q.split())
            if wc < 80:
                return f"question_too_short_{{wc}}_words"
            if it.get("correct_answer") not in {{"A", "B", "C", "D"}}:
                return "correct_answer_invalid"
            return None  # passes

        seen_urls = defaultdict(set)
        seen_qs   = defaultdict(list)  # list of token-sets for fuzzy compare

        # Seed dedup tables from existing rows
        for occ, rows in existing_rows.items():
            for r in rows:
                u = (r.get("source_url") or "").strip()
                q = r.get("question") or ""
                if u: seen_urls[occ].add(u)
                seen_qs[occ].append(tokens(q))

        def is_duplicate(occ, item):
            u = (item.get("source_url") or "").strip()
            if u and u in seen_urls[occ]:
                return True
            qt = tokens(item.get("question") or "")
            if not qt: return False
            for prev in seen_qs[occ]:
                if jaccard(qt, prev) >= 0.6:
                    return True
            return False

        def register(occ, item):
            u = (item.get("source_url") or "").strip()
            if u: seen_urls[occ].add(u)
            seen_qs[occ].append(tokens(item.get("question") or ""))

        # ---- 4. fill each occupation up to TARGET, never above CAP -------
        new_rows = []
        n_dropped_dup = 0
        n_dropped_lint = Counter()

        for source_label, src in [("community", community_new), ("authdoc", authdoc_new)]:
            for occ, items in src.items():
                for it in items:
                    have = len(existing_rows[occ]) + sum(1 for r in new_rows if r["occupation"] == occ)
                    if have >= TARGET: break
                    if have >= CAP: break
                    if is_duplicate(occ, it):
                        n_dropped_dup += 1
                        continue
                    reason = lint_item(it)
                    if reason is not None:
                        n_dropped_lint[reason] += 1
                        continue
                    register(occ, it)
                    flat = dict(it)
                    flat["occupation"] = occ
                    flat["_source_label"] = source_label
                    new_rows.append(flat)

        print(f"dedup dropped: {{n_dropped_dup}}")
        print(f"lint dropped:  {{sum(n_dropped_lint.values())}}")
        for reason, n in n_dropped_lint.most_common():
            print(f"  {{n:>5}}  {{reason}}")
        print(f"new rows kept: {{len(new_rows)}}")

        # ---- 5. write outputs --------------------------------------------
        out_json = RUN / "new_items.json"
        out_json.write_text(json.dumps(new_rows, indent=2))

        # Also emit a CSV in the same column order as Final_csv but WITHOUT
        # any model-answer columns. This is the file that becomes the input
        # to the evaluation step.
        CSV_COLS = [
            "source_url", "occupation", "question",
            "option_A", "option_B", "option_C",
            "option_D", "option_E", "option_F",
            "correct_answer",
        ]
        out_csv = RUN / "new_items.csv"
        with out_csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS)
            w.writeheader()
            for it in new_rows:
                opts = it.get("options") or {{}}
                # Worker JSON stores options as dict {{A,B,C,D}}; force E/F to
                # the standard catch-alls used elsewhere in the bank.
                row = {{
                    "source_url":     (it.get("source_url") or "").strip(),
                    "occupation":     it.get("occupation", ""),
                    "question":       it.get("question", ""),
                    "option_A":       opts.get("A", "").strip(),
                    "option_B":       opts.get("B", "").strip(),
                    "option_C":       opts.get("C", "").strip(),
                    "option_D":       opts.get("D", "").strip(),
                    "option_E":       "All of the above",
                    "option_F":       "None of the above",
                    "correct_answer": (it.get("correct_answer") or "").strip(),
                }}
                w.writerow(row)
        print(f"wrote {{out_csv}} (same columns as Final_csv, minus eval cols)")

        # Final per-occupation distribution after merge
        all_counts = Counter()
        for occ in set(list(existing_rows.keys()) + list({{r['occupation'] for r in new_rows}})):
            all_counts[occ] = len(existing_rows[occ]) + sum(1 for r in new_rows if r["occupation"] == occ)
        hist = Counter(all_counts.values())
        print()
        print("post-merge items-per-occupation distribution:")
        for k in sorted(hist):
            print(f"  {{k:3d}} items: {{hist[k]}} occupations")
        print()
        print(f"wrote {{out_json}}  (only NEW items; existing CSV is untouched)")
        print()
        print("Next: run the eval workers on the new items only.")
        print(f"  python 103_launch_eval_workers.py --bank {{out_json}}")
        print("Then append eval rows to Final_csv/question_model_responses.csv.")
    """).strip() + "\n")
    print(f"\nWrote {launcher}")
    print(f"Wrote {merge}")
    print()
    print("Run order:")
    print(f"  bash {launcher}")
    print(f"  # wait for all workers to finish (tail the logs in {run_dir})")
    print(f"  python {merge}")
    print(f"  # merge writes:")
    print(f"  #   {run_dir / 'new_items.json'}")
    print(f"  #   {run_dir / 'new_items.csv'}   (same columns as Final_csv, minus eval cols)")
    print(f"  python 103_launch_eval_workers.py --bank {run_dir / 'new_items.json'}")
    print()

    # Also write a list of source URLs that already exist in the bank, so the
    # community pass can skip re-fetching them. (Read by 66_ via env or arg if
    # supported; otherwise the merge step will dedupe.)
    bank_urls = set()
    with CURRENT_BANK_CSV.open() as f:
        for r in csv.DictReader(f):
            u = (r.get("source_url") or "").strip()
            if u: bank_urls.add(u)
    (run_dir / "existing_source_urls.txt").write_text(
        "\n".join(sorted(bank_urls))
    )
    print(f"\nWrote {len(bank_urls)} existing source URLs to "
          f"{run_dir/'existing_source_urls.txt'} for dedup at merge time.")

    if args.target >= 12:
        est_items = args.target * len(gaps)
        eval_calls = est_items * 3 * 15
        print(f"\nHeads up: target={args.target} implies up to {est_items:,} new items "
              f"and ~{eval_calls:,} evaluation API calls. Confirm OpenAI/Anthropic/Google "
              f"quotas before launching the full run.")


if __name__ == "__main__":
    main()
