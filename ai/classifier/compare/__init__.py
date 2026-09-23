"""Comparing two documents: how alike, and what changed.

Both halves of POST /assess/compare's extra work, neither of which fits in
``analysis`` because both need TWO documents in one place:

    scoring.py  how alike — per-criterion similarity, the quality/similarity
                blend, and the mean/min/max aggregate across examples.
    diff.py     what changed — ORB + RANSAC alignment then a thresholded
                difference, classifying each blob as added / removed /
                changed. Classical CV, no model.

Diff regions are NOT criteria: they are filed under a synthetic ``_diff:e{i}``
name so they have somewhere to live in regions.json, and they never reach
``compute_weighted_score`` or ``compute_similarity``. The aggregate score with
`diff` on must equal the one with it off.

Process flow position: called by ``jobs.runners.run_compare``.
"""
