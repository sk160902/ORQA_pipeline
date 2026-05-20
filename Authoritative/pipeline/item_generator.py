"""MCQ generator — neutral scenario style, harder distractors.

  - Scenario-anchored, NOT first-person Reddit forum style.
  - Distractors must be HARD — no giveaway phrases (banned list below).
  - Correct option must NOT name the source document.
  - Question must test PRACTICAL OCCUPATIONAL COMPETENCE (not admin trivia,
    description trivia, or labor-market facts).
  - Question is for the EXACT occupation, no adjacent-specialty drift.
  - Correct-answer slot is randomized to break gpt-4o's positional A-bias.
"""
from __future__ import annotations
import hashlib
import json
import random
import re

from . import config
from . import clients
from . import schemas

# Code-side lint: regexes that catch the giveaway phrases banned in the
# BUILD_PROMPT. If any distractor matches, the item is rejected.
# Catches variants of "focus on X exclusively" / "disregard" / "skip + open
# noun" / "solely + without" / "rely on outdated" patterns.
_GIVEAWAY_DISTRACTOR_RES = [
    # ignore [noun] — broadened beyond fixed noun list
    re.compile(r"\bignore (?:the |this |all )?\w+\b", re.I),
    # outdated practices/methods/etc.
    re.compile(r"\bouts?dated (?:practice|method|procedure|guideline|approach|standard|technique|protocol)\b", re.I),
    re.compile(r"\b(?:rely (?:solely )?on|use (?:only )?)?\s*outdated\b", re.I),
    # focus solely / only / exclusively + on  (any word order)
    re.compile(r"\bfocus(?:ing)? (?:solely|only|exclusively|primarily) on\b", re.I),
    re.compile(r"\bfocus(?:ing)? on\b[^.,]{0,40}\b(?:solely|only|exclusively|primarily)\b", re.I),
    re.compile(r"\bonly\s+(?:consider|focus|focus on|address|account for)\b", re.I),
    # disregard / dismiss / overlook
    re.compile(r"\b(?:disregard|dismiss|overlook|ignore) (?:the |any )?\w+", re.I),
    # without checking/consulting/reviewing — broadened nouns
    re.compile(r"\bwithout (?:checking|consulting|reviewing|reading|considering|notifying|verifying|documenting|following|adhering to)\b", re.I),
    # skip [activity] — broadened beyond fixed noun list
    re.compile(r"\bskip(?:ping)? (?:the |any )?\w+", re.I),
    # do nothing / take no action
    re.compile(r"\b(?:do|take) (?:nothing|no action|no further action)\b", re.I),
    # avoid [activity] — broadened
    re.compile(r"\bavoid(?:ing)? (?:the |any |all )?\w+", re.I),
    # absolutist always/never X
    re.compile(r"\b(?:always|never)\s+(?:do|use|take|require|allow|perform|skip|consult|check)\b", re.I),
    # solely / exclusively / only based on (eliminative)
    re.compile(r"\b(?:solely|exclusively|only) (?:based on|considering|relying on|on the basis of|on years of)\b", re.I),
    # without consulting / without considering — open
    re.compile(r"\bwithout (?:additional|further|prior|proper|written|formal)?\s*(?:consultation|consideration|approval|review|notification)\b", re.I),
    # immediately discard / immediately deposit / immediately X without — implies negligence
    re.compile(r"\bimmediately (?:discard|deposit|delete|remove|terminate)\b", re.I),
    # blindly / blindly trust / blindly follow
    re.compile(r"\bblindly\b", re.I),
    # pretend / fake / falsify
    re.compile(r"\b(?:pretend|fake|falsify|fabricate)\b", re.I),
]

# Code-side lint: regexes that catch "correct option names the source."
# If the correct option names a publication, association acronym, or "Per <Source>" pattern, reject.
_SOURCE_NAMING_RES = [
    re.compile(r"\b(?:follow|per|according to|in accordance with|as per|comply with|adhere to)\s+(?:the\s+)?(?:[A-Z]{2,}\s+)?(?:guideline|standard|code|manual|handbook|publication|publication\s+\d+|policy\s+\d+|protocol\s+\d+|chapter\s+\d+|section\s+\d+|article\s+\d+|rule\s+\d+|§\s*\d+)\b", re.I),
    re.compile(r"\b(?:AICPA|AORN|AANP|AACN|APNA|NFPA|ASHRAE|ANSI|IEEE|ACM|BCS|OSHA|CDC|BLS|FDA|EPA|FAA|NRC|NIH|HRSA|VA|DOL|DOT|HHS|IRS|SEC|FINRA|SHRM|ABA|NCBE)\s+(?:Guideline|Standard|Code|Manual|Handbook|Bulletin|Practice|Publication)\b"),
    re.compile(r"\bPer (?:the )?[A-Z][A-Za-z]+ (?:Guideline|Standard|Code|Manual|Handbook|Statement|Policy)\b"),
]


def _check_giveaway_distractors(opts: dict, correct: str) -> str | None:
    """Returns failure-reason string if any distractor matches a giveaway pattern,
    else None."""
    for letter in "ABCD":
        if letter == correct:
            continue
        opt = opts.get(letter, "")
        for r in _GIVEAWAY_DISTRACTOR_RES:
            if r.search(opt):
                return f"option_{letter}_giveaway_phrase"
    return None


def _check_correct_names_source(opts: dict, correct: str) -> str | None:
    opt = opts.get(correct, "")
    for r in _SOURCE_NAMING_RES:
        if r.search(opt):
            return "correct_option_names_source"
    return None


def _shuffle_options(options_abcd: dict, correct: str, seed_str: str) -> tuple[dict, str]:
    """Shuffle options A/B/C/D with a deterministic seed and return new
    (options, new_correct_letter). Reproducible across re-runs given the
    same evidence/question."""
    letters = ["A", "B", "C", "D"]
    seed = int(hashlib.sha1(seed_str.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    perm = letters[:]
    rng.shuffle(perm)
    # Build new mapping: new letter -> original option text
    new_options = {}
    new_correct = correct
    for i, new_letter in enumerate(letters):
        old_letter = perm[i]
        new_options[new_letter] = options_abcd[old_letter]
        if old_letter == correct:
            new_correct = new_letter
    return new_options, new_correct

BUILD_SYSTEM = (
    "You write rigorous multiple-choice items for a benchmark of occupational "
    "competence. Items must be source-grounded, NEUTRAL in tone (no first-"
    "person voice, no emotional language, no narrative padding), and have "
    "distractors that require domain knowledge to rule out — not distractors "
    "that are obviously absurd. Distractors should be the kind of "
    "plausible-but-wrong answer a smart non-expert might pick."
)

BUILD_PROMPT = """Build ONE multiple-choice item for the occupation: "{occupation}".

You are given an EVIDENCE CARD extracted from an authoritative source. Your job:
  1. Write a clear scenario-anchored question whose correct answer is supported by the card.
  2. Write four substantive options A/B/C/D where exactly one is correct.
  3. Make the three distractors HARD — see distractor design rules below.

🚫 CRITICAL — NO SOURCE CITATIONS IN ANY OPTION 🚫
The correct option MUST describe the action/rule as a STANDALONE statement. NEVER cite the
source document, association, standard, code, regulation, guideline, or publication by name.
Specifically, NEVER start an option with or contain phrases like:
  • "Per the [X]..."           • "According to [X]..."
  • "Following [X]..."         • "As required by [X]..."
  • "Under [X]..."             • "In accordance with [X]..."
  • "Pursuant to [X]..."       • "Per [X] guidelines/code/rule/standard..."
  • Any acronym citing a standard body: AICPA, ASCE, ASME, NFPA, NIST, OSHA, IEEE, NCEES, AOTA,
    APTA, ASHA, AVMA, USP, NABP, IRS Pub, Treasury Reg, FAA AC, FAR, etc.
  • Any standard ID: ASME PTC, NFPA 70, IEEE 802.x, NIST SP 800, ASTM E580, etc.
The option must read as the *action itself*, not the citation. Example:
  ❌ "Per AICPA Code of Professional Conduct, recuse from the engagement"
  ✅ "Recuse from the engagement and disclose the conflict to the client in writing"
  ❌ "Following ASME PTC documents, calibrate using the 5-point method"
  ✅ "Calibrate the instrument using a 5-point method against a NIST-traceable reference"
This rule applies to ALL FOUR options — correct AND distractors.

EVIDENCE CARD
  source_url: {url}
  source_quote: "{quote}"
  source_backed_claim: "{claim}"
  scenario_seed: "{scenario}"
  correct_action: "{action}"

QUESTION REQUIREMENTS
  - 1-3 sentences of NEUTRAL situational framing followed by a direct question.
  - DO NOT use first-person voice ("I'm a...", "My...", "I've been...")
  - DO NOT add emotional or stakes language ("worried", "frustrated", "overwhelmed")
  - DO NOT mention or reference the source document.
  - DO NOT ask about private or local company policy unless the source explicitly defines it.
  - The question MUST measure PRACTICAL OCCUPATIONAL COMPETENCE — a decision or action a {occupation} takes while doing the job. NOT certification/exam administration trivia, NOT abstract job-description trivia, NOT labor-market statistics (wages, employment, geography). If the source content is one of those types, decline to generate (return an empty/error JSON).
  - The question MUST be for the EXACT occupation "{occupation}", NOT an adjacent specialty. Examples to avoid:
      • For Registered Nurses: don't write a Nurse Practitioner / Clinical Nurse Specialist / Nurse Manager question — those are different SOC codes
      • For Software Developers: don't write a Software Architect / Project Manager / DevOps Engineer question
      • For Financial Managers: don't write a CFO / CPA / Investment Banker question
      • For Cashiers: don't write a Retail Manager / Inventory Specialist question
    If the source content is for an adjacent role, decline to generate.
  - The question should anchor in a concrete on-the-job situation a {occupation} would face.
  - 40-120 words total.

OPTION REQUIREMENTS
  - Four options A/B/C/D, each 8-40 words.
  - Exactly one option (the correct answer) is a faithful paraphrase of the correct_action.
  - Each option is in imperative or third-person descriptive voice. No first/second person pronouns. No question marks at end of an option.
  - Options E and F are appended automatically downstream as "All of the above" / "None of the above" — DO NOT produce them.
  - The CORRECT option MUST NOT name the source document, association, or guideline by name. Examples to avoid:
      • "Follow the AORN Guideline for Specimen Management..." → just describe the action: "Place specimens in a leak-proof container labeled with patient ID before transport to the lab"
      • "Per the AICPA Code of Professional Conduct, ..." → just state the rule
      • "According to NFPA 70, ..." → just state the requirement
    Naming the source makes the answer transparent and gives away the correct option — the option must describe the actual action/rule, not cite the publication.

DISTRACTOR DESIGN — distractors must be PLAUSIBLE-BUT-WRONG. Use one of these eight patterns for each distractor:
  1. Wrong threshold (e.g., "30 days" when correct is "60 days")
  2. Wrong exception (applies the rule when an exception applies, or vice versa)
  3. Reversed condition (swap "if X then A" → "if X then B")
  4. Adjacent rule from the same domain (a real rule that doesn't apply to this scenario)
  5. Outdated practice (a guideline that has been superseded)
  6. Overgeneralized advice ("always do X" when correct is "do X only when Y")
  7. Procedure that's correct for a different occupation, task, or context
  8. Incomplete action (correct first step but missing a required follow-up; or correct action but stopping before the required completion)

DISTRACTORS MUST NOT:
  - Be absurd
  - Be stylistically different from the correct answer
  - Be much longer or shorter than the correct answer
  - Be copied from unrelated source text
  - Contain unsupported but dangerous advice without clear framing
  - Be another answer entailed by the evidence span (this would make the item have "two correct options" and force a REJECT in verification)
  - Be eliminable purely by general knowledge — they should require domain knowledge to rule out
  - **Use giveaway phrases that any LLM can recognize as wrong without source knowledge.** Specifically banned phrases (a non-expert would eliminate these by elimination):
      • "ignore the [incident/issue/protocol/guideline]"
      • "use outdated practices/methods" or "rely on outdated..."
      • "focus solely on X" / "focus only on X" / "only consider X" (when the implication is monomania)
      • "without checking [guidelines/manual/policy/standard/protocol]"
      • "without consulting [an expert/the team/the supervisor/the manual]"
      • "skip the [safety check/review/verification/documentation]"
      • "always do X" / "never do X" without nuance (when the source allows exceptions)
      • "do nothing" / "take no action" (unless the source explicitly says inaction is correct)
      • "avoid [communication/documentation/safety equipment]" (when those are obviously required)
    These are LLM-recognizable wrong answers. Use real-world plausible-but-incorrect actions instead — like a different real procedure, a wrong-threshold variant, or an outdated-but-once-real practice with a specific name.

Output ONLY a JSON object (no code fences) of the shape:
{{
  "question": "<scenario + direct question, 40-120 words>",
  "options": {{"A": "...", "B": "...", "C": "...", "D": "..."}},
  "correct_answer": "<A | B | C | D>",
  "rationale": "<one-sentence explanation citing the source claim>"
}}
"""


REGEN_DISTRACTOR_SYSTEM = (
    "You generate replacement distractors for occupational MCQs. The original "
    "distractor was flagged as too easy — a non-expert could eliminate it without "
    "needing the source. Generate a NEW distractor that is plausible-but-wrong: "
    "looks like a real action a domain expert might consider, but is incorrect "
    "for THIS specific scenario."
)

REGEN_DISTRACTOR_PROMPT = """OCCUPATION: {occupation}

QUESTION:
{question}

CURRENT OPTIONS:
A) {a}
B) {b}
C) {c}
D) {d}

The correct answer is {correct}.

Option {weak_letter} was REJECTED as too easy. Reason: "{reason}"

YOUR TASK: Write a REPLACEMENT for option {weak_letter} that is:
1. PLAUSIBLE-BUT-WRONG — a real action/rule/threshold a {occupation} might genuinely consider, but incorrect for THIS scenario
2. NOT eliminable by general knowledge / common sense / basic literacy / LLM reasoning
3. Stylistically similar in length and tone to the other options
4. Use ONE of these patterns:
   - Wrong threshold (different specific number, different time period)
   - Wrong exception (apply rule when exception applies, or vice versa)
   - Reversed condition
   - Adjacent rule from the SAME domain (real authoritative rule that doesn't apply here)
   - Outdated practice (a real superseded practice with specifics)
   - Procedure correct for a DIFFERENT occupation/setting/jurisdiction
   - Incomplete action (correct first step but missing required follow-up)

DO NOT use any of these giveaway phrases:
"ignore", "outdated practices", "skip the [check/review/verification]", "do nothing", "always" / "never" without nuance, "without consulting/checking", "rely solely on", "focus solely/exclusively on", "use any nearby ... regardless"

DO NOT name a source / association / standard ID (e.g., "Per AICPA Code", "Follow NFPA 70")
DO NOT contradict the question stem itself
DO NOT match or paraphrase any of the other current options

Output ONLY a JSON object: {{"option": "<the new distractor text — single string, NO JSON nesting>"}}
"""


REGEN_CORRECT_SYSTEM = (
    "You rewrite the correct option of an MCQ to remove source citations while "
    "keeping the same underlying rule/action. The new option must describe the "
    "action as a standalone professional statement, never citing the source "
    "document or standard body."
)

REGEN_CORRECT_PROMPT = """OCCUPATION: {occupation}

QUESTION:
{question}

CURRENT OPTIONS:
A) {a}
B) {b}
C) {c}
D) {d}

The correct answer is option {correct}. It was REJECTED because it names the source
document, association, standard body, or guideline by name (e.g., "Per AICPA...",
"Following NFPA 70...", "ASME PTC documents specify...").

YOUR TASK: Rewrite option {correct} so that:
1. It expresses the SAME underlying action/rule
2. It does NOT name any standard body, code, association, regulation, guideline, or publication
3. It does NOT use phrases like "Per [X]", "According to [X]", "Following [X]", "As required by [X]", "Under [X]", "In accordance with [X]", "Pursuant to [X]"
4. It does NOT use any acronym for a standard body (AICPA, ASCE, ASME, NFPA, NIST, OSHA, IEEE, NCEES, AOTA, APTA, ASHA, AVMA, USP, NABP, IRS Pub, FAA AC, FAR, etc.)
5. It does NOT use any standard ID (ASME PTC, NFPA 70, IEEE 802.x, NIST SP 800, ASTM E580, etc.)
6. It reads as the actual action a {occupation} would take — describe what to DO, not what document to follow
7. Keep the same length (8-40 words), same imperative/descriptive voice, same level of specificity

Output ONLY a JSON object: {{"option": "<rewritten option text — single string>"}}
"""


def regenerate_correct_option(item: schemas.Item) -> str | None:
    """Rewrite the correct option to remove source citations.

    Returns the new option text, or None on error.
    """
    correct_letter = item.correct_answer
    if correct_letter not in "ABCD":
        return None
    opts = item.options or {}
    prompt = REGEN_CORRECT_PROMPT.format(
        occupation=item.occupation_title,
        question=item.question,
        a=opts.get("A", ""), b=opts.get("B", ""),
        c=opts.get("C", ""), d=opts.get("D", ""),
        correct=correct_letter,
    )
    txt, err = clients.call_openai(
        prompt, model=config.BUILD_MODEL,
        temperature=0.3, max_tokens=300,
        response_format={"type": "json_object"},
        system=REGEN_CORRECT_SYSTEM,
    )
    if err:
        return None
    try:
        obj = json.loads(txt)
    except Exception:
        return None
    new_opt = (obj.get("option") or "").strip()
    if not new_opt or len(new_opt) < 8:
        return None
    # Quick check it doesn't still name a source
    lower = new_opt.lower()
    bad_phrases = ["per the ", "per aicpa", "per asme", "per nfpa", "per nist", "per osha",
                   "per ieee", "per ncees", "per aota", "per apta", "per asha", "per avma",
                   "per usp", "per nabp", "per irs", "per faa", "according to ",
                   "following the ", "as required by ", "in accordance with ",
                   "pursuant to ", "under the "]
    for ph in bad_phrases:
        if ph in lower:
            return None
    return new_opt


REGEN_ENTAILED_SYSTEM = (
    "You rewrite a distractor that is too close to the correct answer in an MCQ. "
    "The distractor was REJECTED because the source quote ALSO supports it, "
    "creating a 'two correct options' problem. The new distractor must be "
    "plausible-but-wrong AND clearly NOT supported by the source."
)

REGEN_ENTAILED_PROMPT = """OCCUPATION: {occupation}

SOURCE QUOTE (authoritative):
"{quote}"

QUESTION:
{question}

CURRENT OPTIONS:
A) {a}
B) {b}
C) {c}
D) {d}

The correct answer is option {correct}.

Option {entailed_letter} was REJECTED because the source quote ALSO entails/supports it,
creating a "two correct options" problem. The verifier said: "{reason}"

YOUR TASK: Write a REPLACEMENT for option {entailed_letter} that is:
1. PLAUSIBLE-BUT-WRONG — a real action a {occupation} might genuinely consider, but incorrect for THIS scenario
2. CLEARLY NOT SUPPORTED by the source quote — must be inconsistent with or unrelated to the rule the source describes
3. Use ONE of these patterns:
   - Wrong threshold (different specific number, different time period)
   - Wrong exception (apply rule when exception applies, or vice versa)
   - Reversed condition
   - Adjacent rule from same domain (real authoritative rule that doesn't apply here)
   - Outdated practice (a real superseded practice with specifics)
   - Procedure correct for a DIFFERENT occupation/setting/jurisdiction
4. NOT eliminable by general knowledge / common sense
5. Stylistically similar in length and tone to the other options (8-40 words)

DO NOT name a source / association / standard ID
DO NOT use giveaway phrases ("ignore", "outdated", "skip", "do nothing", "always" / "never")
DO NOT match or paraphrase the correct answer or any other current option
DO NOT contradict the question stem itself

Output ONLY a JSON object: {{"option": "<the new distractor text — single string>"}}
"""


def regenerate_entailed_distractor(item: schemas.Item, entailed_letter: str,
                                   source_quote: str, reason: str = "") -> str | None:
    """Regenerate a distractor that's too close to the correct answer (Pass 4 fail).

    Returns the new option text, or None on error.
    """
    if entailed_letter not in "ABCD" or entailed_letter == item.correct_answer:
        return None
    opts = item.options or {}
    prompt = REGEN_ENTAILED_PROMPT.format(
        occupation=item.occupation_title,
        quote=source_quote.replace('"', "'"),
        question=item.question,
        a=opts.get("A", ""), b=opts.get("B", ""),
        c=opts.get("C", ""), d=opts.get("D", ""),
        correct=item.correct_answer,
        entailed_letter=entailed_letter,
        reason=(reason or "two correct options")[:200],
    )
    txt, err = clients.call_openai(
        prompt, model=config.BUILD_MODEL,
        temperature=0.4, max_tokens=300,
        response_format={"type": "json_object"},
        system=REGEN_ENTAILED_SYSTEM,
    )
    if err:
        return None
    try:
        obj = json.loads(txt)
    except Exception:
        return None
    new_opt = (obj.get("option") or "").strip()
    if not new_opt or len(new_opt) < 8:
        return None
    err = _check_giveaway_distractors({entailed_letter: new_opt}, item.correct_answer)
    if err:
        return None
    return new_opt


REGEN_AMBIGUOUS_SYSTEM = (
    "You rewrite the question stem of an MCQ to remove ambiguity by adding "
    "explicit scope (jurisdiction, time period, setting, or version). The new "
    "stem must keep the same correct answer, same source-grounded action, but "
    "make the relevant context unambiguous so the answer is well-defined."
)

REGEN_AMBIGUOUS_PROMPT = """OCCUPATION: {occupation}

SOURCE QUOTE (authoritative):
"{quote}"

LIMITATIONS / SCOPE FROM EVIDENCE CARD:
"{limitations}"

CURRENT QUESTION:
{question}

OPTIONS:
A) {a}
B) {b}
C) {c}
D) {d}

The correct answer is option {correct}.

The verifier flagged this question as AMBIGUOUS — missing context (jurisdiction, time period,
version, or setting) that makes the correct answer uncertain. Reason: "{reason}"

YOUR TASK: Rewrite the question stem so that:
1. The scope/context is EXPLICIT (e.g., name the state, the setting like "in an outpatient clinic", the year/version of the rule, or the jurisdiction)
2. The correct answer remains option {correct} (do NOT change which option is correct)
3. Keep the same scenario and the same on-the-job competence focus
4. Stay within 40-120 words
5. Use NEUTRAL situational framing — no first-person voice, no emotional language
6. DO NOT mention or name the source document, association, or guideline
7. DO NOT use private company policy unless source defines it
8. The scope you add MUST be consistent with the source quote and limitations above

Output ONLY a JSON object: {{"question": "<rewritten question stem — single string>"}}
"""


def regenerate_question_with_scope(item: schemas.Item, source_quote: str,
                                   limitations: str = "", reason: str = "") -> str | None:
    """Rewrite the question stem to add explicit scope (Pass 5 fail).

    Returns the new question text, or None on error.
    """
    opts = item.options or {}
    prompt = REGEN_AMBIGUOUS_PROMPT.format(
        occupation=item.occupation_title,
        quote=source_quote.replace('"', "'"),
        limitations=(limitations or "(none specified)").replace('"', "'"),
        question=item.question,
        a=opts.get("A", ""), b=opts.get("B", ""),
        c=opts.get("C", ""), d=opts.get("D", ""),
        correct=item.correct_answer,
        reason=(reason or "missing context for jurisdiction/time/version")[:200],
    )
    txt, err = clients.call_openai(
        prompt, model=config.BUILD_MODEL,
        temperature=0.3, max_tokens=400,
        response_format={"type": "json_object"},
        system=REGEN_AMBIGUOUS_SYSTEM,
    )
    if err:
        return None
    try:
        obj = json.loads(txt)
    except Exception:
        return None
    new_q = (obj.get("question") or "").strip()
    if not new_q or len(new_q.split()) < 20:
        return None
    return new_q


REGEN_OCCUPATION_SYSTEM = (
    "You rewrite the question stem of an MCQ to refocus it on the EXACT target "
    "occupation, removing drift to adjacent specialties. Only attempt this if "
    "the source quote and correct action are genuinely applicable to the target "
    "occupation — if the source is for an adjacent role (e.g., source is for a "
    "Nurse Practitioner but target is Registered Nurse), return an empty string."
)

REGEN_OCCUPATION_PROMPT = """OCCUPATION (target): "{occupation}"

SOURCE QUOTE (authoritative):
"{quote}"

CURRENT QUESTION:
{question}

OPTIONS:
A) {a}
B) {b}
C) {c}
D) {d}

The correct answer is option {correct}.

The verifier flagged that this question is NOT about "{occupation}" specifically — it
drifted to an ADJACENT specialty. Reason: "{reason}"

Examples of drift:
  • A Registered Nurse question that's really for a Nurse Practitioner / Clinical Nurse Specialist
  • A Software Developer question that's really for a Software Architect / Project Manager / DevOps Engineer
  • A Cashier question that's really for a Retail Manager / Inventory Specialist
  • A Civil Engineer question that's really for a Construction Manager

YOUR TASK: Decide first whether the source quote is actually about a task that "{occupation}" performs on the job:

CASE A — source IS for {occupation}:
  Rewrite the question stem to clearly anchor in a task or scenario that ONLY a {occupation} would face — not the adjacent specialist. The correct answer must remain option {correct}, and all options stay the same. Just rewrite the scenario in the question stem to put a {occupation} in the driver's seat.
  Example: "A nurse practitioner..." → "A registered nurse, before the prescriber's shift, ..."
  Output: {{"question": "<rewritten stem>"}}

CASE B — source is genuinely for the adjacent role:
  Do NOT attempt to force-fit. Output: {{"question": ""}}

Constraints when rewriting (CASE A only):
- Keep the same correct answer (option {correct})
- 40-120 words total
- NEUTRAL situational framing — no first-person, no emotional language
- DO NOT mention the source document or guideline by name
- The scenario MUST be a task a {occupation} performs while doing the job

Output ONLY a JSON object: {{"question": "<rewritten stem OR empty string>"}}
"""


def regenerate_question_for_occupation_alignment(item: schemas.Item, source_quote: str,
                                                  reason: str = "") -> str | None:
    """Rewrite the question stem to refocus on the exact target occupation (Pass 2a fail).

    Returns the new question text, or None if regen didn't produce one (which can
    mean: source is genuinely for adjacent role, OR generator failed).
    """
    opts = item.options or {}
    prompt = REGEN_OCCUPATION_PROMPT.format(
        occupation=item.occupation_title,
        quote=source_quote.replace('"', "'"),
        question=item.question,
        a=opts.get("A", ""), b=opts.get("B", ""),
        c=opts.get("C", ""), d=opts.get("D", ""),
        correct=item.correct_answer,
        reason=(reason or "occupation drift to adjacent specialty")[:200],
    )
    txt, err = clients.call_openai(
        prompt, model=config.BUILD_MODEL,
        temperature=0.3, max_tokens=400,
        response_format={"type": "json_object"},
        system=REGEN_OCCUPATION_SYSTEM,
    )
    if err:
        return None
    try:
        obj = json.loads(txt)
    except Exception:
        return None
    new_q = (obj.get("question") or "").strip()
    if not new_q or len(new_q.split()) < 20:
        return None  # generator declined or produced too-short stem
    return new_q


REGEN_SPECIFIC_SYSTEM = (
    "You rewrite the correct option of an MCQ to make it as SPECIFIC and ACTIONABLE "
    "as the distractors. The original correct option was REJECTED for being a "
    "generic platitude while the distractors named specific tools, thresholds, or "
    "procedures. Your job is to find the SAME underlying action in the source quote, "
    "but state it with the same level of specificity as the distractors."
)

REGEN_SPECIFIC_PROMPT = """OCCUPATION: {occupation}

SOURCE QUOTE (authoritative):
"{quote}"

QUESTION:
{question}

CURRENT OPTIONS:
A) {a}
B) {b}
C) {c}
D) {d}

The correct answer is option {correct}, but it was REJECTED for being TOO GENERIC compared to the distractors. Reason: "{reason}"

The distractors describe SPECIFIC actions (named tools, thresholds, procedures). Option {correct} is a vague platitude ("adhere to basic safety requirements", "follow appropriate procedures", etc.).

YOUR TASK: Rewrite option {correct} so that:
1. It expresses the SAME underlying rule/action that the source quote actually supports
2. It is as SPECIFIC as the distractors — name specific tools, thresholds, procedures, document types, or measurements where the source supports them
3. It does NOT use vague qualifiers: "appropriate", "adequate", "proper", "basic", "standard", "necessary", "applicable", "relevant"
4. It does NOT state a platitude: "adhere to safety requirements", "follow guidelines", "ensure compliance", "use best practices"
5. Stay within 8-40 words, same imperative/descriptive voice
6. Do NOT name the source document, association, code, regulation, or guideline by name
7. Do NOT use phrases like "Per [X]", "According to [X]", "Following [X]"

If the source quote ITSELF is generic and doesn't support a more specific action, output an empty string {{"option": ""}} — do NOT make up specifics not in the source.

Output ONLY a JSON object: {{"option": "<rewritten specific option, or empty string>"}}
"""


def regenerate_correct_option_specific(item: schemas.Item, source_quote: str,
                                        reason: str = "") -> str | None:
    """Rewrite the correct option to be as specific as the distractors (Pass 9 fail).

    Returns the new option text, or None if regen failed or source is genuinely generic.
    """
    correct_letter = item.correct_answer
    if correct_letter not in "ABCD":
        return None
    opts = item.options or {}
    prompt = REGEN_SPECIFIC_PROMPT.format(
        occupation=item.occupation_title,
        quote=source_quote.replace('"', "'"),
        question=item.question,
        a=opts.get("A", ""), b=opts.get("B", ""),
        c=opts.get("C", ""), d=opts.get("D", ""),
        correct=correct_letter,
        reason=(reason or "correct option is too generic vs distractors")[:200],
    )
    txt, err = clients.call_openai(
        prompt, model=config.BUILD_MODEL,
        temperature=0.3, max_tokens=300,
        response_format={"type": "json_object"},
        system=REGEN_SPECIFIC_SYSTEM,
    )
    if err:
        return None
    try:
        obj = json.loads(txt)
    except Exception:
        return None
    new_opt = (obj.get("option") or "").strip()
    if not new_opt or len(new_opt) < 8:
        return None  # generator declined or too short
    # Lint: avoid lazy generic words still appearing
    lower = new_opt.lower()
    generic_phrases = ["adhere to basic", "follow appropriate", "ensure compliance with",
                       "use best practices", "consult an expert", "appropriate procedures",
                       "applicable standards", "relevant guidelines"]
    for ph in generic_phrases:
        if ph in lower:
            return None
    # Also still check source-naming
    bad_source_phrases = ["per the ", "per aicpa", "per asme", "per nfpa", "per nist",
                          "according to ", "following the ", "in accordance with ",
                          "pursuant to "]
    for ph in bad_source_phrases:
        if ph in lower:
            return None
    return new_opt


def regenerate_distractor(item: schemas.Item, weak_letter: str,
                           eliminability_reason: str = "") -> str | None:
    """Regenerate a single distractor that was flagged as eliminable.

    Returns the new option text, or None on error.
    """
    if weak_letter not in "ABCD" or weak_letter == item.correct_answer:
        return None
    opts = item.options or {}
    prompt = REGEN_DISTRACTOR_PROMPT.format(
        occupation=item.occupation_title,
        question=item.question,
        a=opts.get("A", ""), b=opts.get("B", ""),
        c=opts.get("C", ""), d=opts.get("D", ""),
        correct=item.correct_answer,
        weak_letter=weak_letter,
        reason=(eliminability_reason or "weak distractor")[:200],
    )
    txt, err = clients.call_openai(
        prompt, model=config.BUILD_MODEL,
        temperature=0.4, max_tokens=300,
        response_format={"type": "json_object"},
        system=REGEN_DISTRACTOR_SYSTEM,
    )
    if err:
        return None
    try:
        obj = json.loads(txt)
    except Exception:
        return None
    new_opt = (obj.get("option") or "").strip()
    if not new_opt or len(new_opt) < 8:
        return None
    # Check it's not a giveaway phrase
    err = _check_giveaway_distractors({weak_letter: new_opt}, item.correct_answer)
    if err:
        return None
    return new_opt


def build_item(card: schemas.EvidenceCard) -> tuple[schemas.Item | None, str | None]:
    """Generate a single MCQ from an evidence card. Returns (item, error)."""
    # Inject scope/limitations into the build prompt so the question stem
    # mentions the relevant jurisdiction/setting when it matters.
    scope_hint = ""
    if card.limitations:
        scope_hint = (
            f"\nIMPORTANT SCOPE NOTE: this rule applies under specific conditions: "
            f"\"{card.limitations}\". The question stem MUST make this scope clear "
            f"(e.g., name the state, the setting, or the time period explicitly) so "
            f"the correct answer is well-defined within the stated scope.\n"
        )
    prompt = BUILD_PROMPT.format(
        occupation=card.occupation_title,
        url=card.source_url,
        quote=card.source_quote.replace('"', "'"),
        claim=card.source_backed_claim.replace('"', "'"),
        scenario=card.scenario_seed.replace('"', "'"),
        action=card.correct_action.replace('"', "'"),
    ) + scope_hint
    txt, err = clients.call_openai(
        prompt, model=config.BUILD_MODEL,
        temperature=0.3, max_tokens=1200,
        response_format={"type": "json_object"},
        system=BUILD_SYSTEM,
    )
    if err:
        return None, err
    try:
        obj = json.loads(txt)
    except Exception as e:
        return None, f"parse: {e}"

    q = (obj.get("question") or "").strip()
    opts = obj.get("options") or {}
    ca = (obj.get("correct_answer") or "").strip().upper()
    rationale = (obj.get("rationale") or "").strip()

    if not q or not all(k in opts for k in "ABCD") or ca not in "ABCD":
        return None, "missing_or_invalid_fields"

    # Lint: pronouns, trailing question marks, duplicate options
    forbidden_pronouns = {"i", "i'm", "i've", "we", "we're", "our", "ours", "us", "my", "your"}
    seen_norm: set[str] = set()
    for letter in "ABCD":
        opt = (opts.get(letter) or "").strip()
        if not opt:
            return None, f"option_{letter}_empty"
        if opt.endswith("?"):
            return None, f"option_{letter}_trailing_question_mark"
        toks = {t.lower().strip(".,;:") for t in opt.split()}
        if toks & forbidden_pronouns:
            return None, f"option_{letter}_forbidden_pronoun"
        norm = " ".join(opt.lower().split())  # collapse whitespace + lowercase
        if norm in seen_norm:
            return None, f"option_{letter}_duplicate"
        seen_norm.add(norm)

    abcd_only = {l: opts[l].strip() for l in "ABCD"}

    # Quality lint:
    # (a) reject if any distractor uses a banned giveaway phrase
    err = _check_giveaway_distractors(abcd_only, ca)
    if err:
        return None, err
    # (b) reject if the correct option names the source document
    err = _check_correct_names_source(abcd_only, ca)
    if err:
        return None, err

    # Shuffle A-D to break gpt-4o's positional A-bias. Deterministic seed
    # tied to the evidence card + question text so the same input always
    # produces the same shuffle (audit-stable).
    seed_str = f"{card.evidence_id}|{q[:100]}"
    abcd_shuffled, ca_shuffled = _shuffle_options(abcd_only, ca, seed_str)
    full_options = dict(abcd_shuffled)
    full_options["E"] = "All of the above"
    full_options["F"] = "None of the above"
    ca = ca_shuffled

    item = schemas.Item.make(
        evidence_id=card.evidence_id,
        source_id=card.source_id,
        onet_soc_code=card.onet_soc_code,
        occupation_title=card.occupation_title,
        source_url=card.source_url,
        source_domain=card.source_domain,
        source_title=card.source_title,
        publisher=card.publisher,
        publication_date=card.publication_date,
        version=card.version,
        source_tier=card.source_tier,
        source_quote=card.source_quote,
        limitations=card.limitations,
        question=q,
        options=full_options,
        correct_answer=ca,
        rationale=rationale,
    )
    item.cognitive_type = card.cognitive_type  # mirror from card if present
    return item, None
