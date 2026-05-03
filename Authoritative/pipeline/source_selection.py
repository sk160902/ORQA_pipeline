"""Per-occupation source whitelist builder.

Given a list of (occupation_title, soc_code) pairs, builds a whitelist of
authoritative associations + state licensing boards for each, using one LLM
call per occupation against:
  Pool A — O*NET's per-SOC associations (scraped from onetonline.org)
  Pool B — full O*NET professional-associations xlsx (~3,124 entries)

Same logic as the v1 select_associations_per_occupation.py but parameterized
over the occupation list (so it works for the wage-bill-selected top 20).

Picks are validated: every selected (name, url) must exact-match a row in
Pool A ∪ Pool B. State-board URLs are LLM-provided + HEAD-validated.
"""
from __future__ import annotations
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
import urllib.error

import certifi

from . import config
from . import clients
from . import onet_context
from . import enrichment

ALL_ASSOCS_XLSX = config.LEGACY_OUT / "All_Professional_Associations.xlsx"
SSL_CTX = ssl.create_default_context(cafile=certifi.where())
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15")

ASSOC_ROW_RE = re.compile(
    r'data-title="Related Occupations"\s+data-text="(?P<count>\d+)"'
    r'.*?'
    r'data-title="Association"\s+data-text="(?P<name>[^"]+)"'
    r'.*?'
    r'<a\s+href="(?P<url>https?://[^"]+)"',
    re.DOTALL,
)

SELECTION_SYSTEM = (
    "You are an expert occupational researcher with deep knowledge of US "
    "professional certification, state licensing, and occupational standards. "
    "You select the most authoritative published sources of practice guidance "
    "for a given occupation, with explicit priority on certifying / licensing "
    "bodies (the organizations workers are formally accountable to)."
)

SELECTION_USER_TEMPLATE = """OCCUPATION: {occupation} (SOC {soc})

This occupation is also known as: {alt_titles}

Workers in this role typically perform tasks such as:
{tasks_block}

POOL A (O*NET-curated subset for this SOC, strong prior).
The "rc" column is "related_count" — how many OTHER O*NET occupations the
association also serves. Lower rc = more occupation-specific. The "spec"
column is org_specificity = 1 / log(2 + rc). Prefer HIGH spec picks (i.e.
occupation-specific bodies) over generalist umbrella organizations.
This list is sorted by spec DESC (most specific first):
{pool_a_block}

POOL B (full O*NET professional-association list — broader pool, may surface
authorities relevant to this occupation that POOL A missed):
{pool_b_block}

PART 1 — Select 8-15 PROFESSIONAL ASSOCIATIONS from POOL A or POOL B whose
websites would be the most authoritative practice-guidance sources for this
occupation.

PRIORITIZATION:
  1. CERTIFYING / LICENSING BODIES first.
  2. PROFESSIONAL SOCIETIES that publish standards, clinical guidelines,
     technical SOPs, or apprenticeship curricula online.
  3. STANDARDS-SETTING BODIES (NFPA, ASHRAE, ANSI, etc.).
  4. Last priority: general/advocacy bodies without published practice guidance.

EXCLUSIONS:
  1. EXCLUDE labor-organizing bodies UNLESS they publish occupation-specific
     practice guidance.
  2. PREFER national over regional/general societies.

If after applying these rules you have fewer than 8 picks, return fewer.
Don't pad with off-topic orgs. If POOL A is empty (some SOCs have no formal
O*NET association cross-references), rely on POOL B.

PART 2 — Provide 1-3 STATE LICENSING BOARDS that license this occupation in
the US. State boards are NOT in either pool — provide URLs from your
knowledge. Pick prominent state boards (CA, TX, FL, NY usually have the
deepest published practice guidance). If this occupation is NOT state-licensed
(e.g. Customer Service Reps, Fast Food Workers), return an empty array.

OUTPUT — return ONLY a JSON object (no code fences):
{{
  "occupation": "{occupation}",
  "soc": "{soc}",
  "selected_associations": [
    {{
      "name": "<exact name from POOL A or POOL B>",
      "url":  "<exact url from POOL A or POOL B>",
      "pool": "A" | "B",
      "kind": "certification_board" | "licensing_body" | "professional_society" | "standards_body" | "advocacy_body",
      "related_count": <int>,
      "why_relevant": "<one short sentence>"
    }}
  ],
  "state_licensing_boards": [
    {{
      "name": "<state board name>",
      "url":  "<homepage URL>",
      "state": "CA" | ...,
      "why_relevant": "<one short sentence>"
    }}
  ]
}}
"""


def scrape_pool_a(soc: str, retries: int = 3) -> list[dict]:
    """Scrape onetonline.org for per-SOC associations."""
    url = f"https://www.onetonline.org/search/associations/list/{soc}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
                html = r.read().decode("utf-8", errors="ignore")
            break
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    else:
        return []
    rows = []
    seen = set()
    for m in ASSOC_ROW_RE.finditer(html):
        u = m.group("url").rstrip("/")
        if u in seen:
            continue
        seen.add(u)
        rows.append({
            "name": m.group("name").strip(),
            "url": m.group("url").strip(),
            "related_count": int(m.group("count")),
        })
    return rows


_POOL_B_CACHE: list[dict] | None = None


def load_pool_b() -> list[dict]:
    """Load the full O*NET professional-associations xlsx once."""
    global _POOL_B_CACHE
    if _POOL_B_CACHE is not None:
        return _POOL_B_CACHE
    import pandas as pd
    df = pd.read_excel(ALL_ASSOCS_XLSX, sheet_name="Professional Associations", header=3)
    df.columns = ["association", "website", "onet_ally"]
    df = df.dropna(subset=["association"]).reset_index(drop=True)
    out = []
    for _, r in df.iterrows():
        url = str(r.get("website") or "").strip()
        if not url or not url.startswith("http"):
            continue
        try:
            host = urllib.parse.urlparse(url).netloc.lower()
            if host.startswith("www."):
                host = host[4:]
        except Exception:
            continue
        if not host:
            continue
        out.append({"name": str(r["association"]).strip(), "url": url, "domain": host})
    _POOL_B_CACHE = out
    return out


def _head_check(url: str, timeout: int = 6) -> bool:
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                code = r.getcode()
                if 200 <= code < 400:
                    return True
        except urllib.error.HTTPError as e:
            if e.code == 405 and method == "HEAD":
                continue
            if 200 <= e.code < 400:
                return True
            return False
        except Exception:
            return False
    return False


_KIND_BOOSTS = {
    # Plan §6.2 optional boosts (additive on top of base specificity)
    "certification_board": 0.40,
    "licensing_body":      0.40,
    "standards_body":      0.30,
    "regulatory_board":    0.30,
    "professional_society": 0.10,
    "advocacy_body":       0.00,
    "government_handbook": 0.20,  # for §4.2 BLS OOH enrichments
}


def org_specificity(related_count: int, *, kind: str = "", is_onet_ally: bool = False,
                    occupation_specific_domain: bool = False) -> float:
    """Plan §6.2: org_specificity = 1 / log(2 + n_distinct_occupations_linked)
    plus optional boosts per kind, O*NET-Ally status, occupation-specific domain.

    Lower related_count (occupation-specific) → higher base specificity.
    """
    import math
    base = 1.0 / math.log(2 + max(0, int(related_count)))
    boost = _KIND_BOOSTS.get((kind or "").lower(), 0.0)
    if is_onet_ally:
        boost += 0.10
    if occupation_specific_domain:
        boost += 0.10
    return base + boost


def _build_prompt(occupation: str, soc: str, pool_a: list[dict],
                  pool_b: list[dict]) -> str:
    ctx = onet_context.get(soc)
    alts = "; ".join(ctx.get("alternate_titles", [])) or "(none)"
    tasks = "\n".join(f"  - {t}" for t in ctx.get("key_tasks", [])) or "  (none)"
    if pool_a:
        # Sort by org_specificity DESC (most occupation-specific first) per plan §6.2.
        # Pool A doesn't carry `kind` until after the LLM picks, so use base score
        # (related_count only) for the pre-pick ordering.
        sorted_a = sorted(pool_a, key=lambda c: org_specificity(c.get("related_count", 99)), reverse=True)
        a_lines = []
        for i, c in enumerate(sorted_a, 1):
            spec = org_specificity(c.get("related_count", 99))
            a_lines.append(f"  {i:>2}. [rc={c['related_count']:>2}  spec={spec:.2f}]  {c['name']}  —  {c['url']}")
        pool_a_block = "\n".join(a_lines)
    else:
        pool_a_block = "  (no per-SOC associations in O*NET — rely on POOL B)"
    pool_b_block = "\n".join(f"  {c['name']} | {c['domain']}" for c in pool_b)
    return SELECTION_USER_TEMPLATE.format(
        occupation=occupation, soc=soc,
        alt_titles=alts, tasks_block=tasks,
        pool_a_block=pool_a_block, pool_b_block=pool_b_block,
    )


def _validate(raw: str, pool_a: list[dict], pool_b: list[dict]) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    obj = json.loads(raw)
    valid_keys = set()
    for c in pool_a:
        d = urllib.parse.urlparse(c["url"]).netloc.lower()
        if d.startswith("www."):
            d = d[4:]
        valid_keys.add((c["name"], d))
    for c in pool_b:
        valid_keys.add((c["name"], c["domain"]))
    kept = []
    dropped = []
    for pick in obj.get("selected_associations", []):
        url = pick.get("url", "")
        try:
            d = urllib.parse.urlparse(url).netloc.lower()
            if d.startswith("www."):
                d = d[4:]
        except Exception:
            d = ""
        if (pick.get("name", ""), d) in valid_keys:
            kept.append(pick)
        else:
            dropped.append(pick)
    obj["selected_associations"] = kept
    obj["_dropped_invalid_associations"] = dropped
    boards_kept, boards_dropped = [], []
    for b in obj.get("state_licensing_boards", []):
        if b.get("url") and _head_check(b["url"]):
            boards_kept.append(b)
        else:
            b["_failed_head"] = True
            boards_dropped.append(b)
    obj["state_licensing_boards"] = boards_kept
    obj["_dropped_invalid_boards"] = boards_dropped
    return obj


def select_for(occupation: str, soc: str, *, pool_b: list[dict] | None = None) -> dict:
    """Run source selection for one occupation. Returns the validated record.

    Pool B is enriched with §4.2 sources (BLS OOH + CareerOneStop) before
    being passed to the LLM picker, so the picker can select from those
    occupation-specific structured sources too. Enrichment entries are also
    included in the validation key set so picks against them survive validation.
    """
    pool_b = list(pool_b or load_pool_b())
    enrichment_entries = enrichment.enrich_pool(soc, occupation)
    # Add enrichment to Pool B (dedupe by name+domain)
    seen_keys = {(c.get("name", ""), c.get("domain", "")) for c in pool_b}
    for e in enrichment_entries:
        key = (e.get("name", ""), e.get("domain", ""))
        if key in seen_keys:
            continue
        pool_b.append(e)
        seen_keys.add(key)

    pool_a = scrape_pool_a(soc)
    prompt = _build_prompt(occupation, soc, pool_a, pool_b)
    txt, err = clients.call_openai(
        prompt, model="gpt-4o", temperature=0.0, max_tokens=4000,
        response_format={"type": "json_object"}, system=SELECTION_SYSTEM,
    )
    if err:
        return {"occupation": occupation, "soc": soc, "error": err}
    try:
        obj = _validate(txt, pool_a, pool_b)
    except Exception as e:
        return {"occupation": occupation, "soc": soc, "error": f"parse: {e}"}
    obj["pool_a_count"] = len(pool_a)
    obj["enrichment_count"] = len(enrichment_entries)
    obj["enrichment_sources"] = [e.get("source") for e in enrichment_entries]

    # Plan §6.1: enrich each kept association with the recommended schema
    # (org_id, source_system, category, is_onet_ally, retrieved_at, notes).
    # Plus compute final org_specificity per pick using kind boost.
    import time as _time
    today = _time.strftime("%Y-%m-%d")
    pool_a_keys = {(c["name"], urllib.parse.urlparse(c["url"]).netloc.lower().lstrip("www.")):
                    c for c in pool_a}
    pool_b_keys = {(c["name"], c["domain"]): c for c in pool_b}
    for i, pick in enumerate(obj.get("selected_associations", []), 1):
        d = urllib.parse.urlparse(pick.get("url", "")).netloc.lower()
        if d.startswith("www."):
            d = d[4:]
        rc = pick.get("related_count")
        if rc is None or rc == 0:
            # Try to recover related_count from Pool A if pick came from there
            src_a = pool_a_keys.get((pick.get("name", ""), d))
            if src_a:
                rc = src_a.get("related_count", 99)
            else:
                rc = 99
        kind = (pick.get("kind") or "").lower()
        # Detect occupation-specific domain (very rough: if the occupation
        # title or first non-stop word appears in the domain)
        occ_words = [w.lower() for w in occupation.split() if len(w) > 4]
        occ_specific = any(w in d for w in occ_words)
        # is_onet_ally — only Pool B has this signal in the source xlsx; we
        # don't carry it through, so default False (would need pool_b row check).
        spec = org_specificity(rc, kind=kind, occupation_specific_domain=occ_specific)
        # Determine source_system per plan §6.1
        if pool_a_keys.get((pick.get("name", ""), d)):
            source_system = "onet_per_soc_associations"
        elif (pick.get("name", ""), d) in pool_b_keys:
            # Could be a Pool-B pick OR an enrichment (BLS OOH / CareerOneStop)
            # Check if this domain came from enrichment
            ent = next((e for e in enrichment_entries if e.get("name", "") == pick.get("name", "")), None)
            if ent:
                source_system = ent.get("source", "enrichment")
            else:
                source_system = "onet_full_xlsx"
        else:
            source_system = "unknown"
        # Annotate the pick with the §6.1 recommended fields
        pick["org_id"] = f"org_{i:06d}_{soc.replace('-','')}"
        pick["source_system"] = source_system
        pick["category"] = "national" if rc and rc > 5 else "specialist"
        pick["is_onet_ally"] = False  # not carried through Pool B; would require xlsx col read
        pick["retrieved_at"] = today
        pick["notes"] = ""
        pick["org_specificity"] = round(spec, 4)

    return obj


def build_whitelists(occupations: list[tuple[str, str]],
                     existing: dict | None = None,
                     incremental_save_path=None,
                     incremental_save_every: int = 5) -> dict:
    """Build whitelists for a list of (occupation, soc) pairs.

    If `existing` is provided, results are merged into it (existing entries
    are preserved, new entries are added).

    If `incremental_save_path` (Path) is provided, writes the merged dict
    to disk after every `incremental_save_every` new SOCs — so a kill mid-build
    leaves work preserved. On re-run, SOCs already in the saved file are skipped.
    """
    import json
    out = dict(existing or {})
    pool_b = load_pool_b()
    n_built_since_save = 0
    for i, (occ, soc) in enumerate(occupations, 1):
        if soc in out:
            # Quiet skip in resumable mode — these are previously-built SOCs
            continue
        print(f"  [select {i}/{len(occupations)}] {occ} ({soc})...", flush=True)
        rec = select_for(occ, soc, pool_b=pool_b)
        if "error" in rec:
            print(f"    ERROR: {rec['error']}")
            continue
        n_a = len(rec.get("selected_associations", []))
        n_b = len(rec.get("state_licensing_boards", []))
        print(f"    picked {n_a} associations + {n_b} state boards")
        out[soc] = rec
        n_built_since_save += 1
        # Incremental save — preserves work on interrupt
        if incremental_save_path is not None and n_built_since_save >= incremental_save_every:
            try:
                incremental_save_path.write_text(json.dumps(out, indent=2))
                print(f"    [checkpoint] saved whitelist progress: {len(out)} SOCs in dict", flush=True)
                n_built_since_save = 0
            except Exception as e:
                print(f"    [checkpoint failed: {e}]")
        time.sleep(0.5)
    # Final save
    if incremental_save_path is not None:
        try:
            incremental_save_path.write_text(json.dumps(out, indent=2))
            print(f"    [final] saved whitelist: {len(out)} SOCs", flush=True)
        except Exception:
            pass
    return out
