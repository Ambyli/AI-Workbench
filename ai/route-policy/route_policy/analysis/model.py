"""What an analysis is asked for, and what one came back as.

Two plain records, shared by the evaluator that asks (``evaluators.
llm_classifier``), the queue that runs the classification (``analysis.
queue``), the table that keeps the result (``db/repositories/analyses.py``)
and the request view that carries verdicts to the evaluator
(``engine.context.RequestContext.analyses``). Pure: no I/O, no imports from
the rest of the service, so any of those may import it without a cycle.

**Keyed by the conversation itself.** A verdict is addressed by
``input_key`` = SHA-256 over the evaluator's configuration fingerprint and
the transcript it classified (``evaluators.llm_classifier.input_key``) -- not
by a chat or message id. The same conversation prefix always maps to the
same key, so tool rounds of one message ask for one classification, an
edited or regenerated message can never reuse a verdict made for different
text, and a caller with no Open WebUI chat id works the same way.

**The transcript is memory only.** :attr:`AnalysisRequest.text` is the
rendered conversation the classifier reads; it lives in route-policy's
memory (and, for the length of one job, in the document classifier) and is
never stored, logged or returned. ``repr=False`` keeps it out of any log line
that prints a request.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

# Statuses an analysis can end in (ClassifierClient.assess). The first two
# carry scores; the rest are failures, recorded so their rates can be read in
# Trino, and never retried for the same prefix (the next user message is a
# new prefix, so a new attempt).
USABLE_STATUSES = ("ok", "incomplete")
FAILED_STATUSES = ("unavailable", "refused", "failed", "timeout", "not_local")
STATUSES = USABLE_STATUSES + FAILED_STATUSES


@dataclass(frozen=True)
class AnalysisRequest:
    """One conversation prefix to classify -- built by an ``llm_classifier``
    evaluator (``analysis_requests``), completed with its provenance by the
    service, run by ``analysis.queue.AnalysisQueue``."""

    input_key: str
    fingerprint: str
    model: str                          # a classifier registry id (GET /models)
    criteria: Mapping[str, str]         # route -> criterion text, in tie order (the ruleset's routes)
    otherwise: Optional[str]
    min_score: int
    exchange_index: int                 # k: the prefix ends with the k-th user message (1-based)
    input_chars: int                    # len(text)
    evaluator_id: str
    text: str = field(repr=False, compare=False)   # the rendered transcript -- memory only
    # Who asked first. The evaluator knows the chat and message; the service
    # fills in the ruleset and the decision (dataclasses.replace).
    chat_id: Optional[str] = None
    message_id: Optional[str] = None
    ruleset_id: Optional[str] = None
    ruleset_version: Optional[int] = None
    decision_id: Optional[uuid.UUID] = None


@dataclass(frozen=True)
class Verdict:
    """One stored analysis, as the evaluator reads it back (an ``analyses``
    row without its cost columns)."""

    input_key: str
    status: str
    route: Optional[str]                # None: failed, or no criterion reached min_score and no `otherwise`
    confidence: Optional[float]
    scores: Dict[str, Optional[int]]    # route -> 1..10 (None where that criterion errored)
    exchange_index: int
    model_id: str

    @property
    def usable(self) -> bool:
        """It carries scores (``ok`` / ``incomplete``)."""
        return self.status in USABLE_STATUSES
