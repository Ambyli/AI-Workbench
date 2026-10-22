"""POST /evaluate -- dry-run a ruleset on a conversation. Writes nothing.

Either a stored version (``{"ruleset": {"id": "default", "version": 1}}``;
version omitted = the highest) or an unsaved document (``{"document":
{...}}``; ``patterns_ref`` refs resolve inside the baked rulesets directory),
plus ``messages`` / ``tools`` / ``backends`` and optionally
``requested_model`` (default ``auto``) and ``background_task`` (default
false: what the hook sends for an Open WebUI background task). Returns the
/decide response shape with ``decision_id: null`` and, per vote, the
evaluator's ``detail`` and ``error``.

An ``llm_classifier`` reads the STORED verdicts for the conversation, as on
/decide (one read-only query, waited for as long as it takes); /evaluate never
enqueues a classification, so a conversation nobody routed has none and the
evaluator abstains ("no verdict ..."). The signal evaluators get ONE
signals call, as on /decide, but waited for up to
``SIGNALS_EVALUATE_TIMEOUT_S`` (10 s) with no deadline cut, so a draft is
judged on complete signals (``signals_ms`` in their detail says how long it
took); it runs concurrently with the verdict read. A ``pin: turn`` ruleset is never
pinned here: /evaluate carries no chat or message id, so every call is its
turn's first.

    200  the decision
    404  no such ruleset version
    422  the body, or the document, is invalid -- ``detail.errors`` lists every problem
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from .. import config
from ..engine import pipeline
from ..rulesets.loader import RulesetFileError, resolve_refs
from ..rulesets.validate import RulesetInvalid, compile_ruleset
from .schemas import EvaluateRequest

router = APIRouter()


@router.post("/evaluate")
async def evaluate(body: EvaluateRequest, request: Request) -> dict:
    service = request.app.state.service
    if body.document is not None:
        try:
            ruleset = compile_ruleset(resolve_refs(body.document, service.rulesets_dir))
        except RulesetFileError as exc:
            raise HTTPException(422, {"message": "the document's refs cannot be resolved",
                                      "errors": [str(exc)]}) from None
        except RulesetInvalid as exc:
            raise HTTPException(422, {"message": "the document is not a valid ruleset",
                                      "errors": exc.errors}) from None
    else:
        ruleset = await service.store.get(body.ruleset.id, body.ruleset.version)
        if ruleset is None:
            version = f"@{body.ruleset.version}" if body.ruleset.version else ""
            raise HTTPException(404, f"no ruleset {body.ruleset.id}{version}")
    ctx = body.context()
    await asyncio.gather(
        service.load_analyses(ruleset, ctx, budget_s=None),
        service.load_signals(ruleset, ctx, None, kind="evaluate", budget_s=config.SIGNALS_EVALUATE_TIMEOUT_S,
                             deadline=False),
    )
    decision = await pipeline.run(ruleset, ctx)
    return decision.response(None, with_detail=True)
