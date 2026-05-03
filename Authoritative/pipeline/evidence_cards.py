"""Evidence card extractor.

The audit-trail artifact between fetched source and generated MCQ. Each
card carries:
  - source_url, source_quote (verbatim ≤500 chars), source_backed_claim
  - scenario_seed (1-2 sentence situational frame to anchor the MCQ)
  - correct_action (the action the source supports)
  - task_context (top-N O*NET tasks for the occupation, for traceability)

A single fetched doc typically yields 1-3 cards (one per distinct fact).
"""
from __future__ import annotations
import json
import re

from . import config
from . import clients
from . import schemas
from . import source_whitelist
from . import onet_context

EXTRACT_SYSTEM = (
    "You extract source-backed evidence cards from authoritative occupational "
    "documents. Each card must be a SPECIFIC, VERIFIABLE fact, rule, "
    "threshold, procedure, or decision criterion that is directly supported "
    "by a verbatim quote from the document AND must measure PRACTICAL "
    "OCCUPATIONAL COMPETENCE — i.e., decisions or actions a worker takes "
    "while doing the job. Never invent details that aren't in the source. "
    "Reject any content that is just credentialing administrivia, occupational-"
    "description text, or labor-market statistics."
)

EXTRACT_PROMPT = """OCCUPATION: {occupation} (SOC {soc})

This occupation is also known as: {alt_titles}.
Workers in this role typically perform tasks such as:
{task_block}

DOCUMENT URL: {url}
SOURCE TIER: {tier}

You are given cleaned text from an authoritative document (government regulation, professional society guideline, certification body handbook, licensing board FAQ, or occupational guide). Extract up to {n} distinct EVIDENCE CARDS.

Each evidence card must be:
  - A SPECIFIC, ACTIONABLE fact / rule / threshold / procedure / exception (not vague advice like "use judgment" or "follow best practices")
  - Directly entailed by a verbatim ≤500-character quote from the document
  - Relevant to the day-to-day on-the-job work of someone in the occupation above
  - About PRACTICAL OCCUPATIONAL COMPETENCE — a decision or action a worker takes while doing the job
  - Materially distinct from the other cards (no rewording — return fewer cards if there's only one distinct fact)

DO NOT extract cards about (these waste budget and produce weak items):
  - Certification / licensing administrative trivia: exam time allocation, passing-score processes, application procedures, license renewal fees, exam content outline structure (these test exam administration, not occupational competence)
  - Occupational description trivia: "what does this job do" at the abstract level, generic job duties, career-entry expectations, "this occupation involves..." text
  - Labor-market facts: wages, salary ranges, employment numbers, top industries, top states, job-growth projections, percentile statistics, demographic breakdowns
  - Marketing / mission / values / vision text from association websites
  - Career-entry content (how to BECOME this occupation; we want how to DO the job)
  - Generic safety platitudes ("follow safety procedures") without a specific source-stated rule

For each card, output:
  - "source_quote": verbatim ≤500-character snippet from the document
  - "source_backed_claim": one-sentence factual claim entailed by the quote
  - "scenario_seed": a short (1-2 sentence) NEUTRAL situational frame that puts a worker in the situation where this fact applies. NO emotional language, NO first-person voice, NO Reddit-style padding. Example: "A pharmacy technician is preparing a prescription that requires reconstitution. The patient's insurance requires generic substitution where allowed."
  - "correct_action": one-sentence statement of the recommended action that the source supports
  - "limitations": jurisdiction / time period / version / scope limits the source attaches to this rule (e.g., "California only", "before 2023", "residential settings only", "Class A vehicles only"). Empty string "" if no limits apply.
  - "question_potential": one of "high" (specific threshold/rule, MCQ-friendly), "medium" (clear claim but distractors will need work), "low" (claim is broad/vague, may be hard to make a clean MCQ).

Output ONLY a JSON object (no code fences) of the shape:
{{
  "cards": [
    {{"source_quote": "...", "source_backed_claim": "...", "scenario_seed": "...", "correct_action": "...", "limitations": "...", "question_potential": "high|medium|low"}},
    ...
  ]
}}

If no extractable evidence exists, return {{"cards": []}}.

DOCUMENT TEXT:
{doc_text}
"""


def _length_aware_n(doc_chars: int) -> int:
    if doc_chars < 4000:
        return 1
    if doc_chars < 9000:
        return 2
    return 3


def extract_cards(occupation: str, soc: str, url: str, fetched: dict) -> tuple[list[schemas.EvidenceCard], str | None]:
    """Returns (cards, error). cards=[] if error or no extractable evidence.

    `fetched` is the dict returned by source_fetcher.fetch_and_clean — contains
    text, source_title, content_hash."""
    doc_text = fetched.get("text", "")
    if not doc_text:
        return [], "empty_doc"
    domain = source_whitelist.domain_of(url)
    tier = source_whitelist.tier_for_domain(domain, soc)
    publisher = source_whitelist.publisher_for_domain(domain, soc)
    title = fetched.get("source_title", "") or domain
    chash = fetched.get("content_hash", "")
    pub_date = fetched.get("publication_date", "")
    version = fetched.get("version", "")

    rec = onet_context.get(soc)
    alt = "; ".join(rec.get("alternate_titles", [])) or "(none)"
    tasks = rec.get("key_tasks", [])
    task_block = "\n".join(f"  - {t}" for t in tasks) or "  (none)"

    prompt = EXTRACT_PROMPT.format(
        occupation=occupation, soc=soc,
        alt_titles=alt, task_block=task_block,
        url=url, tier=tier,
        n=_length_aware_n(len(doc_text)),
        doc_text=doc_text,
    )
    txt, err = clients.call_openai(
        prompt, model=config.EXTRACT_MODEL,
        temperature=0.0, max_tokens=2400,
        response_format={"type": "json_object"},
        system=EXTRACT_SYSTEM,
    )
    if err:
        return [], err
    try:
        obj = json.loads(txt)
        cards_raw = obj.get("cards") if isinstance(obj, dict) else None
        if not isinstance(cards_raw, list):
            return [], "no_cards_field"
    except Exception as e:
        return [], f"parse: {e}"

    out = []
    for c in cards_raw:
        sq = (c.get("source_quote") or "").strip()
        sbc = (c.get("source_backed_claim") or "").strip()
        ss = (c.get("scenario_seed") or "").strip()
        ca = (c.get("correct_action") or "").strip()
        lim = (c.get("limitations") or "").strip()
        qp = (c.get("question_potential") or "medium").strip().lower()
        if qp not in ("high", "medium", "low"):
            qp = "medium"
        if not all([sq, sbc, ss, ca]):
            continue
        if not _quote_in_doc(sq, doc_text):
            continue
        card = schemas.EvidenceCard.make(
            onet_soc_code=soc,
            occupation_title=occupation,
            source_url=url,
            source_domain=domain,
            source_title=title,
            publisher=publisher,
            publication_date=pub_date,
            version=version,
            content_hash=chash,
            source_tier=tier,
            source_quote=sq[:500],
            source_backed_claim=sbc,
            scenario_seed=ss,
            correct_action=ca,
            limitations=lim,
            question_potential=qp,
            task_context=tasks,
        )
        out.append(card)
    return out, None


def _quote_in_doc(quote: str, doc: str) -> bool:
    """Loose membership test — collapse whitespace and check substring."""
    if not quote or not doc:
        return False
    q = re.sub(r"\s+", " ", quote).strip().lower()
    d = re.sub(r"\s+", " ", doc).lower()
    if len(q) < 30:
        # too-short quotes are noise
        return False
    # try whole quote first; fall back to first 80 chars (LLMs sometimes
    # extend quotes slightly past document boundary)
    if q in d:
        return True
    return q[:80] in d
