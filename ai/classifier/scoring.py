from logger import logger
from llm import _verdict_from_score


def compute_similarity(example_assessment: dict, input_assessment: dict) -> dict:
    example_scores = example_assessment.get("per_criterion_scores", {})
    input_scores = input_assessment.get("per_criterion_scores", {})
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
        similarity = 1.0 - abs(e - i) / 9.0
        per_criterion[criterion] = {"example_score": e, "input_score": i,
                                    "similarity": round(similarity, 3)}
        logger.debug("compute_similarity: %s example=%s input=%s similarity=%.3f",
                     criterion, e, i, similarity)

    overall = sum(v["similarity"] for v in per_criterion.values()) / len(per_criterion)
    result = {"overall_similarity": round(overall, 3),
              "similarity_score": round(overall * 10, 1),
              "per_criterion": per_criterion}
    logger.info("compute_similarity: returning overall_similarity=%.3f similarity_score=%.1f",
                result["overall_similarity"], result["similarity_score"])
    return result


def combined_score(input_overall: float, similarity_score: float, weight: float) -> dict:
    logger.info("combined_score: input_overall=%s similarity_score=%s weight=%s",
                input_overall, similarity_score, weight)
    score = (1.0 - weight) * input_overall + weight * similarity_score
    score = round(max(1.0, min(10.0, score)), 1)
    result = {"score": score, "verdict": _verdict_from_score(int(score))}
    logger.info("combined_score: returning score=%s verdict=%s", result["score"], result["verdict"])
    return result


def aggregate(scores: list[float], method: str) -> dict:
    logger.info("aggregate: scores=%s method=%s", scores, method)
    if method == "min":
        score = min(scores)
    elif method == "max":
        score = max(scores)
    else:
        score = sum(scores) / len(scores)
    score = round(score, 1)
    result = {"score": score, "verdict": _verdict_from_score(int(score))}
    logger.info("aggregate: returning score=%s verdict=%s", result["score"], result["verdict"])
    return result
