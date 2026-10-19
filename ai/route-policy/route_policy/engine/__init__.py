"""The decision engine.

    context.py     RequestContext: the request as evaluators see it (parsed once)
    pipeline.py    run(ruleset, ctx): every evaluator -> combiner -> invariants -> Decision
    invariants.py  the two rules no ruleset can override

Deliberately imports nothing here: evaluators import ``engine.context``, and
the pipeline imports the evaluators' base, so a package-level import would
be a cycle.
"""
