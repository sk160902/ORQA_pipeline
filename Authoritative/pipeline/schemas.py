"""Dataclasses for evidence cards and items.

Per Abhishek's call: evidence cards are the audit-trail artifact between
source discovery and MCQ generation. Each item points to its evidence card
via evidence_id so a reviewer can click through (item → evidence → source)
without re-reading the source document.

We are running at OCCUPATION level (not task level), per the call decision.
The task_context field carries the top-N O*NET tasks for the occupation as
flavor / discovery anchor, not as a per-item task binding.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import hashlib
import time


def _gen_id(prefix: str, payload: str) -> str:
    h = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{h}"


@dataclass
class EvidenceCard:
    """The intermediate artifact between fetched source and generated MCQ.

    A single source document typically yields 1-3 evidence cards (one per
    distinct fact). Each card is the anchor for one MCQ.
    """
    evidence_id: str
    source_id: str  # plan §9.1: stable per-source-document id (sha-derived from content_hash)
    onet_soc_code: str
    occupation_title: str
    source_url: str
    source_domain: str
    source_title: str  # parsed from HTML <title> or PDF metadata
    publisher: str  # human-readable publisher (e.g. "American Dental Hygienists' Association")
    publication_date: str  # plan §7.1: from PDF metadata or HTML <meta>; "" if unknown
    version: str  # plan §7.1: edition/version string; "" if unknown
    content_hash: str  # sha256(raw bytes)[:16] for dedup / reproducibility
    source_tier: str  # "A" (national assoc / cert board / state board / .gov), "B" (other), "C" (vendor)
    source_quote: str  # verbatim ≤500 chars from the doc
    source_backed_claim: str  # one-sentence factual claim entailed by the quote
    scenario_seed: str  # short situational frame (1-2 sentences) to anchor the MCQ
    correct_action: str  # the recommended action / answer the source supports
    limitations: str = ""  # jurisdiction / time / version / scope limits, if any (per §9.2)
    question_potential: str = "medium"  # "high" / "medium" / "low" - LLM judgment of MCQ-suitability
    cognitive_type: Optional[str] = None  # plan §9.1: applied_rule_reasoning / threshold_recall / etc.
    task_context: list[str] = field(default_factory=list)  # top-N O*NET tasks for this occupation
    fetched_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"))

    @classmethod
    def make(cls, *, onet_soc_code, occupation_title, source_url, source_domain,
             source_title, publisher, content_hash, source_tier, source_quote,
             source_backed_claim, scenario_seed, correct_action, task_context,
             limitations="", question_potential="medium",
             publication_date="", version="", cognitive_type=None):
        eid = _gen_id("ev", f"{source_url}|{source_quote[:120]}|{correct_action[:80]}")
        sid = f"src_{content_hash[:12]}" if content_hash else _gen_id("src", source_url)
        return cls(
            evidence_id=eid,
            source_id=sid,
            onet_soc_code=onet_soc_code,
            occupation_title=occupation_title,
            source_url=source_url,
            source_domain=source_domain,
            source_title=source_title,
            publisher=publisher,
            publication_date=publication_date,
            version=version,
            content_hash=content_hash,
            source_tier=source_tier,
            source_quote=source_quote,
            source_backed_claim=source_backed_claim,
            scenario_seed=scenario_seed,
            correct_action=correct_action,
            limitations=limitations,
            question_potential=question_potential,
            cognitive_type=cognitive_type,
            task_context=task_context,
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Item:
    """An accepted MCQ. Points back to its evidence card via evidence_id."""
    item_id: str
    evidence_id: str
    source_id: str  # plan §10.3: paper-roster source identifier
    onet_soc_code: str
    occupation_title: str
    source_url: str
    source_domain: str
    source_title: str
    publisher: str
    publication_date: str
    version: str
    source_tier: str
    source_quote: str
    limitations: str
    question: str
    options: dict
    correct_answer: str
    rationale: str
    verification: dict = field(default_factory=dict)
    quality_tier: Optional[str] = None
    cognitive_type: Optional[str] = None
    difficulty_estimate: Optional[str] = None
    difficulty_pretest: dict = field(default_factory=dict)
    replacement_for: Optional[str] = None
    generated_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d"))

    @classmethod
    def make(cls, *, evidence_id, source_id, onet_soc_code, occupation_title, source_url,
             source_domain, source_title, publisher, source_tier, source_quote,
             limitations, question, options, correct_answer, rationale,
             publication_date="", version=""):
        iid = _gen_id("item", f"{evidence_id}|{question[:120]}")
        return cls(
            item_id=iid,
            evidence_id=evidence_id,
            source_id=source_id,
            onet_soc_code=onet_soc_code,
            occupation_title=occupation_title,
            source_url=source_url,
            source_domain=source_domain,
            source_title=source_title,
            publisher=publisher,
            publication_date=publication_date,
            version=version,
            source_tier=source_tier,
            source_quote=source_quote,
            limitations=limitations,
            question=question,
            options=options,
            correct_answer=correct_answer,
            rationale=rationale,
        )

    def to_dict(self) -> dict:
        return asdict(self)
