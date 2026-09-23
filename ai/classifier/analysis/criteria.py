"""Criteria parsing and pattern validation — the first thing a request meets.

Turns the `criteria` form field (a JSON string, because a multipart form
cannot carry structured objects) into typed ``CriterionInput`` objects, and
rejects an unusable text pattern at SUBMIT time so a bad regular expression is
a 400 on the POST rather than a job that fails in a worker minutes later.

    criterion_pattern()      — the text a type="text" criterion searches for.
    validate_text_criteria() — compile every text pattern through the matcher's
                               own path, so the rules cannot drift.
    parse_criteria()         — the JSON string → validated criteria.

Process flow position: the bottom of the analysis package — it imports nothing
from its siblings. Called by ``api.assess`` and ``api.locate`` before a job is
queued, and by ``analysis.text_eval`` for the pattern default.
"""

import json

from fastapi import HTTPException

from common.documents import Document, InvalidPatternError, match_text

from api.schemas import CriterionInput
from logger import logger


def criterion_pattern(c: CriterionInput) -> str:
    """The text a type="text" criterion searches for.

    ``pattern`` when supplied, otherwise the criterion's own name — so
    ``{"name": "Notice to Owner", "type": "text"}`` needs no second field.
    """
    return c.pattern if c.pattern else c.name


def validate_text_criteria(criteria: list[CriterionInput]) -> None:
    """Reject malformed text patterns at submit time rather than at job time.

    A bad regular expression is a client error: catching it here turns it into
    a 400 on the POST instead of a job that fails minutes later in a worker.
    Validation is done by running the matcher's own compile path against an
    empty document, so the rules can never drift from match_text().

    Args:
        criteria: The full criteria list (non-text entries are ignored).

    Raises:
        HTTPException(400): Pattern empty, too long, or an invalid regex.
    """
    empty = Document(kind="txt", pages=[])
    for c in criteria:
        if c.type != "text":
            continue
        try:
            match_text(
                empty,
                criterion_pattern(c),
                c.match,
                case_sensitive=c.case_sensitive,
                fuzzy_threshold=c.fuzzy_threshold,
                min_count=c.min_count,
            )
        except InvalidPatternError as exc:
            logger.error("validate_text_criteria: '%s' rejected: %s", c.name, exc)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid pattern for text criterion '{c.name}': {exc}",
            )


def parse_criteria(raw: str) -> list[CriterionInput]:
    """Parse and validate the criteria JSON string from a multipart form field.

    The /assess endpoint receives criteria as a JSON string (multipart forms
    cannot carry structured objects natively).  This function converts it into
    a typed list of CriterionInput objects and pre-validates any text patterns.

    Args:
        raw: JSON string, e.g. '[{"name":"sharpness","type":"cv","weight":1.0}]'

    Returns:
        List of validated CriterionInput objects.

    Raises:
        HTTPException(400): If the string is not valid JSON, not a list,
            contains items that fail CriterionInput validation, or carries an
            unusable text pattern.
    """
    logger.info("parse_criteria: raw=%s", raw[:200])
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("criteria must be a JSON array")
        result = [CriterionInput(**item) for item in parsed]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.error("parse_criteria: failed to parse criteria: %s", exc)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid criteria JSON: {exc}. "
                "Expected a JSON array of objects, e.g. "
                '[{"name": "image sharpness", "type": "llm", "hint": "quality"}]'
            ),
        )
    validate_text_criteria(result)
    logger.info(
        "parse_criteria: returning %d criteria: %s",
        len(result),
        [f"{c.name}({c.type})" for c in result],
    )
    return result
