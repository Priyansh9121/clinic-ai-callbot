"""Choosing which approved answers the receptionist may speak.

Knowledge entries are read to callers verbatim, so where they come from is a
safety question rather than a convenience one. There are two sources:

* a JSON file the clinic signs off on, named by ``CLINIC_KNOWLEDGE_FILE``;
* the fictional development set in :mod:`app.clinic.knowledge_data`.

The development set exists so the whole system can be exercised without a real
clinic's content, and it is the wrong thing to read down a telephone line to a
patient — "Riverside Medical Clinic" does not exist, and neither do its fees.
So it is refused outright when APP_ENV is production, and announced loudly
everywhere else.

This is deliberately a file and not a CMS. The content changes when a clinic
approves new wording, which is rare, reviewable, and belongs in version control
next to the deployment rather than behind an editor.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.clinic.knowledge import KnowledgeEntry, KnowledgeTopic, StaticClinicKnowledge
from app.clinic.knowledge_data import build_dev_knowledge
from app.core.config import Settings
from app.core.errors import ProviderUnavailableError
from app.core.logging import get_logger

logger = get_logger(__name__)


def load_knowledge_file(path: Path) -> StaticClinicKnowledge:
    """Read approved answers from JSON.

    Shape — a list of objects, each with a topic from :class:`KnowledgeTopic`::

        [
          {
            "topic": "OPENING_HOURS",
            "answer": "We're open 8am to 6pm Monday to Friday.",
            "keywords": ["opening hours", "open", "what time"],
            "requires_human": false
          }
        ]

    Every failure here is fatal at start-up rather than at call time: a missing
    or malformed file must not become a caller hearing silence, or worse, a
    half-loaded answer set that transfers every question it no longer knows.
    """
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise ProviderUnavailableError(
            detail=f"CLINIC_KNOWLEDGE_FILE does not exist: {path}",
            public_message="Clinic knowledge is not configured.",
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProviderUnavailableError(
            detail=f"CLINIC_KNOWLEDGE_FILE is not valid JSON ({path}): line {exc.lineno}",
            public_message="Clinic knowledge is not configured.",
        ) from exc

    if not isinstance(raw, list) or not raw:
        raise ProviderUnavailableError(
            detail=f"CLINIC_KNOWLEDGE_FILE must be a non-empty JSON list: {path}",
            public_message="Clinic knowledge is not configured.",
        )

    entries: list[KnowledgeEntry] = []
    for index, item in enumerate(raw):
        try:
            topic = KnowledgeTopic(item["topic"])
            answer = str(item["answer"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderUnavailableError(
                detail=(
                    f"CLINIC_KNOWLEDGE_FILE entry {index} is invalid ({path}): {exc}. "
                    f"Valid topics: {', '.join(sorted(t.value for t in KnowledgeTopic))}"
                ),
                public_message="Clinic knowledge is not configured.",
            ) from exc
        if not answer:
            raise ProviderUnavailableError(
                detail=f"CLINIC_KNOWLEDGE_FILE entry {index} has an empty answer ({path})",
                public_message="Clinic knowledge is not configured.",
            )
        entries.append(
            KnowledgeEntry(
                topic=topic,
                answer=answer,
                keywords=tuple(str(k) for k in item.get("keywords", ())),
                requires_human=bool(item.get("requires_human", False)),
            )
        )
    return StaticClinicKnowledge(entries=entries)


def build_clinic_knowledge(settings: Settings) -> StaticClinicKnowledge:
    """The approved-answer set for this deployment.

    Falls back to the fictional development content only outside production,
    and says so every time — a placeholder that fails quietly is a placeholder
    that reaches a patient.
    """
    configured = settings.clinic_knowledge_file.strip()
    if configured:
        knowledge = load_knowledge_file(Path(configured))
        logger.info("clinic.knowledge_loaded", source="file", entries=len(knowledge.entries))
        return knowledge

    if settings.app_env == "production":
        raise ProviderUnavailableError(
            detail=(
                "CLINIC_KNOWLEDGE_FILE must be set in production. The development "
                "knowledge set is fictional placeholder copy and must never be "
                "read to a patient."
            ),
            public_message="Clinic knowledge is not configured.",
        )

    logger.warning(
        "clinic.knowledge_is_placeholder",
        detail="fictional development content — set CLINIC_KNOWLEDGE_FILE before deployment",
    )
    return build_dev_knowledge()
