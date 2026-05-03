"""Serper.dev search client.

Replaces OpenAI's web_search tool (the tightest rate-limit bottleneck in the
v1 pipeline). Serper uses Google's index, has 2,500 free queries on signup,
and no per-second throttling at our scale.

Builds site:-restricted queries against the per-occupation whitelist so we
get pages from those domains specifically, instead of OpenAI web_search's
.gov-biased default ranking.
"""
from __future__ import annotations
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from typing import Iterable

import certifi

from . import config
from . import source_whitelist
from . import onet_context

SERPER_URL = "https://google.serper.dev/search"
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# Track which keys have been observed exhausted/erroring so we don't waste
# requests on them. Module-level — resets per process.
_EXHAUSTED_KEYS: set[str] = set()

# Topical aspects used to widen the search budget for occupations that
# under-yield on the first round. Same list as 94_authdoc_extended.
TOPICAL_ASPECTS = [
    "safety procedures, hazard mitigation, and emergency response",
    "certification, licensing requirements, and continuing education",
    "day-to-day operational best practices and standard operating procedures",
    "common challenges, troubleshooting, and remediation of errors",
    "regulatory compliance, documentation, and reporting requirements",
    "specific tools, software, equipment, and instruments used on the job",
    "customer, client, patient, or stakeholder interaction and communication",
    "professional ethics, legal duties, and conduct expectations",
    "quality control, quality assurance, and performance evaluation standards",
]


def _serper_query(query: str, num: int = 10) -> list[dict]:
    """Issue one Serper search. Returns list of {link, title, snippet}.

    Tries each available key in order (config.SERPER_KEYS). If a key returns
    0 results AND a 4xx (e.g. quota), mark it exhausted and fall through to
    the next key. If a key returns 0 results but no error, it's just a query
    with no hits — don't mark exhausted.
    """
    if not config.SERPER_KEYS:
        raise RuntimeError("No Serper keys configured (write to api_key_serper.txt)")
    body = json.dumps({"q": query, "num": num}).encode("utf-8")
    last_err = None
    for key in config.SERPER_KEYS:
        if key in _EXHAUSTED_KEYS:
            continue
        req = urllib.request.Request(
            SERPER_URL, data=body, method="POST",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
                data = json.loads(r.read().decode("utf-8", errors="ignore"))
        except urllib.error.HTTPError as e:
            # Serper returns: 400 for quota exhausted, 401 for bad key,
            # 402/403 for forbidden, 429 for rate-limit. All → mark exhausted.
            last_err = f"HTTP {e.code}"
            if e.code in (400, 401, 402, 403, 429):
                _EXHAUSTED_KEYS.add(key)
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        out = []
        for hit in data.get("organic", []) or []:
            link = (hit.get("link") or "").strip()
            if not link:
                continue
            out.append({
                "link": link,
                "title": (hit.get("title") or "").strip(),
                "snippet": (hit.get("snippet") or "").strip(),
            })
        return out  # success — even if 0 hits, this key is fine
    # All keys failed/exhausted
    return []


# Plan §6.3 query templates. Each runs against the per-occupation domain
# whitelist (or the baseline .gov/.edu fallback for cross-cutting rounds).
# These produce richer source coverage than a single OR-pattern query.
_DOC_TYPE_TEMPLATES = [
    'site:{domain} "{occupation}" "standard"',
    'site:{domain} "{occupation}" "guideline"',
    'site:{domain} "{occupation}" "manual"',
    'site:{domain} "{occupation}" "procedure"',
    'site:{domain} "{occupation}" "code of ethics"',
    'site:{domain} "{occupation}" "certification handbook"',
    'site:{domain} "{occupation}" "exam content outline"',
    'site:{domain} "{occupation}" "competency"',
    'site:{domain} "{occupation}" filetype:pdf',
]

_FALLBACK_TEMPLATES = [
    '"{occupation}" "{aspect}" site:.gov',
    '"{occupation}" "{aspect}" site:.edu',
    '"{occupation}" "{aspect}" "standard"',
    '"{occupation}" "{aspect}" "practice guideline"',
]


def _build_queries(occupation: str, soc: str, aspect: str = "") -> list[str]:
    """Build site:-restricted query set per plan §6.3.

    Round 1 (no aspect): runs the doc-type templates against each per-occupation
    domain (standard / guideline / manual / procedure / code of ethics / cert
    handbook / exam content / competency / pdf).

    Topical rounds (aspect set): combines the aspect keyword with the
    occupation across each per-occupation domain, plus a small set of .gov/.edu
    fallback queries.
    """
    allowed = sorted(source_whitelist.domains_for(soc))

    queries: list[str] = []

    if aspect:
        # Topical round: occupation × aspect × domain
        keyword_combined = f'({aspect})'
        for domain in allowed:
            queries.append(f'site:{domain} "{occupation}" {keyword_combined}')
        # plus 2 fallback queries against .gov / .edu (cross-cutting safety/labor)
        for tmpl in _FALLBACK_TEMPLATES[:2]:
            queries.append(tmpl.format(occupation=occupation, aspect=aspect))
    else:
        # Round 1: doc-type-rich queries per domain; cap templates per domain
        # so the global budget stays in MAX_DISCOVERY_QUERIES_PER_OCCUPATION.
        per_domain_cap = max(1, config.MAX_DISCOVERY_QUERIES_PER_OCCUPATION
                             // max(1, len(allowed)))
        per_domain_cap = min(per_domain_cap, len(_DOC_TYPE_TEMPLATES))
        for domain in allowed:
            for tmpl in _DOC_TYPE_TEMPLATES[:per_domain_cap]:
                queries.append(tmpl.format(domain=domain, occupation=occupation))

    seen = set()
    out = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[:config.MAX_DISCOVERY_QUERIES_PER_OCCUPATION]


_PAYWALLED_URL_PATTERNS = (
    # IEEE Xplore deep-link PDFs (iel5/iel7) are subscription-only — even
    # ScrapingBee with premium+JS gets only the auth landing page.
    re.compile(r"ieeexplore\.ieee\.org/iel\d/", re.I),
)


def _is_known_paywalled_url(url: str) -> bool:
    return any(p.search(url) for p in _PAYWALLED_URL_PATTERNS)


def discover_urls(occupation: str, soc: str, *, aspect: str = "",
                  exclude: Iterable[str] = ()) -> list[dict]:
    """Returns a list of {url, title, snippet, domain} URLs filtered through
    the per-occupation whitelist. exclude: URLs to skip."""
    excl = set(exclude)
    queries = _build_queries(occupation, soc, aspect=aspect)
    seen_urls = set()
    out = []
    for q in queries:
        try:
            hits = _serper_query(q, num=10)
        except Exception:
            hits = []
        for h in hits:
            u = h["link"].split("?utm_")[0]
            if u in seen_urls or u in excl:
                continue
            if _is_known_paywalled_url(u):
                continue
            if not source_whitelist.is_whitelisted(u, soc):
                continue
            seen_urls.add(u)
            out.append({
                "url": u,
                "title": h["title"],
                "snippet": h["snippet"],
                "domain": source_whitelist.domain_of(u),
            })
        time.sleep(0.05)  # gentle pacing, well under any Serper limit
    return out
