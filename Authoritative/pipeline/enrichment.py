"""§4.2 Optional Enrichment Inputs — at occupation level.

Plan-listed enrichment sources:
  - BLS Occupational Outlook Handbook (OOH) — implemented (no key needed)
  - CareerOneStop Professional Associations API — implemented (needs key, gracefully no-op if absent)
  - CareerOneStop Certification Finder — implemented (same)
  - CareerOneStop License Finder — implemented (same)
  - Credential Engine — skipped (no public free API at occupation granularity)
  - ANAB / NCCA accreditation directories — skipped (search-only, no API)
  - State regulatory board pages — already covered by LLM picker in source_selection

Each implemented enricher returns a list of {name, url, domain, kind, source}
records that get merged into Pool B before source_selection runs the LLM picker.

All enrichers operate at OCCUPATION level (one fetch per SOC), aligned with
the call decision to defer task-level work.
"""
from __future__ import annotations
import json
import re
import ssl
import urllib.parse
import urllib.request

import certifi

from . import config

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15")

# CareerOneStop key file (optional). Format:
#   line 1: user_id
#   line 2: token
COS_KEY_FILE = config.REPO_ROOT / "api_key_careeronestop.txt"

# OOH index — used to map SOC -> OOH page URL
OOH_AZ_URL = "https://www.bls.gov/ooh/a-z-index.htm"

_OOH_SOC_MAP_CACHE: dict[str, str] | None = None
_OOH_CACHE_PATH = None


def _ooh_cache_path():
    global _OOH_CACHE_PATH
    if _OOH_CACHE_PATH is None:
        _OOH_CACHE_PATH = config.V2_OUT / "ooh_soc_map.json"
    return _OOH_CACHE_PATH


def _load_ooh_cache() -> dict[str, str]:
    global _OOH_SOC_MAP_CACHE
    if _OOH_SOC_MAP_CACHE is not None:
        return _OOH_SOC_MAP_CACHE
    p = _ooh_cache_path()
    if p.exists():
        _OOH_SOC_MAP_CACHE = json.loads(p.read_text())
    else:
        _OOH_SOC_MAP_CACHE = {}
    return _OOH_SOC_MAP_CACHE


def _save_ooh_cache():
    p = _ooh_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_OOH_SOC_MAP_CACHE or {}, indent=2))


def _ooh_url_via_serper(soc: str, occupation_title: str) -> str | None:
    """Use Serper to find the OOH page for this occupation. Cache by SOC."""
    cache = _load_ooh_cache()
    if soc in cache:
        return cache[soc] or None  # cached "" means we tried and found nothing
    if not config.SERPER_KEY:
        return None
    body = json.dumps({"q": f'site:bls.gov/ooh "{occupation_title}"', "num": 5}).encode("utf-8")
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=body, method="POST",
        headers={"X-API-KEY": config.SERPER_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
    except Exception:
        cache[soc] = ""
        _save_ooh_cache()
        return None
    chosen = ""
    for hit in data.get("organic", []) or []:
        link = (hit.get("link") or "").strip()
        if "/ooh/" in link and link.endswith(".htm") and "/ooh/home.htm" not in link:
            chosen = link
            break
    cache[soc] = chosen
    _save_ooh_cache()
    return chosen or None


def ooh_enrichment(soc: str, occupation_title: str = "") -> list[dict]:
    """Returns enrichment entries for the BLS OOH page for this SOC.

    Uses Serper to find the OOH page (cached). Adds the OOH page itself as
    a Tier-A enrichment entry — OOH pages have rich practice/career content
    plus typically a "More Information About This Occupation" section with
    association URLs.
    """
    if not occupation_title:
        return []
    url = _ooh_url_via_serper(soc, occupation_title)
    if not url:
        return []
    domain = urllib.parse.urlparse(url).netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return [{
        "name": f"BLS Occupational Outlook Handbook — {occupation_title}",
        "url": url,
        "domain": domain,
        "kind": "government_handbook",
        "source": "bls_ooh",
    }]


def careeronestop_enrichment(soc: str) -> list[dict]:
    """Returns CareerOneStop associations + certifications + licenses for this SOC.

    Requires api_key_careeronestop.txt (line1=user_id, line2=token). Returns
    [] if key file missing — graceful no-op."""
    if not COS_KEY_FILE.exists():
        return []
    parts = COS_KEY_FILE.read_text().strip().splitlines()
    if len(parts) < 2:
        return []
    user_id, token = parts[0].strip(), parts[1].strip()
    if not user_id or not token:
        return []

    out: list[dict] = []
    base = "https://api.careeronestop.org/v1"
    headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA, "Accept": "application/json"}

    # 1) Professional Associations
    assoc_url = f"{base}/professionalassociations/{user_id}/{soc}/Y"
    try:
        req = urllib.request.Request(assoc_url, headers=headers)
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
        for a in data.get("ProfessionalAssociation", []) or []:
            url = (a.get("Url") or "").strip()
            name = (a.get("OrgName") or "").strip()
            if not url or not name:
                continue
            domain = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
            if domain.startswith("www."):
                domain = domain[4:]
            out.append({"name": name, "url": url, "domain": domain,
                        "kind": "professional_society", "source": "cos_associations"})
    except Exception:
        pass

    # 2) Certifications (organization is the cert body)
    cert_url = f"{base}/certificationfinder/{user_id}/{soc}/0/0/0/0/0/0/0/Y"
    try:
        req = urllib.request.Request(cert_url, headers=headers)
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
        seen_orgs = set()
        for c in (data.get("CertList") or []):
            org_name = (c.get("OrganizationName") or "").strip()
            org_url = (c.get("Url") or "").strip()
            if not org_name or not org_url or org_name in seen_orgs:
                continue
            seen_orgs.add(org_name)
            domain = urllib.parse.urlparse(org_url).netloc.lower().lstrip("www.")
            if domain.startswith("www."):
                domain = domain[4:]
            out.append({"name": org_name, "url": org_url, "domain": domain,
                        "kind": "certification_board", "source": "cos_certifications"})
    except Exception:
        pass

    # 3) Licenses (state-level — already covered by LLM picker, but COS gives structured records)
    lic_url = f"{base}/licensecertificationfinder/{user_id}/{soc}/Y"
    try:
        req = urllib.request.Request(lic_url, headers=headers)
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
        seen_lic = set()
        for l in (data.get("LicensingAgencyList") or []):
            name = (l.get("AgencyName") or "").strip()
            url = (l.get("AgencyUrl") or "").strip()
            if not name or not url or url in seen_lic:
                continue
            seen_lic.add(url)
            domain = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
            if domain.startswith("www."):
                domain = domain[4:]
            out.append({"name": name, "url": url, "domain": domain,
                        "kind": "licensing_body", "source": "cos_licenses"})
    except Exception:
        pass

    return out


def enrich_pool(soc: str, occupation_title: str = "") -> list[dict]:
    """All §4.2 enrichments combined. Returns merged list of enrichment entries
    suitable for adding to Pool B in source_selection."""
    out: list[dict] = []
    out.extend(ooh_enrichment(soc, occupation_title))
    out.extend(careeronestop_enrichment(soc))
    # Dedupe by domain (keep first occurrence — OOH first, then COS associations, certs, licenses)
    seen = set()
    deduped = []
    for e in out:
        key = e.get("domain", "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped


def enrichment_status() -> dict:
    return {
        "bls_ooh": "active (no key needed)",
        "careeronestop": "active" if COS_KEY_FILE.exists() else "INACTIVE — provide api_key_careeronestop.txt (user_id on line 1, token on line 2)",
        "credential_engine": "skipped (no public free API)",
        "anab_ncca": "skipped (search-only directories, no API)",
        "state_regulatory_boards": "covered by LLM picker in source_selection (existing behavior)",
    }
