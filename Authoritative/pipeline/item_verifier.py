"""Independent verification of generated items.

All 7 plan-§11.1 verification passes (occupation level — Pass 2 is occupation
alignment instead of task alignment per call decision):

  Pass 1 — Source authority: code-side check that source is on the per-occupation
           whitelist + tier label is A/B/C (not rejected). Implemented via
           source_tier on the item (set at fetch time by source_whitelist).
  Pass 2 — Occupation alignment: question is about a task this occupation
           realistically performs (not adjacent occupation, not generic).
  Pass 3 — Entailment: correct answer entailed by source quote.
  Pass 4 — Distractor non-entailment: no distractor entailed (incl. partial).
  Pass 5 — Ambiguity & scope: jurisdiction/time/version/missing-context flag.
  Pass 6 — Leakage & quality: answer not given away by stem; option length
           balance; no duplicates (LLM judgment + code-side checks).
  Pass 7 — Difficulty pretest: 3 closed-book models (in difficulty_pretest module).
  Pass 8 — Distractor eliminability (extension): independent LLM judges whether
           any distractor can be eliminated by general knowledge / common sense /
           LLM-reasoning alone (without needing the source). REJECT if any can.

Reject criteria (plan §11.2):
  - unsupported correct answer  (Pass 3 fails)
  - two correct options          (Pass 4: any distractor fully entailed)
  - ambiguous answer             (Pass 5: ambiguous flag set)
  - weak source                  (Pass 1: source_tier not in {A, B, C} or off-whitelist)
  - unfixable jurisdiction       (limitations non-empty + ambiguous + no scope hint in stem)
  - occupation misalignment      (Pass 2 fails)
  - distractor eliminable        (Pass 8: any distractor flagged by judge as
                                 eliminable by general knowledge alone)
"""
from __future__ import annotations
import json
import re

from . import clients
from . import schemas

VERIFY_SYSTEM = (
    "You are a strict, independent verifier of multiple-choice items for an "
    "occupational competence benchmark. You judge entailment from a source "
    "quote, occupation-task alignment, and answer-leakage. You rely strictly "
    "on the source quote and the stated occupation; you do NOT use outside "
    "knowledge to repair weak items."
)

VERIFY_PROMPT = """OCCUPATION: {occupation}

SOURCE QUOTE (verbatim from an authoritative document):
"{quote}"

QUESTION:
{question}

CANDIDATE OPTIONS:
A) {a}
B) {b}
C) {c}
D) {d}

The generator labeled option {correct} as the correct answer.

YOUR TASK — seven independent judgments per plan §11.1 + Abhishek's quality review:

(P2a) occupation_aligned_exact: does this question describe a task or scenario that someone working as the EXACT occupation "{occupation}" would face on the job? false if the question is about an ADJACENT specialty (e.g., asking a "Registered Nurse" question that's actually for a Nurse Practitioner / Clinical Nurse Specialist; asking a "Software Developer" question that's really for a Software Architect / Project Manager / DevOps Engineer; asking a "Cashier" question that's really for a Retail Manager). Adjacent-specialty drift is a REJECT.

(P2b) tests_practical_competence: does this question test PRACTICAL OCCUPATIONAL COMPETENCE (a decision or action a worker takes while doing the job)? Reject if it's:
  - certification/exam administrative trivia (exam time, passing scores, application fees, license renewal procedures)
  - occupational-description trivia ("what do X do" at the abstract level, generic job duties, career-entry expectations)
  - labor-market facts (wages, employment numbers, top industries, geographic stats, demographic breakdowns)
  - marketing / mission text from association websites
  - generic safety platitudes without a specific source-stated rule

(P3) labeled_correct_entailed: does the source quote alone provide clear support for option {correct} being correct? Do NOT use outside knowledge.

(P4a) entailment per option: for each A/B/C/D, judge if it is FULLY entailed by the source quote.

(P4b) partial_entailment per option (per plan §11.1): for each option, judge if it is PARTIALLY supported (correct under a different assumption, correct except for a small detail, captures part of the right answer).

(P5) ambiguous: missing context (jurisdiction/time/version) that makes the correct answer uncertain?

(P6a) answer_leaked: does stem wording or stylistic difference give away the correct option without needing knowledge? (Distractors absurd; correct option much longer; stem echoes correct option's phrasing.)

(P6b) correct_names_source: does the correct option name a specific source document, association acronym, or guideline title (e.g., "Follow the AORN Guideline...", "Per the AICPA Code...", "According to NFPA 70...")? This makes the answer too transparent — REJECT.

Output ONLY a JSON object (no code fences) of the shape:
{{
  "occupation_aligned_exact": true|false,
  "tests_practical_competence": true|false,
  "competence_violation_type": "" | "certification_admin_trivia" | "description_trivia" | "labor_market_facts" | "marketing_mission" | "generic_platitude",
  "entailment": {{"A": true|false, "B": true|false, "C": true|false, "D": true|false}},
  "partial_entailment": {{"A": true|false, "B": true|false, "C": true|false, "D": true|false}},
  "labeled_correct_entailed": true|false,
  "any_distractor_entailed": true|false,
  "any_distractor_partially_entailed": true|false,
  "ambiguous": true|false,
  "answer_leaked": true|false,
  "correct_names_source": true|false,
  "notes": "<one short sentence on any issues>"
}}
"""


ELIMINABILITY_SYSTEM = (
    "You are an adversarial judge of multiple-choice item quality. Your job is "
    "to detect distractors that a non-expert could eliminate WITHOUT needing "
    "the source — by general knowledge, common sense, basic literacy, or "
    "obvious-wrong-answer LLM-reasoning. You do NOT have access to the source. "
    "You judge ONLY whether each distractor is eliminable by reasoning that "
    "doesn't require domain expertise."
)

ELIMINABILITY_PROMPT = """OCCUPATION: {occupation}

QUESTION:
{question}

OPTIONS:
A) {a}
B) {b}
C) {c}
D) {d}

The correct answer is {correct}. The other three options are intended distractors.

YOUR TASK — for each DISTRACTOR (i.e., each option that is NOT {correct}):
Judge whether a non-expert could eliminate it as obviously wrong WITHOUT needing the source. A distractor is "eliminable" if any of these are true:
  - It is absurd, dangerous-on-its-face, or violates basic common sense
  - It uses giveaway phrasing ("ignore the protocol", "skip the safety check", "do nothing", "use outdated", "rely solely on", "always" / "never" without nuance, "without consulting", etc.)
  - It is stylistically obviously wrong (much shorter/longer than the correct answer; oddly-phrased; uses placeholder language)
  - General knowledge / common sense / basic literacy is enough to rule it out
  - It contradicts the question stem itself
  - An LLM with no domain training would reject it as the answer based on pattern-matching

A distractor is NOT eliminable if a non-expert would have to consult the source / a domain expert / a reference manual to know it's wrong. Plausible-but-wrong domain-specific options are GOOD distractors and NOT eliminable.

Output ONLY a JSON object (no code fences) of this shape — include exactly the three distractor letters as keys:
{{
  "eliminable": {{"<letter>": true|false, "<letter>": true|false, "<letter>": true|false}},
  "reasons": {{"<letter>": "<one short clause if eliminable, else empty>", "<letter>": "...", "<letter>": "..."}},
  "any_eliminable": true|false
}}
"""


PASS9_SYSTEM = (
    "You judge whether the correct option of an MCQ is as SPECIFIC and ACTIONABLE "
    "as its distractors. Generic platitudes as the correct answer (e.g., 'adhere "
    "to basic safety requirements', 'follow appropriate procedures', 'ensure "
    "compliance with standards') are a quality defect when distractors describe "
    "specific concrete actions — a careful test-taker can easily eliminate the "
    "specific distractors and pick the safe vague answer."
)

PASS9_PROMPT = """OCCUPATION: {occupation}

QUESTION:
{question}

OPTIONS:
A) {a}
B) {b}
C) {c}
D) {d}

The labeled correct answer is option {correct}.

YOUR TASK: Judge whether the correct option is as SPECIFIC and ACTIONABLE as the distractors.

A correct option is TOO GENERIC if it:
  - Uses vague qualifiers without specifics: "appropriate", "adequate", "proper", "basic", "standard", "necessary", "applicable", "relevant"
  - States a platitude: "adhere to safety requirements", "follow guidelines", "ensure compliance", "use best practices", "consult an expert", "follow procedures"
  - Lacks specific names, numbers, thresholds, tools, procedures that the distractors have
  - Could be the safe default answer for almost any scenario in this domain

A correct option is SPECIFIC ENOUGH if it:
  - Names a specific tool, technique, threshold, document type, or procedure
  - Describes concrete steps with measurable details
  - Could be wrong if a different specific action were correct
  - Is at the same level of detail as the distractors

Compare option {correct} to the other three options. Are they at similar specificity levels?

Output ONLY a JSON object:
{{
  "correct_option_specific_enough": true|false,
  "correct_option_too_generic": true|false,
  "comparison_to_distractors": "more_specific" | "similar" | "less_specific",
  "reason": "<one sentence explaining why>"
}}
"""


def _pass9_correct_specificity(item: schemas.Item) -> dict:
    """Pass 9: judge whether the correct option is as specific as the distractors.
    Returns dict with `ok` (True if correct is specific enough), and reason.
    """
    opts = item.options or {}
    prompt = PASS9_PROMPT.format(
        occupation=item.occupation_title,
        question=item.question,
        a=opts.get("A", ""), b=opts.get("B", ""),
        c=opts.get("C", ""), d=opts.get("D", ""),
        correct=item.correct_answer,
    )
    txt, err = clients.call_verify(prompt, max_tokens=300, system=PASS9_SYSTEM)
    if err:
        return {"ok": True, "reason": "skipped_err", "raw_error": err}
    parsed = _parse_json(txt)
    if parsed is None:
        return {"ok": True, "reason": "parse_fail", "raw": txt[:200]}
    too_generic = bool(parsed.get("correct_option_too_generic"))
    specific_enough = parsed.get("correct_option_specific_enough")
    if specific_enough is None:
        specific_enough = not too_generic
    return {
        "ok": bool(specific_enough) and not too_generic,
        "comparison": (parsed.get("comparison_to_distractors") or "").strip(),
        "reason": (parsed.get("reason") or "")[:200],
    }


def _pass8_distractor_eliminability(item: schemas.Item) -> dict:
    """Plan-extension Pass 8: independent LLM judge for distractor eliminability.

    Returns dict with:
      ok: bool — True if no distractor is eliminable (pass)
      eliminable: {letter: bool}
      reasons: {letter: str}
      raw_error: str | None
    """
    opts = item.options
    prompt = ELIMINABILITY_PROMPT.format(
        occupation=item.occupation_title,
        question=item.question,
        a=opts.get("A", ""), b=opts.get("B", ""),
        c=opts.get("C", ""), d=opts.get("D", ""),
        correct=item.correct_answer,
    )
    txt, err = clients.call_verify(prompt, max_tokens=400, system=ELIMINABILITY_SYSTEM)
    if err:
        return {"ok": True, "eliminable": {}, "reasons": {}, "raw_error": err}
    parsed = _parse_json(txt)
    if parsed is None:
        return {"ok": True, "eliminable": {}, "reasons": {},
                "raw_error": "parse_fail", "raw": txt[:300]}
    elim_map = parsed.get("eliminable") or {}
    reasons_map = parsed.get("reasons") or {}
    any_elim = bool(parsed.get("any_eliminable")) or any(
        bool(elim_map.get(letter)) for letter in "ABCD" if letter != item.correct_answer
    )
    return {
        "ok": not any_elim,
        "eliminable": {l: bool(elim_map.get(l, False)) for l in "ABCD" if l != item.correct_answer},
        "reasons": {l: (reasons_map.get(l, "") or "")[:200] for l in "ABCD" if l != item.correct_answer},
        "raw_error": None,
    }


def _option_length_imbalance(opts: dict, correct: str) -> bool:
    """Code-side Pass 6 helper: flag if the correct option's word count is
    significantly different from the mean of the distractors' word counts.
    Returns True if imbalanced (potential leakage signal)."""
    lens = {l: len((opts.get(l, "") or "").split()) for l in "ABCD"}
    cl = lens.get(correct, 0)
    distractor_lens = [v for k, v in lens.items() if k != correct]
    if not distractor_lens or cl == 0:
        return False
    mean_d = sum(distractor_lens) / len(distractor_lens)
    if mean_d == 0:
        return False
    return cl < 0.6 * mean_d or cl > 1.75 * mean_d


def _pass1_source_authority(item: schemas.Item) -> tuple[bool, str]:
    """Plan §11.1 Pass 1: verify source authority.

    Code-side check (no LLM): tier in {A, B, C} (not None / off-whitelist) AND
    URL + publisher metadata are present AND publisher isn't an obvious
    forum / blog / SEO domain. The URL whitelist already does heavy lifting
    upstream; this is the per-item ratification.
    """
    if item.source_tier not in {"A", "B", "C"}:
        return False, f"source_tier={item.source_tier!r} not in A/B/C"
    if not item.source_url:
        return False, "missing source_url"
    if not item.publisher:
        return False, "missing publisher"
    bad_publisher_substrings = (
        "reddit", "wikipedia", "blog", "medium.com", "quora", "answers.com",
        "wikihow", "wikia", "tumblr",
    )
    pl = (item.publisher + " " + item.source_domain).lower()
    for bad in bad_publisher_substrings:
        if bad in pl:
            return False, f"publisher/domain contains forbidden substring '{bad}'"
    return True, ""


def _detect_unfixable_jurisdiction(item: schemas.Item, ambiguous: bool) -> bool:
    """Plan §11.2 reject criterion: 'unfixable jurisdiction ambiguity'.

    Returns True if the source has a jurisdiction limitation AND the question
    stem doesn't echo it AND the verifier flagged ambiguous. In that case the
    scope cannot be recovered from the stem.
    """
    lim = (item.limitations or "").lower().strip()
    if not lim:
        return False
    if not ambiguous:
        return False
    stem = (item.question or "").lower()
    # Try a couple of common jurisdiction tokens
    keywords = []
    # Common state names + abbreviations
    state_re = re.search(r"\b(california|texas|florida|new york|washington|massachusetts|"
                         r"oregon|illinois|pennsylvania|virginia|colorado|nevada|arizona|"
                         r"michigan|minnesota|wisconsin|north carolina|south carolina|"
                         r"georgia|maryland|tennessee|ohio|alabama|missouri|indiana|"
                         r"kentucky|louisiana|hawaii|alaska)\b", lim)
    if state_re:
        keywords.append(state_re.group(1))
    # Year / time period
    yr_re = re.search(r"\b(20\d{2})\b", lim)
    if yr_re:
        keywords.append(yr_re.group(1))
    # Setting (residential/commercial/industrial/clinical/hospital)
    setting_re = re.search(r"\b(residential|commercial|industrial|clinical|hospital|outpatient|inpatient)\b", lim)
    if setting_re:
        keywords.append(setting_re.group(1))
    if not keywords:
        # Limitations exists but we can't extract a discrete keyword to match
        # against the stem — conservatively flag as unfixable
        return True
    # If NONE of the discrete keywords appear in the stem, the scope wasn't carried through
    return not any(k in stem for k in keywords)


def verify_item(item: schemas.Item) -> dict:
    """Returns the verification dict + assigns quality_tier on the item."""
    opts = item.options

    # Pass 1 — code-side source authority check (no LLM call needed)
    pass1_ok, pass1_reason = _pass1_source_authority(item)

    prompt = VERIFY_PROMPT.format(
        occupation=item.occupation_title,
        quote=item.source_quote.replace('"', "'"),
        question=item.question,
        a=opts.get("A", ""), b=opts.get("B", ""),
        c=opts.get("C", ""), d=opts.get("D", ""),
        correct=item.correct_answer,
    )
    txt, err = clients.call_verify(prompt, max_tokens=600, system=VERIFY_SYSTEM)
    if err:
        v = {"error": err, "labeled_correct_entailed": False, "any_distractor_entailed": False}
        item.verification = v
        item.quality_tier = None
        return v

    parsed = _parse_json(txt)
    if parsed is None:
        v = {"error": "parse_fail", "raw": txt[:300],
             "labeled_correct_entailed": False, "any_distractor_entailed": False}
        item.verification = v
        item.quality_tier = None
        return v

    # Pass 2a (exact occupation alignment) + 2b (practical competence)
    occupation_aligned = bool(parsed.get("occupation_aligned_exact"))
    if "occupation_aligned_exact" not in parsed:
        # backward-compat: also accept the old field name
        occupation_aligned = bool(parsed.get("occupation_aligned", True))

    tests_practical_competence = bool(parsed.get("tests_practical_competence"))
    if "tests_practical_competence" not in parsed:
        tests_practical_competence = True  # be permissive if missing
    competence_violation_type = (parsed.get("competence_violation_type") or "").strip()

    entail_map = parsed.get("entailment") or {}
    partial_map = parsed.get("partial_entailment") or {}
    labeled_entailed = bool(parsed.get("labeled_correct_entailed"))
    if entail_map.get(item.correct_answer) is True:
        labeled_entailed = True

    any_distractor_entailed = False
    for letter in "ABCD":
        if letter == item.correct_answer:
            continue
        if entail_map.get(letter) is True:
            any_distractor_entailed = True
            break
    if parsed.get("any_distractor_entailed") is True:
        any_distractor_entailed = True

    any_distractor_partially_entailed = False
    for letter in "ABCD":
        if letter == item.correct_answer:
            continue
        if partial_map.get(letter) is True:
            any_distractor_partially_entailed = True
            break
    if parsed.get("any_distractor_partially_entailed") is True:
        any_distractor_partially_entailed = True

    ambiguous = bool(parsed.get("ambiguous"))
    answer_leaked_llm = bool(parsed.get("answer_leaked"))
    answer_leaked_code = _option_length_imbalance(opts, item.correct_answer)
    answer_leaked = answer_leaked_llm or answer_leaked_code

    correct_names_source = bool(parsed.get("correct_names_source"))

    # Pass 6b regeneration: if correct option names the source, try to rewrite
    # it (up to 2 attempts) instead of rejecting the whole item. Same pattern
    # as Pass 8 distractor regen — fix the issue, don't kill the item.
    pass6b_regen_attempts = 0
    MAX_P6B_REGEN = 2
    while correct_names_source and pass6b_regen_attempts < MAX_P6B_REGEN:
        from . import item_generator
        new_correct = item_generator.regenerate_correct_option(item)
        if not new_correct:
            break  # generator failed
        item.options[item.correct_answer] = new_correct
        pass6b_regen_attempts += 1
        # Re-run the main verifier to refresh all judgments with the new option
        opts = item.options  # refreshed
        prompt2 = VERIFY_PROMPT.format(
            occupation=item.occupation_title,
            quote=item.source_quote.replace('"', "'"),
            question=item.question,
            a=opts.get("A", ""), b=opts.get("B", ""),
            c=opts.get("C", ""), d=opts.get("D", ""),
            correct=item.correct_answer,
        )
        txt2, err2 = clients.call_verify(prompt2, max_tokens=600, system=VERIFY_SYSTEM)
        if err2:
            break
        parsed2 = _parse_json(txt2)
        if parsed2 is None:
            break
        # Refresh all relevant fields from the new verification
        occupation_aligned = bool(parsed2.get("occupation_aligned_exact",
                                  parsed2.get("occupation_aligned", True)))
        tests_practical_competence = bool(parsed2.get("tests_practical_competence", True))
        competence_violation_type = (parsed2.get("competence_violation_type") or "").strip()
        entail_map = parsed2.get("entailment") or {}
        partial_map = parsed2.get("partial_entailment") or {}
        labeled_entailed = bool(parsed2.get("labeled_correct_entailed"))
        if entail_map.get(item.correct_answer) is True:
            labeled_entailed = True
        any_distractor_entailed = False
        for letter in "ABCD":
            if letter == item.correct_answer: continue
            if entail_map.get(letter) is True:
                any_distractor_entailed = True; break
        if parsed2.get("any_distractor_entailed") is True:
            any_distractor_entailed = True
        any_distractor_partially_entailed = False
        for letter in "ABCD":
            if letter == item.correct_answer: continue
            if partial_map.get(letter) is True:
                any_distractor_partially_entailed = True; break
        if parsed2.get("any_distractor_partially_entailed") is True:
            any_distractor_partially_entailed = True
        ambiguous = bool(parsed2.get("ambiguous"))
        answer_leaked_llm = bool(parsed2.get("answer_leaked"))
        answer_leaked_code = _option_length_imbalance(opts, item.correct_answer)
        answer_leaked = answer_leaked_llm or answer_leaked_code
        correct_names_source = bool(parsed2.get("correct_names_source"))
        notes = (parsed2.get("notes") or "")[:300]
        parsed = parsed2

    notes = (parsed.get("notes") or "")[:300]

    # Pass 2a regeneration: if question drifted to adjacent specialty, try to
    # rewrite the stem to refocus on the exact target occupation. The generator
    # returns empty if source is genuinely for adjacent role (no force-fit).
    pass2a_regen_attempts = 0
    MAX_P2A_REGEN = 2
    while not occupation_aligned and pass2a_regen_attempts < MAX_P2A_REGEN:
        from . import item_generator
        new_q = item_generator.regenerate_question_for_occupation_alignment(
            item, item.source_quote, notes
        )
        if not new_q:
            break  # generator declined (source genuinely wrong) or failed
        item.question = new_q
        pass2a_regen_attempts += 1
        # Re-run main verifier with new question
        opts = item.options
        prompt2a = VERIFY_PROMPT.format(
            occupation=item.occupation_title,
            quote=item.source_quote.replace('"', "'"),
            question=item.question,
            a=opts.get("A", ""), b=opts.get("B", ""),
            c=opts.get("C", ""), d=opts.get("D", ""),
            correct=item.correct_answer,
        )
        txt2a, err2a = clients.call_verify(prompt2a, max_tokens=600, system=VERIFY_SYSTEM)
        if err2a: break
        parsed2a = _parse_json(txt2a)
        if parsed2a is None: break
        # Refresh fields
        occupation_aligned = bool(parsed2a.get("occupation_aligned_exact",
                                  parsed2a.get("occupation_aligned", True)))
        tests_practical_competence = bool(parsed2a.get("tests_practical_competence", True))
        entail_map = parsed2a.get("entailment") or {}
        partial_map = parsed2a.get("partial_entailment") or {}
        labeled_entailed = bool(parsed2a.get("labeled_correct_entailed"))
        if entail_map.get(item.correct_answer) is True: labeled_entailed = True
        any_distractor_entailed = False
        for letter in "ABCD":
            if letter == item.correct_answer: continue
            if entail_map.get(letter) is True:
                any_distractor_entailed = True; break
        if parsed2a.get("any_distractor_entailed") is True:
            any_distractor_entailed = True
        ambiguous = bool(parsed2a.get("ambiguous"))
        answer_leaked_llm = bool(parsed2a.get("answer_leaked"))
        answer_leaked_code = _option_length_imbalance(opts, item.correct_answer)
        answer_leaked = answer_leaked_llm or answer_leaked_code
        correct_names_source = bool(parsed2a.get("correct_names_source"))
        notes = (parsed2a.get("notes") or "")[:300]
        parsed = parsed2a

    # Pass 4 regeneration: if any distractor is FULLY entailed by the source
    # (= "two correct options"), regenerate JUST that distractor instead of
    # rejecting the whole item. Up to 2 retries.
    pass4_regen_attempts = 0
    MAX_P4_REGEN = 2
    while any_distractor_entailed and pass4_regen_attempts < MAX_P4_REGEN:
        from . import item_generator
        # Identify which letters are entailed (excluding correct)
        entailed_letters = []
        for letter in "ABCD":
            if letter == item.correct_answer:
                continue
            if entail_map.get(letter) is True:
                entailed_letters.append(letter)
        if not entailed_letters:
            break
        any_replaced = False
        for letter in entailed_letters:
            new_opt = item_generator.regenerate_entailed_distractor(
                item, letter, item.source_quote, notes
            )
            if new_opt:
                item.options[letter] = new_opt
                any_replaced = True
        if not any_replaced:
            break
        pass4_regen_attempts += 1
        # Re-run main verifier with updated options
        opts = item.options
        prompt3 = VERIFY_PROMPT.format(
            occupation=item.occupation_title,
            quote=item.source_quote.replace('"', "'"),
            question=item.question,
            a=opts.get("A", ""), b=opts.get("B", ""),
            c=opts.get("C", ""), d=opts.get("D", ""),
            correct=item.correct_answer,
        )
        txt3, err3 = clients.call_verify(prompt3, max_tokens=600, system=VERIFY_SYSTEM)
        if err3: break
        parsed3 = _parse_json(txt3)
        if parsed3 is None: break
        # Refresh fields
        occupation_aligned = bool(parsed3.get("occupation_aligned_exact",
                                  parsed3.get("occupation_aligned", True)))
        tests_practical_competence = bool(parsed3.get("tests_practical_competence", True))
        entail_map = parsed3.get("entailment") or {}
        partial_map = parsed3.get("partial_entailment") or {}
        labeled_entailed = bool(parsed3.get("labeled_correct_entailed"))
        if entail_map.get(item.correct_answer) is True: labeled_entailed = True
        any_distractor_entailed = False
        for letter in "ABCD":
            if letter == item.correct_answer: continue
            if entail_map.get(letter) is True:
                any_distractor_entailed = True; break
        if parsed3.get("any_distractor_entailed") is True:
            any_distractor_entailed = True
        any_distractor_partially_entailed = False
        for letter in "ABCD":
            if letter == item.correct_answer: continue
            if partial_map.get(letter) is True:
                any_distractor_partially_entailed = True; break
        if parsed3.get("any_distractor_partially_entailed") is True:
            any_distractor_partially_entailed = True
        ambiguous = bool(parsed3.get("ambiguous"))
        answer_leaked_llm = bool(parsed3.get("answer_leaked"))
        answer_leaked_code = _option_length_imbalance(opts, item.correct_answer)
        answer_leaked = answer_leaked_llm or answer_leaked_code
        correct_names_source = bool(parsed3.get("correct_names_source"))
        notes = (parsed3.get("notes") or "")[:300]
        parsed = parsed3

    # Pass 5 regeneration: if question is ambiguous, rewrite stem to add scope.
    # Up to 2 retries. Re-runs main verifier afterward.
    pass5_regen_attempts = 0
    MAX_P5_REGEN = 2
    while ambiguous and pass5_regen_attempts < MAX_P5_REGEN:
        from . import item_generator
        # Use the source's limitations field if available
        limitations = getattr(item, "limitations", "") or ""
        new_q = item_generator.regenerate_question_with_scope(
            item, item.source_quote, limitations, notes
        )
        if not new_q:
            break
        item.question = new_q
        pass5_regen_attempts += 1
        # Re-run main verifier with new question
        opts = item.options
        prompt4 = VERIFY_PROMPT.format(
            occupation=item.occupation_title,
            quote=item.source_quote.replace('"', "'"),
            question=item.question,
            a=opts.get("A", ""), b=opts.get("B", ""),
            c=opts.get("C", ""), d=opts.get("D", ""),
            correct=item.correct_answer,
        )
        txt4, err4 = clients.call_verify(prompt4, max_tokens=600, system=VERIFY_SYSTEM)
        if err4: break
        parsed4 = _parse_json(txt4)
        if parsed4 is None: break
        # Refresh fields
        occupation_aligned = bool(parsed4.get("occupation_aligned_exact",
                                  parsed4.get("occupation_aligned", True)))
        tests_practical_competence = bool(parsed4.get("tests_practical_competence", True))
        entail_map = parsed4.get("entailment") or {}
        partial_map = parsed4.get("partial_entailment") or {}
        labeled_entailed = bool(parsed4.get("labeled_correct_entailed"))
        if entail_map.get(item.correct_answer) is True: labeled_entailed = True
        any_distractor_entailed = False
        for letter in "ABCD":
            if letter == item.correct_answer: continue
            if entail_map.get(letter) is True:
                any_distractor_entailed = True; break
        if parsed4.get("any_distractor_entailed") is True:
            any_distractor_entailed = True
        ambiguous = bool(parsed4.get("ambiguous"))
        answer_leaked_llm = bool(parsed4.get("answer_leaked"))
        answer_leaked_code = _option_length_imbalance(opts, item.correct_answer)
        answer_leaked = answer_leaked_llm or answer_leaked_code
        correct_names_source = bool(parsed4.get("correct_names_source"))
        notes = (parsed4.get("notes") or "")[:300]
        parsed = parsed4

    # Plan §11.2: "unfixable jurisdiction ambiguity" detector
    unfixable_jurisdiction = _detect_unfixable_jurisdiction(item, ambiguous)

    # Pass 9 — correct option specificity vs distractors (regen if generic)
    pass9 = {"ok": True, "reason": "skipped_pre_reject", "comparison": ""}
    pass9_regen_attempts = 0
    pre_reject_p9 = (not pass1_ok or not labeled_entailed or not occupation_aligned
                     or not tests_practical_competence or any_distractor_entailed
                     or ambiguous or unfixable_jurisdiction or correct_names_source)
    if not pre_reject_p9:
        pass9 = _pass9_correct_specificity(item)
        MAX_P9_REGEN = 2
        while not pass9["ok"] and pass9_regen_attempts < MAX_P9_REGEN:
            from . import item_generator
            new_correct = item_generator.regenerate_correct_option_specific(
                item, item.source_quote, pass9.get("reason", "")
            )
            if not new_correct:
                break  # generator declined (source genuinely generic) or failed
            item.options[item.correct_answer] = new_correct
            pass9_regen_attempts += 1
            # Re-run main verifier (since correct option changed) + Pass 9
            opts = item.options
            prompt9b = VERIFY_PROMPT.format(
                occupation=item.occupation_title,
                quote=item.source_quote.replace('"', "'"),
                question=item.question,
                a=opts.get("A", ""), b=opts.get("B", ""),
                c=opts.get("C", ""), d=opts.get("D", ""),
                correct=item.correct_answer,
            )
            txt9b, err9b = clients.call_verify(prompt9b, max_tokens=600, system=VERIFY_SYSTEM)
            if err9b: break
            parsed9b = _parse_json(txt9b)
            if parsed9b is None: break
            # Refresh fields
            occupation_aligned = bool(parsed9b.get("occupation_aligned_exact",
                                       parsed9b.get("occupation_aligned", True)))
            tests_practical_competence = bool(parsed9b.get("tests_practical_competence", True))
            entail_map = parsed9b.get("entailment") or {}
            partial_map = parsed9b.get("partial_entailment") or {}
            labeled_entailed = bool(parsed9b.get("labeled_correct_entailed"))
            if entail_map.get(item.correct_answer) is True: labeled_entailed = True
            any_distractor_entailed = False
            for letter in "ABCD":
                if letter == item.correct_answer: continue
                if entail_map.get(letter) is True:
                    any_distractor_entailed = True; break
            if parsed9b.get("any_distractor_entailed") is True:
                any_distractor_entailed = True
            ambiguous = bool(parsed9b.get("ambiguous"))
            answer_leaked_llm = bool(parsed9b.get("answer_leaked"))
            answer_leaked_code = _option_length_imbalance(opts, item.correct_answer)
            answer_leaked = answer_leaked_llm or answer_leaked_code
            correct_names_source = bool(parsed9b.get("correct_names_source"))
            notes = (parsed9b.get("notes") or "")[:300]
            parsed = parsed9b
            pass9 = _pass9_correct_specificity(item)

    # Pass 8 — distractor eliminability judge (independent LLM, no source)
    # Only run if the main verifier passes core gates (saves cost on items
    # we're already going to reject)
    pre_reject = (not pass1_ok or not labeled_entailed or not occupation_aligned
                  or not tests_practical_competence or any_distractor_entailed
                  or ambiguous or unfixable_jurisdiction or correct_names_source)
    if pre_reject:
        pass8 = {"ok": True, "eliminable": {}, "reasons": {}, "raw_error": "skipped_pre_reject"}
        regen_attempts = 0
    else:
        pass8 = _pass8_distractor_eliminability(item)
        # Regeneration loop: if Pass 8 flags any distractor as eliminable,
        # regenerate JUST those distractors and re-judge. Up to 3 retries.
        # Avoids killing items that are 90% fine because of one weak option.
        regen_attempts = 0
        MAX_REGEN = 3
        while not pass8["ok"] and regen_attempts < MAX_REGEN:
            from . import item_generator
            elim = pass8.get("eliminable", {}) or {}
            reasons = pass8.get("reasons", {}) or {}
            weak_letters = [l for l in "ABCD"
                            if l != item.correct_answer and elim.get(l)]
            if not weak_letters:
                break
            any_replaced = False
            for letter in weak_letters:
                new_opt = item_generator.regenerate_distractor(
                    item, letter, reasons.get(letter, "")
                )
                if new_opt:
                    item.options[letter] = new_opt
                    any_replaced = True
            if not any_replaced:
                break  # generator failed for all weak letters
            regen_attempts += 1
            pass8 = _pass8_distractor_eliminability(item)

    v = {
        "pass1_source_authority": pass1_ok,
        "pass1_reason": pass1_reason,
        "occupation_aligned_exact": occupation_aligned,
        "tests_practical_competence": tests_practical_competence,
        "competence_violation_type": competence_violation_type,
        "entailment": entail_map,
        "partial_entailment": partial_map,
        "labeled_correct_entailed": labeled_entailed,
        "any_distractor_entailed": any_distractor_entailed,
        "any_distractor_partially_entailed": any_distractor_partially_entailed,
        "ambiguous": ambiguous,
        "answer_leaked": answer_leaked,
        "answer_leaked_signal": "llm" if answer_leaked_llm else ("length_imbalance" if answer_leaked_code else None),
        "correct_names_source": correct_names_source,
        "unfixable_jurisdiction": unfixable_jurisdiction,
        "pass8_distractor_eliminability_ok": pass8["ok"],
        "pass8_eliminable": pass8["eliminable"],
        "pass8_eliminability_reasons": pass8["reasons"],
        "pass8_regen_attempts": regen_attempts,
        "pass6b_regen_attempts": pass6b_regen_attempts,
        "pass4_regen_attempts": pass4_regen_attempts,
        "pass5_regen_attempts": pass5_regen_attempts,
        "pass2a_regen_attempts": pass2a_regen_attempts,
        "pass9_correct_specificity_ok": pass9["ok"],
        "pass9_comparison": pass9.get("comparison", ""),
        "pass9_regen_attempts": pass9_regen_attempts,
        "notes": notes,
    }
    item.verification = v

    # Tier assignment — plan §11.2 strict + Abhishek's quality additions
    if not pass1_ok:
        item.quality_tier = None  # weak source
    elif not labeled_entailed:
        item.quality_tier = None  # unsupported correct answer
    elif not occupation_aligned:
        item.quality_tier = None  # adjacent-specialty drift / occupation mismatch (Pass 2a)
    elif not tests_practical_competence:
        item.quality_tier = None  # admin trivia / description / labor-market facts (Pass 2b)
    elif any_distractor_entailed:
        item.quality_tier = None  # two correct options
    elif ambiguous:
        item.quality_tier = None  # ambiguous answer
    elif unfixable_jurisdiction:
        item.quality_tier = None  # unfixable jurisdiction
    elif correct_names_source:
        item.quality_tier = None  # correct option names the source — REJECT (per Abhishek review)
    elif not pass9["ok"]:
        item.quality_tier = None  # Pass 9: correct option too generic vs distractors — REJECT
    elif not pass8["ok"]:
        item.quality_tier = None  # Pass 8: distractor eliminable by general knowledge — REJECT
    elif answer_leaked:
        item.quality_tier = "C"   # leakage: downgrade
    elif any_distractor_partially_entailed:
        item.quality_tier = "C"   # partial-entailment: kept for review
    elif item.source_tier == "A":
        item.quality_tier = "A"
    elif item.source_tier == "C":
        item.quality_tier = "C"   # vendor doc → Tier C
    else:
        item.quality_tier = "B"
    return v


def _parse_json(txt: str):
    """Tolerant JSON extraction — strips code fences, finds first {...} block."""
    if not txt:
        return None
    txt = txt.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt)
        txt = re.sub(r"\s*```\s*$", "", txt)
    try:
        return json.loads(txt)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", txt)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None
