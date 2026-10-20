"""Background classifications for the ``llm_classifier`` evaluator.

    model.py              AnalysisRequest (what to classify) and Verdict (what came back) -- pure
    verdict.py            choose(): per-route 1-10 scores -> one route and a confidence -- pure
    classifier_client.py  one classification through the document classifier's POST /assess:
                          locality guard, submit, poll, read, DELETE (also used by bench/)
    queue.py              AnalysisQueue: bounded, deduplicated, in memory; workers that run
                          the client and write one ``analyses`` row per prefix

The flow: ``/decide`` answers, then the service collects each decided
ruleset's ``Evaluator.analysis_requests`` and submits them here; a worker
classifies the conversation prefix and stores the verdict under its
content-addressed key; the next ``/decide`` reads the verdicts it needs back
(``Service.load_analyses``) before the pipeline runs. Nothing here is on the
request path, and no conversation text is ever stored.

Deliberately imports nothing here: ``engine.context`` and ``evaluators.base``
import ``analysis.model``, and the queue imports the db layer, so a
package-level import would load far more than they need (and risk a cycle).
"""
