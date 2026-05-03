"""Main pipeline runner.

For each occupation:
  1. Discover URLs via Serper (site:-restricted to per-occupation whitelist)
  2. Round 1: general discovery
  3. For each URL:
       - HEAD-check, fetch, clean, truncate
       - Extract evidence cards
       - For each card: build MCQ → verify → keep if quality_tier in {A, B}
  4. If under TARGET_ITEMS, run topical rounds with different aspects.
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path

from . import config
from . import schemas
from . import search_client
from . import source_fetcher
from . import evidence_cards
from . import item_generator
from . import item_verifier
from . import difficulty_pretest
from . import cognitive_type
from . import academic_sources

_WORD = re.compile(r"[a-z0-9]+", re.I)


def _qtokens(s: str) -> set:
    return set(_WORD.findall((s or "").lower()))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Source-mix enforcement (per Abhishek's "OSHA/CDC only where canonical" + per-SOC cap).
# Cap any single source domain to MAX_DOMAIN_SHARE_PER_SOC of a SOC's items
# (after a small grace window so we don't reject the first few items needed to compute share).
MAX_DOMAIN_SHARE_PER_SOC = 0.30  # no single domain >30% of any SOC's items
GRACE_WINDOW_ITEMS = 4  # don't enforce until SOC has at least this many items

# CDC/BLS/OSHA canonical mapping by SOC major group.
# - canonical: source is the recognized authority for this SOC group → no special cap
# - capped: source can appear but at low share (forces other authorities to fill)
# - blocked: source is not canonical for this SOC group → reject items from it
CDC_CANONICAL_GROUPS = frozenset({"29", "31", "21"})                       # healthcare practitioners + support + social service
CDC_CAPPED_GROUPS = frozenset({"17", "19", "33", "37", "39", "45", "47", "49", "51"})  # engineering + sciences + protective + cleaning/personal/farming + construction/maintenance/production (NIOSH safety)
# All other SOC groups → CDC blocked (computing 15, finance 13, legal 23, education 25, arts 27, sales 41, admin 43, transport 53, mgmt 11, food 35)
CDC_CAP_FOR_CAPPED_SOCS = 0.15

OSHA_CANONICAL_GROUPS = frozenset({"47", "49", "51", "53"})    # construction, maintenance, production, transport (workplace safety)
OSHA_CAPPED_GROUPS = frozenset({"17", "29", "31", "33", "37", "45"})
OSHA_CAP_FOR_CAPPED_SOCS = 0.15

BLS_CAP_FOR_ALL_SOCS = 0.10  # BLS is just labor stats, never the canonical authority for practical knowledge


def _is_cdc_domain(domain: str) -> bool:
    d = (domain or "").lower()
    return d == "cdc.gov" or d.endswith(".cdc.gov")


def _is_osha_domain(domain: str) -> bool:
    d = (domain or "").lower()
    return d == "osha.gov" or d.endswith(".osha.gov")


def _is_bls_domain(domain: str) -> bool:
    d = (domain or "").lower()
    return d == "bls.gov" or d.endswith(".bls.gov")


def _domain_share(items_list, domain: str) -> tuple[int, int, float]:
    """Returns (n_from_domain, total, share)."""
    if not items_list: return 0, 0, 0.0
    n = sum(1 for it in items_list if (it.source_domain or "").lower() == (domain or "").lower())
    return n, len(items_list), n / len(items_list)


def check_source_mix_acceptance(item, items_list, soc: str) -> tuple[bool, str]:
    """Returns (accept, reason).
    Enforces per-SOC source caps + CDC/OSHA/BLS canonical limits.
    Returns (False, reason) to reject the item (don't add to bank).
    """
    grp = soc[:2]
    domain = (item.source_domain or "").lower()
    n_dom, total, _ = _domain_share(items_list, domain)
    new_share = (n_dom + 1) / (total + 1)

    # CDC handling
    if _is_cdc_domain(domain):
        if grp not in CDC_CANONICAL_GROUPS:
            if grp in CDC_CAPPED_GROUPS:
                # capped: enforce stricter share limit even after grace
                if total >= GRACE_WINDOW_ITEMS and new_share > CDC_CAP_FOR_CAPPED_SOCS:
                    return False, f"CDC capped at {CDC_CAP_FOR_CAPPED_SOCS*100:.0f}% for SOC {grp}; would be {new_share*100:.0f}%"
            else:
                # blocked: CDC not canonical for this SOC group
                return False, f"CDC blocked for SOC {grp} (not canonical authority)"

    # OSHA handling
    if _is_osha_domain(domain):
        if grp not in OSHA_CANONICAL_GROUPS:
            if grp in OSHA_CAPPED_GROUPS:
                if total >= GRACE_WINDOW_ITEMS and new_share > OSHA_CAP_FOR_CAPPED_SOCS:
                    return False, f"OSHA capped at {OSHA_CAP_FOR_CAPPED_SOCS*100:.0f}% for SOC {grp}; would be {new_share*100:.0f}%"
            else:
                return False, f"OSHA blocked for SOC {grp} (not canonical authority)"

    # BLS handling — never canonical for practical knowledge, capped everywhere
    if _is_bls_domain(domain):
        if total >= GRACE_WINDOW_ITEMS and new_share > BLS_CAP_FOR_ALL_SOCS:
            return False, f"BLS capped at {BLS_CAP_FOR_ALL_SOCS*100:.0f}%; would be {new_share*100:.0f}%"

    # General per-SOC source cap (any domain, including authoritative societies)
    if total >= GRACE_WINDOW_ITEMS and new_share > MAX_DOMAIN_SHARE_PER_SOC:
        return False, f"source-cap: {domain} would be {new_share*100:.0f}% > {MAX_DOMAIN_SHARE_PER_SOC*100:.0f}%"

    return True, ""


def process_occupation(occupation: str, soc: str, *, target_items: int,
                       log, replacement_for: str | None = None) -> dict:
    """Returns {"items": [...], "cards": [...], "stats": {...}}.

    `replacement_for`: SOC of the under-yield occupation this is backfilling,
    if applicable. Stamped onto each item.

    Two separate failure budgets:
      MAX_FAILURES_PER_OCCUPATION       — quality/extract budget
      MAX_FETCH_FAILURES_PER_OCCUPATION — infrastructure/HTTP budget
    Mixing them was the bug that killed otherwise-strong occupations whose
    sources happened to be Cloudflare-blocked.
    """
    log(f"\n=== {occupation} ({soc}) ==={'  [REPLACEMENT for ' + replacement_for + ']' if replacement_for else ''}")
    items: list[schemas.Item] = []
    cards: list[schemas.EvidenceCard] = []
    seen_token_sets: list[set] = []
    tried_urls: set[str] = set()
    failures = 0  # quality failures (extract LLM errors)
    fetch_failures = 0  # infrastructure failures (HTTP/ScrapingBee)

    stats = {
        "discovery_rounds": 0,
        "urls_discovered": 0,
        "urls_fetched": 0,
        "fetch_failures": 0,
        "extract_failures": 0,
        "build_failures": 0,
        "verify_rejects": 0,
        "pretest_rejects_easy": 0,
        "pretest_rejects_hard": 0,
        "items_kept": 0,
        "tier_A": 0,
        "tier_B": 0,
        "tier_C": 0,
        "cognitive_type_counts": {},
        "replacement_for": replacement_for,
    }

    def _process_url(url_obj):
        nonlocal failures, fetch_failures
        url = url_obj["url"]
        if url in tried_urls:
            return
        tried_urls.add(url)

        fetched, ferr = source_fetcher.fetch_and_clean(url)
        if ferr or not fetched.get("text"):
            stats["fetch_failures"] += 1
            fetch_failures += 1  # tracked separately from quality failures
            log(f"  fetch fail [{ferr}]: {url[:80]}")
            return
        stats["urls_fetched"] += 1

        cs, eerr = evidence_cards.extract_cards(occupation, soc, url, fetched)
        if eerr:
            stats["extract_failures"] += 1
            failures += 1  # quality budget
            log(f"  extract fail [{eerr}]: {url[:80]}")
            return
        if not cs:
            stats["extract_failures"] += 1
            log(f"  no cards from {url[:80]}")
            return

        for card in cs:
            if len(items) >= target_items:
                return
            cards.append(card)
            item, berr = item_generator.build_item(card)
            if berr or item is None:
                stats["build_failures"] += 1
                log(f"    build fail [{berr}] (card {card.evidence_id})")
                continue
            tokens = _qtokens(item.question)
            if any(_jaccard(tokens, prev) >= config.DUP_JACCARD_THRESHOLD
                   for prev in seen_token_sets):
                log(f"    dup-dropped (jaccard) (card {card.evidence_id})")
                continue
            v = item_verifier.verify_item(item)
            if item.quality_tier is None:
                stats["verify_rejects"] += 1
                log(f"    verify-reject [{v.get('notes', '')[:60]}] (card {card.evidence_id})")
                continue
            # Pass 7 — difficulty pretest (3 models × 3 providers, closed-book)
            try:
                pretest_rec, verdict = difficulty_pretest.pretest(item)
                item.difficulty_pretest = pretest_rec
            except Exception as e:
                verdict = "keep"  # don't reject on pretest infra failure
                item.difficulty_pretest = {"error": f"{type(e).__name__}: {e}"}
            if verdict == "reject_too_easy":
                stats["pretest_rejects_easy"] += 1
                log(f"    pretest-reject (too easy, all 3 models correct) (card {card.evidence_id})")
                continue
            if verdict == "reject_too_hard_low_conf":
                stats["pretest_rejects_hard"] += 1
                log(f"    pretest-reject (all 3 wrong + low verifier confidence) (card {card.evidence_id})")
                continue
            # difficulty_estimate label from pretest n_correct
            n_corr_val = item.difficulty_pretest.get("n_correct")
            if isinstance(n_corr_val, int):
                if n_corr_val >= 3:
                    item.difficulty_estimate = "easy"
                elif n_corr_val == 2:
                    item.difficulty_estimate = "medium"
                else:
                    item.difficulty_estimate = "hard"
            # Cognitive type tagging
            try:
                item.cognitive_type = cognitive_type.classify(item)
            except Exception:
                item.cognitive_type = "other"
            stats["cognitive_type_counts"][item.cognitive_type] = (
                stats["cognitive_type_counts"].get(item.cognitive_type, 0) + 1
            )
            # Source-mix gate: per-SOC cap + CDC/OSHA/BLS canonical limits
            mix_ok, mix_reason = check_source_mix_acceptance(item, items, soc)
            if not mix_ok:
                stats["source_mix_rejects"] = stats.get("source_mix_rejects", 0) + 1
                log(f"    source-mix-reject [{mix_reason}] (card {card.evidence_id})")
                continue
            if replacement_for:
                item.replacement_for = replacement_for
            items.append(item)
            seen_token_sets.append(tokens)
            stats["items_kept"] += 1
            stats[f"tier_{item.quality_tier}"] = stats.get(f"tier_{item.quality_tier}", 0) + 1
            n_corr = item.difficulty_pretest.get("n_correct", "?")
            log(f"    ✓ item {len(items)}/{target_items}  tier={item.quality_tier}  "
                f"cog={item.cognitive_type}  pretest={n_corr}/3  ({card.source_domain})")

    # Round 0 — academic discovery (arXiv + PubMed Central, free APIs)
    # Pulls peer-reviewed papers for STEM/healthcare SOCs. Results bypass the
    # per-SOC whitelist via the global academic-domain allowlist.
    try:
        academic_urls = academic_sources.discover_academic_urls(occupation, soc, max_per_source=8)
        stats["academic_urls_discovered"] = len(academic_urls)
        log(f"  [round 0: academic] discovered {len(academic_urls)} URLs (arXiv + PMC)")
        for url_obj in academic_urls:
            if (len(items) >= target_items
                or failures >= config.MAX_FAILURES_PER_OCCUPATION
                or fetch_failures >= config.MAX_FETCH_FAILURES_PER_OCCUPATION):
                break
            _process_url(url_obj)
    except Exception as e:
        log(f"  [round 0: academic] error: {type(e).__name__}: {e}")
        stats["academic_urls_discovered"] = 0

    # Round 1 — general discovery
    stats["discovery_rounds"] = 1
    urls = search_client.discover_urls(occupation, soc)
    stats["urls_discovered"] += len(urls)
    log(f"  [round 1] discovered {len(urls)} URLs")
    for url_obj in urls:
        if (len(items) >= target_items
            or failures >= config.MAX_FAILURES_PER_OCCUPATION
            or fetch_failures >= config.MAX_FETCH_FAILURES_PER_OCCUPATION):
            break
        _process_url(url_obj)

    # Topical rounds — only if under target
    for aspect in search_client.TOPICAL_ASPECTS:
        if len(items) >= target_items:
            break
        if failures >= config.MAX_FAILURES_PER_OCCUPATION:
            log(f"  hit MAX_FAILURES (quality budget); stopping topical retries.")
            break
        if fetch_failures >= config.MAX_FETCH_FAILURES_PER_OCCUPATION:
            log(f"  hit MAX_FETCH_FAILURES; stopping topical retries.")
            break
        stats["discovery_rounds"] += 1
        log(f"  [round {stats['discovery_rounds']}: {aspect[:40]}...]")
        urls = search_client.discover_urls(occupation, soc, aspect=aspect, exclude=tried_urls)
        stats["urls_discovered"] += len(urls)
        log(f"    discovered {len(urls)} fresh URLs")
        for url_obj in urls:
            if len(items) >= target_items or failures >= config.MAX_FAILURES_PER_OCCUPATION:
                break
            _process_url(url_obj)

    log(f"  END  items={len(items)}/{target_items}  cards={len(cards)}  "
        f"fetched={stats['urls_fetched']}  qual_fails={failures}  fetch_fails={fetch_failures}")
    return {"items": items, "cards": cards, "stats": stats}


def _publisher_concentration(items_list: list) -> tuple[float, int, int]:
    """Returns (top_publisher_share, n_unique_publishers, n_unique_documents).

    Operates on a list of Item objects (not dicts). Used by the replacement
    pass to apply plan §3.4's source-dominance trigger.
    """
    from collections import Counter
    if not items_list:
        return 0.0, 0, 0
    pubs = Counter((it.publisher or it.source_domain or "") for it in items_list)
    docs = {(it.source_url, getattr(it, 'source_quote', '')[:50]) for it in items_list}
    n = sum(pubs.values())
    top_share = pubs.most_common(1)[0][1] / n if n else 0.0
    return top_share, len(pubs), len(docs)


def run(occupations: list[tuple[str, str]], *, run_dir: Path,
        target_items: int = config.TARGET_ITEMS_PER_OCCUPATION,
        reserves: dict[str, list[tuple[str, str]]] | None = None,
        min_items_for_accept: int = 15,
        max_publisher_share: float = 0.7,
        min_unique_publishers: int = 2,
        min_unique_documents: int = 3):
    """Run pipeline across occupations. Writes incrementally so a crash
    doesn't lose progress.

    Replacement loop (per v2 plan §3.4 — four triggers):
      After the primary pass, an occupation is replaced if ANY of:
        (a) items_kept < min_items_for_accept (default 15) — count threshold
        (b) top publisher's share > max_publisher_share (default 0.7) — source-dominance
        (c) n_unique_publishers < min_unique_publishers (default 2) — single-source
        (d) n_unique_documents < min_unique_documents (default 3) — too few docs
      Reserves are passed as {soc_major_group: [(occupation_title, soc), ...]}.

    Outputs:
      run_dir/items.jsonl       — accepted items (one JSON per line)
      run_dir/cards.jsonl       — all evidence cards extracted
      run_dir/stats.json        — per-occupation stats
      run_dir/replacements.json — replacement bookkeeping
      run_dir/run.log           — append-only log
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    items_path = run_dir / "items.jsonl"
    cards_path = run_dir / "cards.jsonl"
    stats_path = run_dir / "stats.json"
    replacements_path = run_dir / "replacements.json"
    log_path = run_dir / "run.log"

    log_f = open(log_path, "a", buffering=1)
    def log(msg):
        print(msg, flush=True)
        log_f.write(msg + "\n")

    log(f"\n========== RUN START  ({len(occupations)} occupations, target={target_items}) ==========")
    log(f"Reserves available for {len(reserves or {})} SOC major groups")
    log(f"Outputs: {items_path.name}, {cards_path.name}, {stats_path.name}")

    all_stats = {}
    if stats_path.exists():
        all_stats = json.loads(stats_path.read_text())

    items_f = open(items_path, "a", buffering=1)
    cards_f = open(cards_path, "a", buffering=1)

    def _process_and_persist(occupation, soc, replacement_for=None):
        try:
            result = process_occupation(occupation, soc, target_items=target_items,
                                        log=log, replacement_for=replacement_for)
        except Exception as e:
            log(f"!! ERROR in {occupation}: {type(e).__name__}: {e}")
            return None
        for it in result["items"]:
            items_f.write(json.dumps(it.to_dict(), ensure_ascii=False) + "\n")
        for c in result["cards"]:
            cards_f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
        rec_key = f"{soc}__replacement_for_{replacement_for}" if replacement_for else soc
        all_stats[rec_key] = {"occupation": occupation, **result["stats"]}
        stats_path.write_text(json.dumps(all_stats, indent=2))
        return result["stats"]

    # ---- Primary pass ----
    primary_results: dict = {}  # soc -> stats
    primary_items: dict = {}    # soc -> [Item objects collected for this occ]
    primary_soc_to_group: dict = {}  # soc -> SOC major group (for reserve lookup)
    for occupation, soc in occupations:
        # Resume support: skip a SOC only if it actually produced items.
        # SOCs that ended at 0 items might have failed due to OpenAI quota
        # (extract_failures dominates urls_fetched) — those should be retried.
        # SOCs with items_kept >= 1 are preserved as-is.
        if soc in all_stats:
            stats = all_stats[soc]
            kept = stats.get("items_kept", 0)
            if kept >= 1:
                log(f"[{occupation}] already done (resume; kept={kept}); skipping")
                primary_results[soc] = stats
                continue
            # 0-item SOC: detect quota-failure pattern and retry if so
            extract_fails = stats.get("extract_failures", 0)
            fetched = stats.get("urls_fetched", 0)
            if fetched > 5 and extract_fails / max(fetched, 1) > 0.95:
                log(f"[{occupation}] previous attempt failed (likely quota: {extract_fails}/{fetched} extract failures); retrying")
                # Remove the stale stats so this SOC gets fresh stats
                del all_stats[soc]
                # don't continue — fall through to re-process
            elif fetched > 0:
                # Genuine zero-yield: SOC was processed but content didn't work.
                # Skip to avoid wasted work (would just produce zero again).
                log(f"[{occupation}] previous attempt yielded 0 items (genuine, {extract_fails}/{fetched} extract fails); skipping")
                primary_results[soc] = stats
                continue
            # else (fetched == 0): never started → re-process
        # Capture the in-memory items list for source-concentration analysis
        try:
            result = process_occupation(occupation, soc, target_items=target_items, log=log)
        except Exception as e:
            log(f"!! ERROR in {occupation}: {type(e).__name__}: {e}")
            continue
        for it in result["items"]:
            items_f.write(json.dumps(it.to_dict(), ensure_ascii=False) + "\n")
        for c in result["cards"]:
            cards_f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
        all_stats[soc] = {"occupation": occupation, **result["stats"]}
        stats_path.write_text(json.dumps(all_stats, indent=2))
        primary_results[soc] = result["stats"]
        primary_items[soc] = result["items"]
        primary_soc_to_group[soc] = soc[:2]

    # ---- Replacement pass (per §3.4) ----
    replacements_log = []
    if reserves:
        log(f"\n========== REPLACEMENT PASS ==========")
        log(f"Triggers: items<{min_items_for_accept}  OR  top-pub>{max_publisher_share*100:.0f}%  "
            f"OR  unique-pubs<{min_unique_publishers}  OR  unique-docs<{min_unique_documents}")
        used_reserve_socs: set[str] = set()
        for occupation_soc, stats in list(primary_results.items()):
            kept = stats.get("items_kept", 0)
            occ_items = primary_items.get(occupation_soc, [])
            top_share, n_pubs, n_docs = _publisher_concentration(occ_items)

            triggers = []
            if kept < min_items_for_accept:
                triggers.append(f"items={kept}<{min_items_for_accept}")
            if kept > 0 and top_share > max_publisher_share:
                triggers.append(f"top_pub={top_share*100:.0f}%>{max_publisher_share*100:.0f}%")
            if kept > 0 and n_pubs < min_unique_publishers:
                triggers.append(f"n_pubs={n_pubs}<{min_unique_publishers}")
            if kept > 0 and n_docs < min_unique_documents:
                triggers.append(f"n_docs={n_docs}<{min_unique_documents}")
            if not triggers:
                continue

            grp = primary_soc_to_group.get(occupation_soc) or occupation_soc[:2]
            reserve_pool = reserves.get(grp, [])
            replacement = None
            for r_occ, r_soc in reserve_pool:
                if r_soc in used_reserve_socs:
                    continue
                if r_soc in primary_results:
                    continue
                replacement = (r_occ, r_soc)
                break
            if not replacement:
                log(f"  no reserves available for {occupation_soc} ({stats.get('occupation', '?')}); "
                    f"kept={kept}, triggers=[{', '.join(triggers)}]")
                continue
            r_occ, r_soc = replacement
            log(f"  replacing {occupation_soc} ({stats.get('occupation', '?')}, kept={kept}) "
                f"with reserve {r_soc} ({r_occ})  triggers=[{', '.join(triggers)}]")
            used_reserve_socs.add(r_soc)
            r_stats = _process_and_persist(r_occ, r_soc, replacement_for=occupation_soc)
            replacements_log.append({
                "removed_soc": occupation_soc,
                "removed_occupation": stats.get("occupation", ""),
                "removed_items_kept": kept,
                "removed_top_publisher_share": top_share,
                "removed_unique_publishers": n_pubs,
                "removed_unique_documents": n_docs,
                "replacement_soc": r_soc,
                "replacement_occupation": r_occ,
                "soc_major_group": grp,
                "replacement_items_kept": (r_stats or {}).get("items_kept", 0),
                "replacement_reason": "; ".join(triggers),
            })
            replacements_path.write_text(json.dumps(replacements_log, indent=2))

    # Plan-strict REPLACE per §3.4: drop the under-yield primary's items from
    # the bank when a replacement was successfully run for that SOC. The
    # replacement keeps its items (replacement_for set); the original primary's
    # items are moved to items_removed.jsonl for transparency.
    items_f.close()
    cards_f.close()
    if reserves and replacements_log:
        from . import post_replace
        result = post_replace.apply_replace_filter(run_dir)
        log(f"\n========== PLAN-STRICT REPLACE FILTER ==========")
        log(json.dumps(result, indent=2))

    log(f"\n========== RUN END ==========")
    log_f.close()
