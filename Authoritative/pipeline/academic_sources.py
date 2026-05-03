"""Academic source integrations: arXiv, PubMed Central, Unpaywall.

These three free APIs give the pipeline access to:
- arXiv: ~2.5M open-access preprints (CS, math, physics, EE, stats, q-fin, q-bio)
- PubMed Central: NIH open-access biomedical papers
- Unpaywall: given a DOI, returns legal free preprint URL if exists

All are FREE, no API keys required, no rate-limit registration.

Used to expand discovery beyond Serper (which surfaces general web results)
into the academic literature corpus relevant to STEM and healthcare SOCs.
"""
from __future__ import annotations
import json
import ssl
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Iterable

SSL_CTX = ssl.create_default_context()
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = ssl._create_unverified_context()

UA = "ONET-MCQ-Pipeline/1.0 (academic-research; contact: research@anthropic.com)"

# SOC major group → arXiv categories (only applies to STEM-relevant SOCs)
SOC_TO_ARXIV_CATS: dict[str, list[str]] = {
    # Computer & Mathematical (15)
    "15-1212.00": ["cs.CR", "cs.SE"],                        # Information Security Analysts
    "15-1221.00": ["cs.AI", "cs.LG", "cs.SE"],               # Computer & Information Research Scientists
    "15-1231.00": ["cs.NI", "cs.SE"],                        # Computer Network Support Specialists
    "15-1232.00": ["cs.HC", "cs.SE"],                        # Computer User Support Specialists
    "15-1241.00": ["cs.NI", "cs.DC"],                        # Computer Network Architects
    "15-1242.00": ["cs.DB"],                                 # Database Administrators
    "15-1244.00": ["cs.NI", "cs.OS"],                        # Network and Computer Systems Administrators
    "15-1251.00": ["cs.SE", "cs.PL"],                        # Computer Programmers
    "15-1252.00": ["cs.SE", "cs.PL", "cs.SY"],               # Software Developers
    "15-1254.00": ["cs.SE", "cs.HC"],                        # Web Developers
    "15-2011.00": ["q-fin.RM", "stat.AP"],                   # Actuaries
    "15-2021.00": ["math.NT", "math.AG", "math.AP"],         # Mathematicians
    "15-2031.00": ["math.OC", "stat.AP"],                    # Operations Research Analysts
    "15-2041.00": ["stat.ME", "stat.AP"],                    # Statisticians
    "15-2051.00": ["cs.LG", "stat.ML"],                      # Data Scientists
    # Engineering (17)
    "17-1011.00": ["cs.GR"],                                 # Architects (computational design intersection)
    "17-1021.00": ["cs.CV"],                                 # Cartographers
    "17-2011.00": ["physics.flu-dyn", "physics.app-ph"],     # Aerospace Engineers
    "17-2031.00": ["q-bio.QM", "physics.bio-ph"],            # Biomedical Engineers
    "17-2041.00": ["physics.chem-ph", "physics.flu-dyn"],    # Chemical Engineers
    "17-2051.00": ["physics.soc-ph"],                        # Civil Engineers
    "17-2061.00": ["cs.AR"],                                 # Computer Hardware Engineers
    "17-2071.00": ["eess.SP", "eess.SY"],                    # Electrical Engineers
    "17-2072.00": ["eess.SP", "eess.SY"],                    # Electronics Engineers
    "17-2081.00": ["physics.ao-ph", "physics.geo-ph"],       # Environmental Engineers
    "17-2111.00": ["physics.app-ph"],                        # Health & Safety Engineers
    "17-2112.00": ["math.OC", "stat.AP"],                    # Industrial Engineers
    "17-2121.00": ["physics.flu-dyn"],                       # Marine Engineers
    "17-2131.00": ["cond-mat.mtrl-sci", "physics.app-ph"],   # Materials Engineers
    "17-2141.00": ["physics.app-ph", "physics.flu-dyn"],     # Mechanical Engineers
    "17-2151.00": ["physics.geo-ph"],                        # Mining Engineers
    "17-2171.00": ["physics.geo-ph", "physics.flu-dyn"],     # Petroleum Engineers
    # Sciences (19)
    "19-1011.00": ["q-bio.OT"],                              # Animal Scientists / Food Scientists
    "19-1012.00": ["q-bio.OT"],                              # Food Scientists
    "19-1021.00": ["q-bio.BM", "q-bio.QM"],                  # Biochemists and Biophysicists
    "19-1022.00": ["q-bio.PE", "q-bio.QM"],                  # Microbiologists
    "19-1023.00": ["q-bio.PE"],                              # Wildlife Biologists
    "19-1031.00": ["q-bio.PE", "physics.geo-ph"],            # Conservation Scientists
    "19-1032.00": ["q-bio.PE"],                              # Foresters
    "19-2011.00": ["astro-ph.GA", "astro-ph.SR"],            # Astronomers
    "19-2012.00": ["physics.app-ph", "cond-mat.soft"],       # Physicists
    "19-2021.00": ["physics.ao-ph"],                         # Atmospheric Scientists
    "19-2031.00": ["physics.chem-ph"],                       # Chemists
    "19-2041.00": ["physics.geo-ph", "physics.ao-ph"],       # Environmental Scientists
    "19-2042.00": ["physics.geo-ph"],                        # Geoscientists
    "19-2043.00": ["physics.ao-ph", "physics.geo-ph"],       # Hydrologists
    "19-3032.00": ["q-bio.NC"],                              # I-O Psychologists
    "19-3034.00": ["q-bio.NC"],                              # School Psychologists
    "19-3041.00": ["physics.soc-ph"],                        # Sociologists
    "19-3092.00": ["physics.geo-ph", "physics.soc-ph"],      # Geographers
    # Finance (13)
    "13-2011.00": ["q-fin.GN"],                              # Accountants and Auditors
    "13-2052.00": ["q-fin.PM", "q-fin.RM"],                  # Personal Financial Advisors
    "13-2053.00": ["q-fin.RM"],                              # Insurance Underwriters
    "13-2072.00": ["q-fin.GN", "q-fin.RM"],                  # Loan Officers
    "13-1041.00": ["cs.CY", "q-fin.GN"],                     # Compliance Officers
    "13-1051.00": ["q-fin.GN", "stat.AP"],                   # Cost Estimators
    "13-1081.00": ["math.OC"],                               # Logisticians
    "13-1082.00": ["cs.SE", "math.OC"],                      # Project Management Specialists
}


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read()


def search_arxiv(query: str, categories: list[str] | None = None,
                 max_results: int = 10) -> list[dict]:
    """Search arXiv via the Atom feed API. Returns list of {url, title, snippet, domain}.

    Categories filter: e.g. ["cs.SE", "cs.AI"] — restricts to those subjects.
    """
    parts = []
    parts.append(f'all:"{query}"')
    if categories:
        cat_clause = " OR ".join(f"cat:{c}" for c in categories)
        parts.append(f"({cat_clause})")
    search_query = " AND ".join(parts)
    params = {"search_query": search_query,
              "start": "0",
              "max_results": str(max_results),
              "sortBy": "relevance",
              "sortOrder": "descending"}
    api = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    try:
        body = _http_get(api, timeout=30)
    except Exception:
        return []

    out = []
    try:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(body)
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            title = (title_el.text or "").strip() if title_el is not None else ""
            summary = (summary_el.text or "").strip() if summary_el is not None else ""
            pdf_url = ""
            for link in entry.findall("atom:link", ns):
                if link.get("title") == "pdf":
                    pdf_url = link.get("href", "")
                    break
            if not pdf_url:
                # Fallback: id field is the abstract page; convert to PDF URL
                id_el = entry.find("atom:id", ns)
                if id_el is not None and id_el.text:
                    pdf_url = id_el.text.replace("/abs/", "/pdf/") + ".pdf"
            if pdf_url:
                out.append({
                    "url": pdf_url,
                    "title": title[:200],
                    "snippet": summary[:300],
                    "domain": "arxiv.org",
                    "source_kind": "arxiv",
                })
    except Exception:
        return []
    # Be polite — arXiv asks for 1 req per 3 sec
    time.sleep(3)
    return out


def search_pubmed_central(query: str, max_results: int = 10) -> list[dict]:
    """Search PubMed Central (open-access biomedical) via NCBI E-utilities.

    Returns list of {url, title, snippet, domain, doi} with PMC ARTICLE URLs.
    The pmc.ncbi.nlm.nih.gov/articles/PMC<id>/ landing page serves a JS
    interstitial when scraped — source_fetcher detects this URL pattern and
    routes through the efetch XML API to get parseable full text.

    Free API, no key required (default rate is 3 req/sec without key).
    """
    # Step 1: esearch to get PMC IDs
    esearch_params = {
        "db": "pmc",
        "term": query + " AND open access[filter]",
        "retmax": str(max_results),
        "retmode": "json",
    }
    esearch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(esearch_params)
    try:
        data = json.loads(_http_get(esearch_url, timeout=15))
        ids = data.get("esearchresult", {}).get("idlist", [])
    except Exception:
        return []
    if not ids:
        return []

    # Step 2: esummary for titles + DOI (used for Unpaywall fallback)
    esum_params = {"db": "pmc", "id": ",".join(ids), "retmode": "json"}
    esum_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urllib.parse.urlencode(esum_params)
    out = []
    try:
        data = json.loads(_http_get(esum_url, timeout=15))
        result = data.get("result", {})
        for pmc_id in ids:
            entry = result.get(pmc_id, {})
            title = (entry.get("title") or "").strip()
            # Extract DOI from articleids (used for Unpaywall enrichment)
            doi = ""
            for aid in entry.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("value", "")
                    break
            # Article landing page URL — source_fetcher will route to efetch
            article_url = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc_id}/"
            out.append({
                "url": article_url,
                "title": title[:200],
                "snippet": "",
                "domain": "pmc.ncbi.nlm.nih.gov",
                "source_kind": "pmc",
                "pmc_id": pmc_id,
                "doi": doi,
            })
    except Exception:
        return []
    time.sleep(0.4)  # be polite (3 req/sec without API key)
    return out


def fetch_pmc_efetch_text(pmc_id: str, timeout: int = 25) -> str:
    """Fetch full PMC article text via efetch XML API. Returns plain text.

    pmc_id is the numeric ID (no "PMC" prefix). Returns empty string on failure.
    """
    if not pmc_id:
        return ""
    pmc_id = str(pmc_id).replace("PMC", "").strip()
    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
           f"?db=pmc&id={urllib.parse.quote(pmc_id)}&rettype=xml&retmode=text")
    try:
        raw = _http_get(url, timeout=timeout)
    except Exception:
        return ""
    if not raw:
        return ""
    # _http_get returns bytes; decode and strip JATS XML tags
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    import re as _re
    text = _re.sub(r"<\?xml[^>]*\?>", "", raw)
    text = _re.sub(r"<!DOCTYPE[^>]*>", "", text)
    text = _re.sub(r"<[^>]+>", " ", text)
    text = _re.sub(r"\s+", " ", text).strip()
    return text


def extract_pmc_id_from_url(url: str) -> str:
    """Extract numeric PMC ID from a pmc.ncbi.nlm.nih.gov article URL.
    Returns '' if not a recognized PMC URL."""
    import re as _re
    m = _re.search(r"/(?:articles/)?PMC(\d+)", url)
    return m.group(1) if m else ""


def lookup_unpaywall(doi: str, email: str = "research@anthropic.com") -> str | None:
    """Given a DOI, return a free open-access PDF URL if Unpaywall has one,
    else None. Free API requires email parameter.
    """
    if not doi: return None
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?email={urllib.parse.quote(email)}"
    try:
        data = json.loads(_http_get(url, timeout=15))
    except Exception:
        return None
    best = data.get("best_oa_location") or {}
    return best.get("url_for_pdf") or best.get("url") or None


def search_crossref_oa(query: str, max_results: int = 10) -> list[dict]:
    """Search Crossref for works matching query, then resolve DOIs through
    Unpaywall to get free OA URLs. Returns list of {url, title, snippet,
    domain, doi, publisher} for entries that have an open-access version.

    Crossref covers all disciplines (300M+ records), giving us OA papers
    from Wiley/Elsevier/Springer/Sage/Taylor & Francis beyond arXiv/PMC.
    Both APIs are free and require no key.
    """
    params = {
        "query": query,
        "rows": str(max(max_results * 4, 20)),  # over-fetch since not all have OA
        "select": "DOI,title,abstract,publisher,container-title",
        "filter": "type:journal-article",
    }
    api = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    try:
        body = _http_get(api, timeout=20)
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="ignore")
        data = json.loads(body)
    except Exception:
        return []

    items = data.get("message", {}).get("items", []) or []
    out = []
    for item in items:
        if len(out) >= max_results:
            break
        doi = (item.get("DOI") or "").strip()
        if not doi:
            continue
        oa_url = lookup_unpaywall(doi)
        if not oa_url:
            continue
        title_list = item.get("title") or []
        title = (title_list[0] if title_list else "").strip()[:200]
        publisher = (item.get("publisher") or "").strip()[:120]
        try:
            domain = urllib.parse.urlparse(oa_url).netloc.lower()
            if domain.startswith("www."):
                domain = domain[4:]
        except Exception:
            domain = ""
        out.append({
            "url": oa_url,
            "title": title,
            "snippet": "",
            "domain": domain,
            "source_kind": "crossref_unpaywall",
            "doi": doi,
            "publisher": publisher,
        })
        time.sleep(0.1)  # gentle pacing on Unpaywall (100k req/day free)
    return out


def get_arxiv_categories_for_soc(soc: str) -> list[str]:
    """Return the arXiv categories (if any) relevant to a SOC."""
    return SOC_TO_ARXIV_CATS.get(soc, [])


def is_healthcare_soc(soc: str) -> bool:
    """Whether this SOC should also pull from PubMed Central."""
    grp = soc[:2]
    return grp in {"29", "31"}


def discover_academic_urls(occupation: str, soc: str,
                           max_per_source: int = 8) -> list[dict]:
    """Returns academic URLs from arXiv and PMC for the given SOC.
    No-op for SOCs that don't map to STEM/healthcare categories.
    """
    out = []
    arxiv_cats = get_arxiv_categories_for_soc(soc)
    if arxiv_cats:
        # Search arXiv with both occupation title AND category filter
        try:
            hits = search_arxiv(occupation, categories=arxiv_cats, max_results=max_per_source)
            out.extend(hits)
        except Exception:
            pass
    if is_healthcare_soc(soc):
        try:
            hits = search_pubmed_central(occupation, max_results=max_per_source)
            out.extend(hits)
        except Exception:
            pass
    # Crossref + Unpaywall: works for ALL SOCs, surfaces OA papers from
    # major publishers (Wiley/Elsevier/Springer/Sage) that aren't in arXiv/PMC.
    try:
        hits = search_crossref_oa(occupation, max_results=max_per_source)
        # De-dup against existing URLs
        seen = {h["url"] for h in out}
        for h in hits:
            if h["url"] not in seen:
                out.append(h)
                seen.add(h["url"])
    except Exception:
        pass
    return out


# Globally-whitelisted academic source domains (always allowed regardless of per-SOC whitelist)
GLOBAL_ACADEMIC_DOMAINS = frozenset({
    "arxiv.org", "export.arxiv.org",
    "ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov",
    "plos.org", "journals.plos.org",
    "doi.org", "dx.doi.org",
    "openaire.eu", "explore.openaire.eu",
    "doaj.org",
    "core.ac.uk", "api.core.ac.uk",
    "semanticscholar.org", "api.semanticscholar.org",
    "openstax.org",
    "libretexts.org", "chem.libretexts.org", "math.libretexts.org",
    "ocw.mit.edu",
    "scholar.google.com",
    "biorxiv.org", "www.biorxiv.org",
    "medrxiv.org", "www.medrxiv.org",
    # Crossref+Unpaywall typically resolves to these OA publisher domains:
    "biomedcentral.com",  # Springer/BMC OA journals
    "frontiersin.org",
    "mdpi.com",
    "hindawi.com",
    "wiley.com",  # OA articles only via Unpaywall resolution
    "springer.com",
    "springeropen.com",
    "elsevier.com", "sciencedirect.com",
    "tandfonline.com",
    "sagepub.com", "journals.sagepub.com",
    "oup.com", "academic.oup.com",
    "nature.com",
    "europepmc.org",
})


def is_global_academic_domain(domain: str) -> bool:
    """Check if a domain should be globally whitelisted as academic."""
    if not domain: return False
    d = domain.lower().lstrip("www.")
    if d.startswith("www."): d = d[4:]
    for ad in GLOBAL_ACADEMIC_DOMAINS:
        if d == ad or d.endswith("." + ad):
            return True
    return False
