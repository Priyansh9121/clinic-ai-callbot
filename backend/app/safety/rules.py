"""Deterministic safety rules.

SCOPE AND LIMITATIONS — read before changing anything here.

This rule set is a *screening* layer, not a clinical triage system. It is
deliberately biased toward false positives: wrongly directing a caller to
emergency services is a recoverable inconvenience, whereas missing a genuine
emergency is not. It has not been clinically validated and must be reviewed by
a qualified clinician before any real patient deployment.

Each rule is a regular expression over the normalised caller utterance.
Patterns use word boundaries so that "stroke" does not fire on "strokes of
luck", and negation-aware phrasing is handled by ``NEGATION_GUARDS``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.safety.models import SafetyAction, SafetyCategory


@dataclass(frozen=True, slots=True)
class SafetyRule:
    rule_id: str
    category: SafetyCategory
    action: SafetyAction
    pattern: re.Pattern[str]
    description: str


def _rule(
    rule_id: str,
    category: SafetyCategory,
    action: SafetyAction,
    pattern: str,
    description: str,
) -> SafetyRule:
    return SafetyRule(
        rule_id=rule_id,
        category=category,
        action=action,
        pattern=re.compile(pattern, re.IGNORECASE),
        description=description,
    )


# Contractions are normalised before matching (can't -> cant), so patterns
# match the collapsed form.
EMERGENCY_RULES: list[SafetyRule] = [
    _rule(
        "EMG_CARDIAC",
        SafetyCategory.MEDICAL_EMERGENCY,
        SafetyAction.EMERGENCY,
        r"\b(chest pain|chest pains|pain in (my|his|her|their) chest|"
        r"crushing chest|tightness in (my|his|her|their) chest|heart attack)\b",
        "Possible cardiac event",
    ),
    _rule(
        "EMG_BREATHING",
        SafetyCategory.MEDICAL_EMERGENCY,
        SafetyAction.EMERGENCY,
        r"\b(cant breathe|cannot breathe|not breathing|stopped breathing|"
        r"struggling to breathe|trouble breathing|difficulty breathing|"
        r"gasping for (air|breath)|choking|cant catch (my|his|her|their) breath)\b",
        "Airway or breathing compromise",
    ),
    _rule(
        "EMG_UNCONSCIOUS",
        SafetyCategory.MEDICAL_EMERGENCY,
        SafetyAction.EMERGENCY,
        r"\b(unconscious|unresponsive|passed out|collapsed|blacked out|"
        r"wont wake up|will not wake up|cant wake (him|her|them) up|fainted)\b",
        "Altered consciousness",
    ),
    _rule(
        "EMG_BLEEDING",
        SafetyCategory.MEDICAL_EMERGENCY,
        SafetyAction.EMERGENCY,
        r"\b(severe bleeding|heavy bleeding|bleeding badly|bleeding heavily|"
        r"wont stop bleeding|will not stop bleeding|losing a lot of blood|"
        r"haemorrhag\w*|hemorrhag\w*)\b",
        "Major haemorrhage",
    ),
    _rule(
        "EMG_STROKE",
        SafetyCategory.MEDICAL_EMERGENCY,
        SafetyAction.EMERGENCY,
        r"\b(stroke|face is drooping|face drooping|slurred speech|slurring)\b|"
        r"\b(cant|cannot) (move|feel) (my|his|her|their) (arm|arms|leg|legs|side|face)\b",
        "Possible stroke",
    ),
    _rule(
        "EMG_OVERDOSE",
        SafetyCategory.MEDICAL_EMERGENCY,
        SafetyAction.EMERGENCY,
        r"\b(overdose|overdosed|od'?d|took too many (pills|tablets)|poisoned|poisoning|"
        r"swallowed (bleach|chemicals))\b",
        "Overdose or poisoning",
    ),
    _rule(
        "EMG_ANAPHYLAXIS",
        SafetyCategory.MEDICAL_EMERGENCY,
        SafetyAction.EMERGENCY,
        r"\b(anaphyla\w*|allergic reaction|throat is closing|throat closing|"
        r"lips are swelling|face is swelling|epipen)\b",
        "Anaphylaxis",
    ),
    _rule(
        "EMG_SEIZURE",
        SafetyCategory.MEDICAL_EMERGENCY,
        SafetyAction.EMERGENCY,
        r"\b(seizure|seizures|convulsing|convulsion|fitting|having a fit)\b",
        "Active seizure",
    ),
    _rule(
        "EMG_OBSTETRIC",
        SafetyCategory.MEDICAL_EMERGENCY,
        SafetyAction.EMERGENCY,
        r"\b(waters (have )?broke|in labour|in labor|giving birth|baby is coming)\b",
        "Obstetric emergency",
    ),
    _rule(
        "EMG_TRAUMA",
        SafetyCategory.MEDICAL_EMERGENCY,
        SafetyAction.EMERGENCY,
        r"\b(car accident|hit by a car|serious burns|badly burned|broken (his|her|my|their) neck|"
        r"head injury|fell from a (ladder|roof|height))\b",
        "Major trauma",
    ),
    _rule(
        "EMG_SELF_HARM",
        SafetyCategory.SELF_HARM,
        SafetyAction.EMERGENCY,
        r"\b(kill myself|killing myself|end my life|ending my life|take my own life|"
        r"suicide|suicidal|hurt myself|harm myself|hurting myself|self harm|"
        r"dont want to (live|be here anymore)|better off dead|no reason to live)\b",
        "Suicidal ideation or self-harm",
    ),
]


ESCALATION_RULES: list[SafetyRule] = [
    _rule(
        "ESC_CLINICAL_ADVICE",
        SafetyCategory.CLINICAL_ADVICE,
        SafetyAction.ESCALATE_HUMAN,
        r"\b(what should i do about|is (this|it|that) (serious|normal|dangerous|ok|okay)|"
        r"do i need to (see|worry)|what do you think (it|this) is|"
        r"whats wrong with me|what's wrong with me|diagnos\w+|should i be worried|"
        r"is it safe to)\b",
        "Request for clinical advice or diagnosis",
    ),
    _rule(
        "ESC_MEDICATION",
        SafetyCategory.MEDICATION,
        SafetyAction.ESCALATE_HUMAN,
        r"\b(how (much|many) (should|can) i take|dosage|dose of|"
        r"can i take .{0,30}(with|and) |double up on|stop taking|"
        r"side effects?|interact\w* with)\b",
        "Medication or dosage question",
    ),
    _rule(
        "ESC_TEST_RESULTS",
        SafetyCategory.TEST_RESULTS,
        SafetyAction.ESCALATE_HUMAN,
        # Qualifiers stack: "my blood test results", "my pathology scan results".
        r"\bmy\s+((test|blood|pathology|scan|x ray|xray|biopsy|ultrasound|swab)\s+)*results?\b"
        r"|\bresults?\s+(are\s+|is\s+)?(back|in|ready)\b"
        r"|\b(get|have|hear|chase|check)\s+(on\s+)?(my|the)\s+results?\b"
        r"|\bwhat did (the|my) (test|blood test|scan|x ray|xray|biopsy) show\b",
        "Request for test results",
    ),
    _rule(
        "ESC_DISTRESS",
        SafetyCategory.DISTRESS,
        SafetyAction.ESCALATE_HUMAN,
        r"\b(im in agony|i'?m in agony|excruciating|unbearable pain|"
        r"cant cope|cannot cope|desperate|please help me)\b",
        "Caller in acute distress",
    ),
]


# Phrasings that should NOT trigger, despite containing a trigger term.
# Checked before the rules run; a match suppresses that rule only.
NEGATION_GUARDS: dict[str, tuple[re.Pattern[str], ...]] = {
    "EMG_CARDIAC": (
        # Booking a follow-up about a past episode is not an active emergency.
        re.compile(
            r"\b(had|about|regarding|follow.?up|check.?up|history of|"
            r"used to have|last (week|month|year))\b.{0,40}\bchest pain",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bchest pain\b.{0,30}\b(last (week|month|year)|a while ago|resolved)\b", re.IGNORECASE
        ),
    ),
    "EMG_STROKE": (
        # "stroke clinic", "post-stroke review" are administrative.
        re.compile(r"\bstroke\b\s*(clinic|review|rehab|specialist|nurse)\b", re.IGNORECASE),
        re.compile(
            r"\b(after|post|had a)\s*stroke\b.{0,30}\b(review|check|follow.?up)\b", re.IGNORECASE
        ),
    ),
}


def all_rules() -> list[SafetyRule]:
    """Emergency rules first — evaluation order is the priority order."""
    return [*EMERGENCY_RULES, *ESCALATION_RULES]
