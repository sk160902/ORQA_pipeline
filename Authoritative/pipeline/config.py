"""Configuration constants and paths for the v2 pipeline."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = ROOT
V2_DIR = ROOT / "v2_pipeline"
V2_OUT = V2_DIR / "output"

# Reuse upstream artifacts
LEGACY_OUT = ROOT / "output"
PER_OCC_ASSOCIATIONS = LEGACY_OUT / "per_occupation_associations_pilot20.json"
ONET_TASKS_CSV = LEGACY_OUT / "onet_tasks_parsed_full.csv"
ONET_DB_DIR = ROOT / "data" / "db_29_1_text"
ONET_ALT_TITLES_TXT = ONET_DB_DIR / "Sample of Reported Titles.txt"

# API keys (read once at module load to fail fast)
# Defensive: APFS dataless files / iCloud-evicted files can stall reads for
# 30+ seconds. Retry with backoff and degrade gracefully (return "") rather
# than crashing the whole pipeline on a single key file timeout.
def _read_key(name: str, retries: int = 3, timeout_per_try: int = 5) -> str:
    import subprocess
    p = ROOT / name
    if not p.exists():
        return ""
    # First try the fast path
    try:
        return p.read_text().strip()
    except (TimeoutError, OSError):
        pass
    # Fall back to subprocess `cat` with shell-level timeout
    for attempt in range(retries):
        try:
            r = subprocess.run(
                ["cat", str(p)],
                capture_output=True, text=True, timeout=timeout_per_try,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout.strip()
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
        # backoff
        import time as _t
        _t.sleep(2 * (attempt + 1))
    # Give up — return empty so the pipeline can still run with whatever keys loaded
    return ""

OPENAI_KEYS = [k for k in [_read_key("api_key.txt")] if k]
ANTHROPIC_KEYS = [k for k in [_read_key("api_key_anthropic.txt")] if k]
GEMINI_KEYS = [k for k in [_read_key("api_key_gemini.txt"), _read_key("api_key_gemini_2.txt")] if k]
TOGETHER_KEYS = [k for k in [_read_key("api_key_together.txt")] if k]
SCRAPINGBEE_KEY = _read_key("api_key_scrapingbee.txt")

# Serper supports multiple keys for round-robin / fallback (each free tier
# gets 2,500 queries; pool them across keys to extend the budget).
SERPER_KEYS = [k for k in [
    _read_key("api_key_serper.txt"),
    _read_key("api_key_serper_2.txt"),
    _read_key("api_key_serper_3.txt"),
] if k]
# Backward-compat: keep SERPER_KEY pointing to the first available key
SERPER_KEY = SERPER_KEYS[0] if SERPER_KEYS else ""

# Pilot 20 occupations (same set as the 294 bank, for clean A/B comparison)
PILOT_20 = [
    ("Landscaping and Groundskeeping Workers", "37-3011.00"),
    ("Tree Trimmers and Pruners", "37-3013.00"),
    ("Plumbers, Pipefitters, and Steamfitters", "47-2152.00"),
    ("Heating, Air Conditioning, and Refrigeration Mechanics and Installers", "49-9021.00"),
    ("Roofers", "47-2181.00"),
    ("Phlebotomists", "31-9097.00"),
    ("Dental Hygienists", "29-1292.00"),
    ("Pharmacy Technicians", "29-2052.00"),
    ("Surgical Technologists", "29-2055.00"),
    ("Respiratory Therapists", "29-1126.00"),
    ("Bookkeeping, Accounting, and Auditing Clerks", "43-3031.00"),
    ("Tax Preparers", "13-2082.00"),
    ("Human Resources Specialists", "13-1071.00"),
    ("Paralegals and Legal Assistants", "23-2011.00"),
    ("Loan Officers", "13-2072.00"),
    ("Hairdressers, Hairstylists, and Cosmetologists", "39-5012.00"),
    ("Massage Therapists", "31-9011.00"),
    ("Childcare Workers", "39-9011.00"),
    ("Retail Salespersons", "41-2031.00"),
    ("Janitors and Cleaners, Except Maids and Housekeeping Cleaners", "37-2011.00"),
]

# Generation targets
TARGET_ITEMS_PER_OCCUPATION = 20
MAX_DISCOVERY_QUERIES_PER_OCCUPATION = 30  # Serper queries
MAX_DOCS_FETCHED_PER_OCCUPATION = 25
# Quality failures (extract LLM error) — content-side budget
MAX_FAILURES_PER_OCCUPATION = 12
# Infrastructure failures (HTTP fetch errors, blocked sources) — separate
# budget so a Cloudflare-blocked source domain doesn't kill an otherwise
# strong-ecosystem occupation
MAX_FETCH_FAILURES_PER_OCCUPATION = 35

# Per-occupation cross-cutting baseline (only when topic genuinely is safety/labor)
GLOBAL_BASELINE_DOMAINS = {"bls.gov", "osha.gov", "cdc.gov"}

# Document handling
MIN_DOC_CHARS = 1500
DOC_TEXT_TRUNCATE = 14000  # characters fed to extractor (no chunking — occupation-level)

# Verification
VERIFICATION_MODEL = "claude-haiku-4-5"  # cheap, fast, separate rate pool from generator
DUP_JACCARD_THRESHOLD = 0.6

# Generator models (call config maps stages → providers)
EXTRACT_MODEL = "gpt-4o"
BUILD_MODEL = "gpt-4o"
DISCOVERY_MODEL = "gpt-4o-mini"  # only used for any LLM calls during discovery (mostly Serper)

# O*NET context per discovery prompt
ONET_MAX_ALT_TITLES = 6
ONET_MAX_TASKS = 5
