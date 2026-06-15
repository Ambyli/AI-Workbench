"""Scoring and similarity helpers used by the /assess/compare endpoint.

These functions operate on already-analysed assessment dicts (output of
analysis.analyze_bgr) and compute how similar two images are and how to
blend or aggregate multiple scores into a final verdict.

  compute_similarity()  — compare two assessments per criterion, returning a
                          0-1 similarity score for each shared criterion and
                          an overall similarity score on a 0-10 scale.

  combined_score()      — blend an input image's absolute quality score with
                          its similarity score to a reference, weighted by the
                          example's weight parameter.

  aggregate()           — collapse a list of per-example combined scores into
                          a single aggregate verdict using mean, min, or max.

Process flow position: called by workers._run_compare() after all analyses
(input + all examples) have been gathered via asyncio.gather().
"""

from logger import logger
from utils import verdict_from_score as _verdict_from_score


def compute_similarity(example_assessment: dict, input_assessment: dict) -> dict:
    """Compare two LLM assessments criterion-by-criterion.

    Similarity per criterion is computed as:
        1.0 - abs(example_score - input_score) / 9.0
    where 9.0 is the maximum possible score difference (10 - 1).
    This maps identical scores to 1.0 and maximally different scores to 0.0.

    Only criteria present in both assessments contribute to the overall
    similarity.  If there are no common criteria, a neutral 0.5 is returned.

    Args:
        example_assessment: llm_assessment dict from a reference image analysis.
        input_assessment:   llm_assessment dict from the subject image analysis.

    Returns:
        dict with overall_similarity (0-1), similarity_score (0-10), and a
        per_criterion breakdown showing example_score, input_score, and
        similarity for each shared criterion.
    """
    example_scores = example_assessment.get("per_criterion_scores", {})
    input_scores = input_assessment.get("per_criterion_scores", {})

    # Only compare criteria that both images were assessed against
    common = set(example_scores.keys()) & set(input_scores.keys())
    logger.info("compute_similarity: common criteria=%s", sorted(common))

    if not common:
        logger.warning("compute_similarity: no common criteria found, returning neutral similarity")
        return {"overall_similarity": 0.5, "similarity_score": 5.0,
                "per_criterion": {}, "note": "No common criteria to compare"}

    per_criterion = {}
    for criterion in common:
        e = example_scores[criterion].get("score", 5)
        i = input_scores[criterion].get("score", 5)
        # Normalise score difference to [0, 1] where 1 = identical
        similarity = 1.0 - abs(e - i) / 9.0
        per_criterion[criterion] = {"example_score": e, "input_score": i,
                                    "similarity": round(similarity, 3)}
        logger.debug("compute_similarity: %s example=%s input=%s similarity=%.3f",
                     criterion, e, i, similarity)

    # Average per-criterion similarities and scale to 0-10 for display
    overall = sum(v["similarity"] for v in per_criterion.values()) / len(per_criterion)
    result = {"overall_similarity": round(overall, 3),
              "similarity_score": round(overall * 10, 1),
              "per_criterion": per_criterion}
    logger.info("compute_similarity: returning overall_similarity=%.3f similarity_score=%.1f",
                result["overall_similarity"], result["similarity_score"])
    return result


def combined_score(input_overall: float, similarity_score: float, weight: float) -> dict:
    """Blend an input image's absolute quality score with its similarity score.

    Formula:
        combined = (1 - weight) × input_overall + weight × similarity_score

    At weight=0.0 the result is the input's own quality score (example ignored).
    At weight=1.0 the result is purely how similar the input is to the example.
    At weight=0.5 both contribute equally.

    The result is clamped to [1.0, 10.0] before converting to a verdict.

    Args:
        input_overall:    The subject image's weighted overall score (1-10).
        similarity_score: Similarity to the reference example (0-10).
        weight:           ExampleInput.weight (0.0-1.0).

    Returns:
        dict with score (1-10) and verdict (PASS/MARGINAL/FAIL).
    """
    logger.info("combined_score: input_overall=%s similarity_score=%s weight=%s",
                input_overall, similarity_score, weight)
    score = (1.0 - weight) * input_overall + weight * similarity_score
    score = round(max(1.0, min(10.0, score)), 1)
    result = {"score": score, "verdict": _verdict_from_score(int(score))}
    logger.info("combined_score: returning score=%s verdict=%s", result["score"], result["verdict"])
    return result


def aggregate(scores: list[float], method: str) -> dict:
    """Collapse a list of per-example combined scores into one aggregate verdict.

    Three methods are supported:
      mean — average of all scores (balanced, default).
      min  — lowest score wins; the subject must be close to every example.
      max  — highest score wins; matching any one example is enough.

    Args:
        scores: List of combined_score values, one per reference example.
        method: "mean", "min", or "max".

    Returns:
        dict with score (1-10) and verdict (PASS/MARGINAL/FAIL).
    """
    logger.info("aggregate: scores=%s method=%s", scores, method)
    if method == "min":
        score = min(scores)
    elif method == "max":
        score = max(scores)
    else:
        # Default: mean across all examples
        score = sum(scores) / len(scores)
    score = round(score, 1)
    result = {"score": score, "verdict": _verdict_from_score(int(score))}
    logger.info("aggregate: returning score=%s verdict=%s", result["score"], result["verdict"])
    return result
