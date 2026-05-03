"""
Step 74: Strip community-voice phrasing from v4 scenarios.

The questions should read as a worker asking an AI assistant for help with
a task — NOT as forum posts addressed to a community. Remove:
  - leading greetings: "Hey everyone,", "Hey team,", "Hi all,", etc.
  - community-addressed questions: "Has anyone encountered...?",
    "Does anyone know...?", "Any tips on...?", "Anyone here...?"

Preserve the actual task question ("What should I do?", "How do I...").

Output: output/qa_auto_source_v4_cleaned.json (same items, cleaned scenarios)
"""
import json, re
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"

BANK = OUT / "qa_auto_source_v4.json"
CLEANED = OUT / "qa_auto_source_v4_cleaned.json"
DIFF = OUT / "v4_cleaned_diff.txt"


GREETING_RX = re.compile(
    r"^\s*(?:hey|hi|hello|greetings|howdy)\s*"
    r"(?:everyone|all|team|guys|folks|y'?all|there|community)?\s*[,.!:]?\s+",
    re.I,
)

# Sentences to remove entirely (match the full sentence up to its ending punctuation).
# Each pattern is expected to match an entire sentence (starting at a word
# boundary, ending at . ? ! followed by space or end-of-string).
COMMUNITY_SENTENCE_PATTERNS = [
    # "Has anyone ever encountered ...?"
    r"has\s+(?:any(?:one|body))\s+(?:ever\s+)?[^.?!]*[.?!]",
    # "Have any of you ...?"
    r"have\s+any\s+of\s+(?:you|y'all)\s+[^.?!]*[.?!]",
    # "Does anyone know / have ...?"
    r"does\s+any(?:one|body)\s+[^.?!]*[.?!]",
    # "Did anyone ..."
    r"did\s+any(?:one|body)\s+[^.?!]*[.?!]",
    # "Anyone here / else / have / know / dealt / encountered ..."
    r"any(?:one|body)\s+(?:here|else|have|has|know|knows|dealt|encountered|experienced|familiar)\b[^.?!]*[.?!]",
    # "Any tips / advice / thoughts / suggestions / help / insights / ideas ..."
    r"any\s+(?:tips|advice|thoughts|suggestions|help|insights|ideas|pointers|recommendations|guidance|input|feedback|experiences)\b[^.?!]*[.?!]",
    # "Would love any advice / thoughts ..."
    r"(?:would|i'd)\s+love\s+any\s+[^.?!]*[.?!]",
    # "Can someone help / explain ..."
    r"can\s+some(?:one|body)\s+[^.?!]*[.?!]",
    # "If anyone has ..."
    r"if\s+any(?:one|body)\s+(?:has|knows|can)\s+[^.?!]*[.?!]",
    # "Looking for advice / tips / input ..."
    r"(?:i'm\s+)?looking\s+for\s+(?:advice|tips|input|help|thoughts|suggestions|guidance)[^.?!]*[.?!]",
    # "How do you [experienced] folks/guys/all X ...?"
    r"how\s+do\s+you\s+(?:\w+\s+)?(?:folks|guys|all|everyone|pros|experts|veterans)\b[^.?!]*[.?!]",
    # "How does everyone handle / manage ...?"
    r"how\s+does\s+everyone\s+[^.?!]*[.?!]",
]
SENTENCE_RX = re.compile(
    "|".join(f"(?:{p})" for p in COMMUNITY_SENTENCE_PATTERNS), re.I
)


def clean(text: str) -> str:
    t = text
    # Strip leading greeting(s) — may have multiple (rare but safe).
    prev = None
    while prev != t:
        prev = t
        t = GREETING_RX.sub("", t)
    # Remove community-addressed sentences.
    t = SENTENCE_RX.sub("", t)
    # Collapse repeated whitespace, fix orphan punctuation / spaces.
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([.,!?;:])", r"\1", t)
    t = t.strip()
    # Capitalize first letter if we lowercased the leading word (rare).
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t


def main():
    bank = json.loads(BANK.read_text())
    diffs = []
    changed = 0
    for q in bank:
        orig = q.get("question", "")
        new = clean(orig)
        if new != orig:
            changed += 1
            diffs.append(
                f"--- {q['occupation']} ({q.get('source_url','')}) ---\n"
                f"BEFORE:\n{orig}\n\nAFTER:\n{new}\n"
            )
        q["question"] = new

    CLEANED.write_text(json.dumps(bank, indent=2))
    DIFF.write_text("\n".join(diffs))
    print(f"Items processed: {len(bank)}")
    print(f"Items changed:   {changed}")
    print(f"Wrote cleaned bank to: {CLEANED}")
    print(f"Diff log:              {DIFF}")


if __name__ == "__main__":
    main()
