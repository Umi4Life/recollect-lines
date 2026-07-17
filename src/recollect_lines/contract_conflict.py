"""Deterministic pre-delegate schema/prose conflict detection (Wave 4 / PR 11).

Runs once at Broker.create() time, before the child is ever launched. Flags a
task whose prose reads as an open-ended, unstructured request (a debate, an
essay, a story, ...) while a structured `result_schema` was requested —
structurally the same shape as the Wave 0 dogfood incident, caught earlier
and advisory rather than after the fact.

Advisory only: an unmatched or ambiguous task is never flagged, and a match
never blocks task creation — see `detect_schema_prose_conflict`. Only a fixed,
static keyword name is ever recorded (e.g. "debate") — never the task text
itself or any excerpt of it.
"""

from __future__ import annotations

import re

from .result_normalization import DEFAULT_RESULT_SCHEMA

# Static, closed vocabulary: genres of open-ended prose that structurally
# cannot satisfy a JSON result contract (a debate/essay/story has no natural
# summary+findings shape). Deliberately narrow so ambiguous or unmatched
# prose is never flagged.
_PROSE_GENRE_SIGNALS: tuple[str, ...] = (
    "debate", "discuss", "discussion", "essay", "story", "poem", "narrative",
    "brainstorm", "opinion piece",
)

_STRUCTURED_SCHEMAS = frozenset({"evidence-report", "review-findings", "implementation-report"})


def detect_schema_prose_conflict(task_text: str, result_schema: str | None) -> dict[str, str] | None:
    """Return a safe, structured conflict warning, or None when nothing matched.

    Deterministic and static-vocabulary only: whether a fixed prose-genre
    word appears in `task_text` is the entire signal. Never records the task
    text itself.
    """
    schema = result_schema or DEFAULT_RESULT_SCHEMA
    if schema not in _STRUCTURED_SCHEMAS or not isinstance(task_text, str):
        return None
    lowered = task_text.lower()
    for signal in _PROSE_GENRE_SIGNALS:
        if re.search(rf"\b{re.escape(signal)}\b", lowered):
            return {
                "code": "prose_genre_vs_structured_schema",
                "requested_schema": schema,
                "matched_signal": signal,
                "message": (
                    f"Task prose matches an open-ended prose signal ({signal!r}) while "
                    f"result_schema={schema!r} requires a structured JSON contract; the "
                    "runtime may return plain prose that cannot satisfy it."
                ),
            }
    return None
