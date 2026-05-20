"""Per-occupation source whitelist (associations + state boards + .gov baseline).

Reuses the upstream artifact `output/per_occupation_associations_pilot20.json`
that was already built by `select_associations_per_occupation.py` in the v1
pipeline. No need to regenerate for the pilot 20.

Tier 1A — professional associations / certification boards / standards bodies
Tier 1B — state licensing boards
Tier 2  — global baseline (.gov: bls/osha/cdc) for cross-cutting safety topics
"""
from __future__ import annotations
import json
import urllib.parse
from . import config

_DOMAINS_CACHE: dict | None = None
_ASSOCS_CACHE: dict | None = None


def _load():
    global _DOMAINS_CACHE, _ASSOCS_CACHE
    if _DOMAINS_CACHE is not None:
        return _DOMAINS_CACHE, _ASSOCS_CACHE
    domains: dict = {}
    assocs: dict = {}
    if config.PER_OCC_ASSOCIATIONS.exists():
        data = json.loads(config.PER_OCC_ASSOCIATIONS.read_text())
        for soc, rec in data.items():
            associations = rec.get("selected_associations") or rec.get("selected") or []
            state_boards = rec.get("state_licensing_boards") or []
            d = set()
            for p in associations + state_boards:
                u = p.get("url") or ""
                try:
                    h = urllib.parse.urlparse(u).netloc.lower()
                    if h.startswith("www."):
                        h = h[4:]
                    if h:
                        d.add(h)
                except Exception:
                    continue
            if d:
                domains[soc] = d
                assocs[soc] = {"associations": associations, "state_boards": state_boards}
    _DOMAINS_CACHE = domains
    _ASSOCS_CACHE = assocs
    return domains, assocs


def domains_for(soc: str) -> set[str]:
    """Returns the set of allowed domains for this occupation (Tier 1A + 1B + baseline)."""
    domains, _ = _load()
    base = config.GLOBAL_BASELINE_DOMAINS
    occ = domains.get(soc, set())
    return occ | base


def associations_for(soc: str) -> dict:
    """Returns {"associations": [...], "state_boards": [...]}."""
    _, assocs = _load()
    return assocs.get(soc, {"associations": [], "state_boards": []})


def is_whitelisted(url: str, soc: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False
    # Global academic domains (arXiv, PubMed Central, OpenStax, etc.) bypass per-SOC whitelist
    try:
        from . import academic_sources
        if academic_sources.is_global_academic_domain(host):
            return True
    except Exception:
        pass
    scope = domains_for(soc)
    for allowed in scope:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def domain_of(url: str) -> str:
    try:
        h = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    if h.startswith("www."):
        h = h[4:]
    return h


# Tier C: vendor documentation, only when the O*NET task involves a
# specific tool/product/software. We don't currently surface Tier C URLs in
# discovery (per-occupation whitelist excludes vendor domains by default), but
# if a vendor URL DID get into the bank via an explicit source-selection pick,
# this set marks well-known vendor documentation domains for Tier C tagging.
_VENDOR_DOC_DOMAINS = {
    # Software / SaaS vendors commonly cited as on-the-job tools
    "intuit.com", "quickbooks.intuit.com", "support.microsoft.com",
    "learn.microsoft.com", "support.apple.com", "developer.apple.com",
    "salesforce.com", "trailhead.salesforce.com", "help.salesforce.com",
    "epic.com", "cerner.com", "oracle.com", "sap.com",
    "autodesk.com", "help.autodesk.com",
    # Equipment / hardware vendors
    "siemens.com", "ge.com", "honeywell.com", "rockwellautomation.com",
    "milwaukeetool.com", "dewalt.com", "miltonroy.com",
    # Generic vendor doc patterns matched separately by suffix logic below
}


def is_vendor_domain(domain: str) -> bool:
    """Return True if domain looks like a vendor / commercial documentation
    site (Tier C). Matches on known vendor list + suffix heuristics."""
    if not domain:
        return False
    d = domain.lower().lstrip("www.")
    if d.startswith("www."):
        d = d[4:]
    if d in _VENDOR_DOC_DOMAINS:
        return True
    # Suffix heuristic: docs.* / support.* / help.* / developer.* + .com TLD
    for prefix in ("docs.", "support.", "help.", "developer.", "learn."):
        if d.startswith(prefix) and d.endswith(".com"):
            return True
    return False


def tier_for_domain(domain: str, soc: str) -> str:
    """Classify a domain into source tier A, B, or C for the item record.

    Tier A: state board, certification/licensing body, standards body, .gov baseline
    Tier B: other professional society / association
    Tier C: vendor documentation
    """
    domain = domain.lower().lstrip("www.")
    if domain.startswith("www."):
        domain = domain[4:]
    if domain in config.GLOBAL_BASELINE_DOMAINS:
        return "A"
    rec = associations_for(soc)
    for b in rec.get("state_boards") or []:
        if domain_of(b.get("url", "")) == domain:
            return "A"
    for a in rec.get("associations") or []:
        if domain_of(a.get("url", "")) == domain:
            kind = (a.get("kind") or "").lower()
            if kind in ("certification_board", "licensing_body", "standards_body"):
                return "A"
            return "B"
    # Vendor documentation = Tier C (only when justified by tool-specific task)
    if is_vendor_domain(domain):
        return "C"
    return "B"


# Friendly publisher names for the global baseline tier.
_BASELINE_PUBLISHERS = {
    "bls.gov": "U.S. Bureau of Labor Statistics",
    "osha.gov": "U.S. Occupational Safety and Health Administration",
    "cdc.gov": "U.S. Centers for Disease Control and Prevention",
}


def publisher_for_domain(domain: str, soc: str) -> str:
    """Return a human-readable publisher name for the source domain.
    Falls back to the bare domain if no friendly name is known."""
    if not domain:
        return ""
    d = domain.lower()
    if d.startswith("www."):
        d = d[4:]
    if d in _BASELINE_PUBLISHERS:
        return _BASELINE_PUBLISHERS[d]
    rec = associations_for(soc)
    for b in rec.get("state_boards") or []:
        if domain_of(b.get("url", "")) == d:
            return b.get("name", d)
    for a in rec.get("associations") or []:
        if domain_of(a.get("url", "")) == d:
            return a.get("name", d)
    return d
