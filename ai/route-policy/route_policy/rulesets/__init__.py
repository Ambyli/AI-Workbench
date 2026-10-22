"""Rulesets: versioned documents of evaluators + a combiner.

    model.py      the pydantic document, the compiled form, the content hash
    loader.py     files -> resolved documents (patterns_ref inlined), seed bindings
    validate.py   resolved document -> CompiledRuleset, or every problem at once
    store.py      seeding at startup, and the cache /decide reads

Deliberately imports nothing here (store.py imports the db layer, which
the loader and validator do not need).
"""
