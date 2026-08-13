"""Approved knowledge entries for the development clinic.

FICTIONAL DEVELOPMENT DATA — "Riverside Medical Clinic" does not exist. Every
answer below is placeholder copy.

Before any real deployment, this content must be replaced with text signed off
by the clinic. Answers are spoken verbatim, so they are written short (one or
two sentences) and in plain language.
"""

from __future__ import annotations

from app.clinic.knowledge import KnowledgeEntry, KnowledgeTopic, StaticClinicKnowledge

DEV_KNOWLEDGE_ENTRIES: list[KnowledgeEntry] = [
    KnowledgeEntry(
        topic=KnowledgeTopic.OPENING_HOURS,
        answer=(
            "We're open weekdays 8am to 6pm, and Saturdays 9am to 1pm. "
            "We're closed Sundays and public holidays."
        ),
        keywords=("opening hours", "open", "hours", "close", "closing", "what time"),
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.LOCATION,
        answer="We're at 12 Riverside Drive, Richmond, close to the train station.",
        # "whereabouts" is everyday Australian phrasing and does not contain
        # "where are you" as a whole word, so it needs its own keyword.
        keywords=(
            "address",
            "where are you",
            "whereabouts",
            "located",
            "location",
            "how do i get",
            "how do i find",
        ),
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.PARKING,
        answer="There's free two-hour parking behind the clinic, off Duke Street.",
        keywords=("parking", "park", "car space"),
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.ACCESSIBILITY,
        answer="The clinic is on the ground floor with step-free access and an accessible toilet.",
        keywords=("wheelchair", "accessible", "accessibility", "step free", "ramp", "disabled"),
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.FEES,
        answer=(
            "A standard consultation is 85 dollars, with a Medicare rebate of about 42 dollars. "
            "Reception can confirm the exact amount."
        ),
        keywords=(
            "fee",
            "fees",
            "cost",
            "price",
            "how much",
            "charge",
            "gap",
            "have to pay",
            "do i pay",
            "is it free",
            "out of pocket",
        ),
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.BULK_BILLING,
        answer=(
            "We bulk bill children under sixteen and pension card holders. "
            "Other appointments are privately billed."
        ),
        keywords=("bulk bill", "bulk billing", "bulk billed", "medicare card", "concession"),
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.CANCELLATION_POLICY,
        answer=(
            "Please give us at least four hours notice to cancel, "
            "otherwise a 30 dollar fee may apply."
        ),
        keywords=("cancellation policy", "cancellation fee", "late cancel", "no show", "notice"),
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.TELEHEALTH,
        answer=(
            "We offer telephone consultations for existing patients seen in the last twelve months."
        ),
        keywords=("telehealth", "phone appointment", "video", "over the phone", "remote"),
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.NEW_PATIENTS,
        answer=(
            "Yes, we're accepting new patients. "
            "New patient appointments are a bit longer, so please arrive five minutes early."
        ),
        keywords=("new patient", "new patients", "first time", "register", "join the clinic"),
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.REFERRALS,
        answer=(
            "You'll need a referral from your GP for specialist appointments. "
            "Referrals last twelve months."
        ),
        keywords=("referral", "referrals", "specialist", "refer me"),
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.SERVICES,
        answer=(
            "We offer general practice, health checks, vaccinations, "
            "skin checks and chronic disease management."
        ),
        # Deliberately no generic question stems ("do you do"): they match
        # every topic and would outrank the specific entry the caller wants.
        keywords=(
            "services",
            "what do you offer",
            "what services",
            "vaccination",
            "skin check",
            "health check",
            "immunisation",
        ),
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.AFTER_HOURS,
        answer=(
            "Outside our opening hours, please call the after hours GP helpline on 13 74 25, "
            "or triple zero in an emergency."
        ),
        keywords=(
            "after hours",
            "afterhours",
            "weekend",
            "overnight",
            "closed now",
            "public holiday",
        ),
    ),
    # --- Topics the bot acknowledges but must never handle itself ----------
    KnowledgeEntry(
        topic=KnowledgeTopic.TEST_RESULTS,
        answer="I can't discuss test results, but I'll put you through to reception.",
        keywords=(
            "test result",
            "test results",
            "results",
            "blood test",
            "pathology",
            "scan",
            "x-ray",
            "biopsy",
        ),
        requires_human=True,
    ),
    KnowledgeEntry(
        topic=KnowledgeTopic.REPEAT_PRESCRIPTIONS,
        answer="I can't arrange prescriptions, but reception can help with that.",
        keywords=("prescription", "repeat script", "script", "medication", "refill", "renew my"),
        requires_human=True,
    ),
]


def build_dev_knowledge() -> StaticClinicKnowledge:
    return StaticClinicKnowledge(entries=DEV_KNOWLEDGE_ENTRIES)
