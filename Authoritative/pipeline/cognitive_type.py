"""Cognitive type tagging for accepted items.

One cheap LLM call per accepted item. Classifies the kind of reasoning the
question tests. Used for paper diagnostic tables (e.g., "models do well on
threshold_recall but poorly on applied_rule_reasoning").

Routed to Together Llama 3.3 70B (separate rate pool from generator).

Categories (fixed set):
  applied_rule_reasoning   — apply a documented rule to a specific scenario
  threshold_recall         — recall a specific number, dose, time, hours, percentage
  procedure_ordering       — what action to take in what order
  definition_recall        — what does a term/concept mean
  exception_handling       — when does the rule NOT apply / what is the exception
  scope_jurisdiction       — does this rule apply here (state/setting/scope)
  calculation              — compute something from given inputs
  tool_or_equipment        — pick the right tool / equipment / software
  safety_action            — what to do for worker/patient/client safety
  documentation_reporting  — what to record / report / file
  other                    — does not fit any of the above
"""
from __future__ import annotations
import re

from . import clients
from . import schemas

CATEGORIES = [
    "applied_rule_reasoning",
    "threshold_recall",
    "procedure_ordering",
    "definition_recall",
    "exception_handling",
    "scope_jurisdiction",
    "calculation",
    "tool_or_equipment",
    "safety_action",
    "documentation_reporting",
    "other",
]

PROMPT = """Classify the cognitive type of this multiple-choice question into EXACTLY ONE of:

  applied_rule_reasoning  - apply a documented rule to a specific scenario
  threshold_recall        - recall a specific number, dose, time, hours, percentage
  procedure_ordering      - what action to take in what order
  definition_recall       - what does a term/concept mean
  exception_handling      - when does the rule NOT apply / what is the exception
  scope_jurisdiction      - does this rule apply here (state/setting/scope)
  calculation             - compute something from given inputs
  tool_or_equipment       - pick the right tool / equipment / software
  safety_action           - what to do for worker/patient/client safety
  documentation_reporting - what to record / report / file
  other                   - none of the above

QUESTION:
{question}

CORRECT ANSWER:
{correct_text}

Reply with ONLY the category name (one word with underscores). No explanation, no quotes, no other text.
"""


def classify(item: schemas.Item) -> str:
    """Returns one of CATEGORIES. Defaults to 'other' on failure."""
    correct_text = item.options.get(item.correct_answer, "")
    prompt = PROMPT.format(question=item.question, correct_text=correct_text)
    txt, err = clients.call_together(
        prompt, temperature=0.0, max_tokens=20,
    )
    if err or not txt:
        return "other"
    # Extract the category token (be robust to extra whitespace/punctuation)
    cleaned = re.sub(r"[^a-z_]", "", txt.lower().strip())
    if cleaned in CATEGORIES:
        return cleaned
    # Try matching as a substring or prefix
    for cat in CATEGORIES:
        if cleaned.startswith(cat) or cat in cleaned:
            return cat
    return "other"
