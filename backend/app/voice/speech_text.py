"""Deterministic text→speech normalisation.

Applied to an approved caller-facing reply immediately before synthesis. It
never changes *what* is said, only how it is pronounced, and it is
deterministic — no model is involved in deciding how to say a phone number.

**Deliberately small.** Round-tripping the response catalogue through
Piper→Whisper showed times ("12:15 pm"), dates ("Friday the 21st") and hour
ranges ("8am to 6pm") already pronounced correctly, so normalising them would
add risk for no benefit. Only two cases needed work:

* **Digit strings** — "13 74 25" risks being read as a quantity ("thirteen
  seventy-four…") rather than digits. Phone numbers must be dictated.
* **"Dr"** — spoken as "Doctor". Presentation stays separate from storage:
  ``practitioners.display_name`` keeps "Dr Sarah Patel" and only the spoken
  form changes, per docs/architecture/voice-pipeline.md.

Identifiers are explicitly *not* transformed: a UUID or a reference code must
never be silently rewritten by a pronunciation rule.
"""

from __future__ import annotations

import re

#: "Dr" / "Dr." as a whole word, not inside a longer word.
_TITLES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bDr\.?(?=\s+[A-Z])"), "Doctor"),
    (re.compile(r"\bSt\.?(?=\s+[A-Z])"), "Saint"),
)

#: A run of digits and phone separators long enough to be a dictated number:
#: mobiles, landlines with an area code, and service numbers like 13 74 25.
#:
#: Matched as a whole run rather than by shape. An earlier shape-based pattern
#: consumed only part of "0491 570 156" and left "156" as a quantity, which is
#: worse than not normalising at all.
#:
#: The trailing lookahead excludes only a digit or a colon. It must *not*
#: exclude ``.`` — a number at the end of a sentence is the common case, and
#: excluding it made the pattern backtrack and truncate the final group again.
#: The lookbehind still blocks decimals and clock times from the left.
_PHONE = re.compile(r"(?<![\w.:])\+?\d[\d\s()-]{4,18}\d(?![\d:])")

#: A run that is *only* digits and separators, at least 6 long — a number
#: someone would dictate rather than quantify.
_DIGIT_RUN = re.compile(r"^[\d\s+()-]+$")

_DIGIT_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
}


def _spell_digits(match: re.Match[str]) -> str:
    """Rewrite a phone-like run as dictated digits.

    Grouping is preserved as commas so the synthesiser pauses where a person
    would: "0491 570 156" → "zero four nine one, five seven zero, one five six".
    """
    raw = match.group(0)
    if not _DIGIT_RUN.match(raw):
        return raw
    digits_only = re.sub(r"\D", "", raw)
    if not 6 <= len(digits_only) <= 15:
        return raw

    groups = [g for g in re.split(r"[\s-]+", raw.strip()) if g]
    spoken_groups: list[str] = []
    for group in groups:
        digits = re.sub(r"\D", "", group)
        if not digits:
            continue
        spoken_groups.append(" ".join(_DIGIT_WORDS[d] for d in digits))
    if not spoken_groups:
        return raw
    prefix = "plus " if raw.strip().startswith("+") else ""
    return prefix + ", ".join(spoken_groups)


def for_speech(text: str) -> str:
    """Normalise an approved reply for synthesis.

    Idempotent: applying it twice yields the same string, so it is safe to run
    on text that has already been normalised (e.g. a cached phrase).
    """
    if not text:
        return ""
    spoken = _PHONE.sub(_spell_digits, text)
    for pattern, replacement in _TITLES:
        spoken = pattern.sub(replacement, spoken)
    return re.sub(r"\s{2,}", " ", spoken).strip()


def practitioner_for_speech(display_name: str) -> str:
    """Spoken form of a practitioner's display name.

    Separate entry point so a future pronunciation table (for names the
    synthesiser mangles) has an obvious home that does not involve editing
    database values.
    """
    return for_speech(display_name)
