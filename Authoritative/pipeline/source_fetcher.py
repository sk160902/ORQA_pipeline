"""Fetch HTML/PDF documents — direct HTTP first, ScrapingBee fallback.

Design: NO chunking, NO page/section preservation. We grab the whole doc,
extract text (HTML or PDF), truncate to DOC_TEXT_TRUNCATE chars, and pass
the blob to the evidence-card extractor.

Also captures: source_title (HTML <title> or PDF metadata),
content_hash (sha256 of raw bytes, truncated to 16 hex chars).
"""
from __future__ import annotations
import hashlib
import io
import re
import ssl
import urllib.parse
import urllib.request
import urllib.error

import certifi

try:
    import pymupdf  # PyMuPDF for PDF text extraction
except Exception:
    pymupdf = None

from . import config

_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)
_HTML_PUB_DATE_RES = [
    re.compile(r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|datePublished|publish_date|pubdate|date|dc\.date|dc\.date\.issued)["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:article:published_time|datePublished|publish_date|pubdate|date|dc\.date|dc\.date\.issued)["\']', re.I),
    re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\']', re.I),
]
_HTML_VERSION_RES = [
    re.compile(r'<meta[^>]+(?:property|name)=["\'](?:version|edition|dc\.version)["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:version|edition|dc\.version)["\']', re.I),
]
_DATE_NORMALIZE_RE = re.compile(r"(\d{4}(?:-\d{2}(?:-\d{2})?)?)")

SSL_CTX = ssl.create_default_context(cafile=certifi.where())
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15")


def head_check(url: str, timeout: int = 4) -> bool:
    """Cheap reachability test before paying for a full fetch."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return 200 <= r.getcode() < 400
    except urllib.error.HTTPError as e:
        return e.code == 405  # some sites 405 on HEAD but accept GET
    except Exception:
        return True  # inconclusive — let downstream try


def _fetch_bytes(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read()


def fetch_direct(url: str, timeout: int = 25) -> tuple[bytes, str]:
    """Returns (raw_bytes, content_type)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read(), (r.headers.get("Content-Type") or "").lower()


# Domains that block scrapers, need premium proxy or JS rendering.
# Sending these straight to ScrapingBee with premium settings — skip direct fetch.
PAYWALLED_OR_ANTIBOT_DOMAINS = frozenset({
    # Engineering societies (anti-scraper, JS-rendered)
    "asce.org", "amplify.asce.org", "pubs.asce.org", "cedb.asce.org",
    "asme.org", "asmedigitalcollection.asme.org",
    "aiche.org", "onlinelibrary.wiley.com",
    "aiaa.org", "arc.aiaa.org",
    "ieee.org", "ieeexplore.ieee.org",
    "spe.org", "onepetro.org",
    "smenet.org",
    "ans.org",
    "ashrae.org", "dl.ashrae.org",
    "aapg.org", "pubs.geoscienceworld.org",
    "geosociety.org", "rock.geosociety.org",
    # Standards bodies
    "astm.org", "dl.astm.org", "store.astm.org",
    "ansi.org", "webstore.ansi.org",
    "iso.org",
    "nfpa.org", "catalog.nfpa.org",
    # Healthcare bodies
    "usp.org", "online.uspnf.com", "apps.usp.org",
    "asha.org", "pubs.asha.org", "apps.asha.org",
    "aota.org", "research.aota.org",
    "apta.org",
    "aoa.org",
    "avma.org", "javma.avma.org",
    # Finance/legal/business bodies
    "aicpa-cima.com", "us.aicpa.org", "publication.aicpa-cima.com",
    "americanbar.org",
    "cfainstitute.org", "rpc.cfainstitute.org",
    "shrm.org",
    "imanet.org", "myimanetwork.imanet.org",
    "actuaries.org", "soa.org",
    # Computing societies
    "acm.org", "dl.acm.org", "queue.acm.org", "cacm.acm.org",
    "isc2.org", "media.isc2.org",
    "isaca.org",
    # State licensing boards behind cookie/JS walls
    "bpelsg.ca.gov",
})


def _is_paywalled_or_antibot(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        # Match exact or subdomain
        for d in PAYWALLED_OR_ANTIBOT_DOMAINS:
            if host == d or host.endswith("." + d):
                return True
        return False
    except Exception:
        return False


def fetch_scrapingbee(url: str, timeout: int = 90, premium: bool = False,
                     render_js: bool = False) -> tuple[bytes, str]:
    """Fetch via ScrapingBee. Set premium=True for known anti-bot sites,
    render_js=True for JS-rendered SPAs."""
    if not config.SCRAPINGBEE_KEY:
        raise RuntimeError("SCRAPINGBEE_KEY not configured")
    params = {"api_key": config.SCRAPINGBEE_KEY, "url": url,
              "render_js": "True" if render_js else "False",
              "premium_proxy": "True" if premium else "False"}
    api = "https://app.scrapingbee.com/api/v1/?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(api, timeout=timeout, context=SSL_CTX) as r:
        return r.read(), (r.headers.get("Content-Type") or "").lower()


def clean_html(raw: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", "", raw, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_pdf_text(raw_bytes: bytes) -> str:
    """Extract plain text from a PDF byte string using PyMuPDF."""
    if pymupdf is None:
        return ""
    try:
        doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
    except Exception:
        return ""
    chunks = []
    try:
        for page in doc:
            try:
                chunks.append(page.get_text("text") or "")
            except Exception:
                continue
    finally:
        doc.close()
    text = re.sub(r"\s+", " ", " ".join(chunks)).strip()
    return text


def _looks_like_pdf(url: str, content_type: str, raw: bytes) -> bool:
    if "application/pdf" in content_type:
        return True
    if url.lower().endswith(".pdf"):
        return True
    return raw[:5] == b"%PDF-"


def _extract_html_title(raw_html: str) -> str:
    m = _TITLE_RE.search(raw_html or "")
    if not m:
        return ""
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title[:200]


def _extract_pdf_title(raw_bytes: bytes) -> str:
    if pymupdf is None:
        return ""
    try:
        doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
    except Exception:
        return ""
    try:
        meta = doc.metadata or {}
        return (meta.get("title") or "").strip()[:200]
    finally:
        doc.close()


def _extract_html_publication_date(raw_html: str) -> str:
    """Return ISO-ish date string (YYYY or YYYY-MM-DD) from HTML <meta> tags.
    Returns '' if not found."""
    for r in _HTML_PUB_DATE_RES:
        m = r.search(raw_html or "")
        if not m:
            continue
        raw = m.group(1).strip()
        nm = _DATE_NORMALIZE_RE.search(raw)
        if nm:
            return nm.group(1)
        return raw[:32]
    return ""


def _extract_html_version(raw_html: str) -> str:
    for r in _HTML_VERSION_RES:
        m = r.search(raw_html or "")
        if m:
            return m.group(1).strip()[:64]
    return ""


def _extract_pdf_publication_date(raw_bytes: bytes) -> str:
    """Return YYYY or YYYY-MM-DD from PDF metadata (CreationDate / ModDate).
    PDF dates use 'D:YYYYMMDDHHmmSS+TZ' format."""
    if pymupdf is None:
        return ""
    try:
        doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
    except Exception:
        return ""
    try:
        meta = doc.metadata or {}
        for k in ("creationDate", "modDate"):
            raw = (meta.get(k) or "").strip()
            if not raw:
                continue
            # PDF format: D:YYYYMMDDHHmmSS+HH'mm'
            m = re.match(r"D:(\d{4})(\d{2})(\d{2})", raw)
            if m:
                return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            m = re.match(r"(\d{4})", raw)
            if m:
                return m.group(1)
        return ""
    finally:
        doc.close()


def _extract_pdf_version(raw_bytes: bytes) -> str:
    """PDF subject/keywords sometimes carry version string. Best-effort."""
    if pymupdf is None:
        return ""
    try:
        doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
    except Exception:
        return ""
    try:
        meta = doc.metadata or {}
        for k in ("subject", "keywords"):
            v = (meta.get(k) or "").strip()
            if not v:
                continue
            m = re.search(r"(?:version|edition|rev\.|v\.?)\s*[:#]?\s*([\w.\-/ ]{1,40})", v, re.I)
            if m:
                return m.group(1).strip()[:64]
        return ""
    finally:
        doc.close()


def fetch_and_clean(url: str) -> tuple[dict, str | None]:
    """Returns (record, error). record is empty dict on failure, otherwise:
        {text, source_title, content_hash, content_type}
    text is truncated to DOC_TEXT_TRUNCATE chars.

    Skips HEAD pre-check — many .gov sites and association pages reject HEAD
    with non-405 codes but accept GET fine.
    """
    # PMC article URLs serve a JS interstitial when fetched directly.
    # Route through efetch XML API which returns parseable full text.
    from . import academic_sources
    pmc_id = academic_sources.extract_pmc_id_from_url(url)
    if pmc_id and "ncbi.nlm.nih.gov" in url.lower():
        text = academic_sources.fetch_pmc_efetch_text(pmc_id)
        if len(text) >= config.MIN_DOC_CHARS:
            content_hash = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
            return {
                "text": text[:config.DOC_TEXT_TRUNCATE],
                "source_title": "",  # title comes from search result; efetch parsing is overkill
                "content_hash": content_hash,
                "content_type": "application/xml",
                "publication_date": "",
                "version": "",
            }, None
        return {}, f"too_short_{len(text)}_pmc_efetch"

    raw, ctype = None, ""
    # Known paywalled/anti-bot domains: skip direct fetch (it'll fail), go
    # straight to ScrapingBee with premium proxy + JS rendering.
    if _is_paywalled_or_antibot(url):
        try:
            raw, ctype = fetch_scrapingbee(url, premium=True, render_js=True)
        except Exception as e:
            # Fallback: try basic ScrapingBee, then direct
            try:
                raw, ctype = fetch_scrapingbee(url, premium=False, render_js=False)
            except Exception:
                try:
                    raw, ctype = fetch_direct(url)
                except Exception as e3:
                    return {}, f"fetch: paywalled_{type(e).__name__}/{type(e3).__name__}"
    else:
        try:
            raw, ctype = fetch_direct(url)
        except Exception as e1:
            try:
                raw, ctype = fetch_scrapingbee(url)
            except Exception as e2:
                return {}, f"fetch: {type(e1).__name__}/{type(e2).__name__}"
    if raw is None:
        return {}, "no_content"

    content_hash = hashlib.sha256(raw).hexdigest()[:16]

    pub_date, version = "", ""
    if _looks_like_pdf(url, ctype, raw):
        text = extract_pdf_text(raw)
        title = _extract_pdf_title(raw)
        pub_date = _extract_pdf_publication_date(raw)
        version = _extract_pdf_version(raw)
    else:
        try:
            html = raw.decode("utf-8", errors="ignore")
        except Exception:
            html = ""
        text = clean_html(html)
        title = _extract_html_title(html)
        pub_date = _extract_html_publication_date(html)
        version = _extract_html_version(html)

    if len(text) < config.MIN_DOC_CHARS:
        return {}, f"too_short_{len(text)}"

    return {
        "text": text[:config.DOC_TEXT_TRUNCATE],
        "source_title": title,
        "content_hash": content_hash,
        "content_type": ctype,
        "publication_date": pub_date,
        "version": version,
    }, None
