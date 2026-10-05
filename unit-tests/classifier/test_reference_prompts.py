"""The reference-guided scoring prompt, and the pure helpers around it.

  * ``build_llm_prompt(references=None)`` (and ``[]``) is BYTE-FOR-BYTE the
    plain prompt — pinned by hash, for four representative calls. If you
    change the scoring prompt on purpose, regenerate the hashes with
    ``_hash(build_llm_prompt(*args, **kwargs))`` for each ``GOLDEN`` call and
    say so in the change.
  * the prefix-cache layout: two criteria on the same image and text share a
    serialized prefix running through the whole DOCUMENT TEXT block;
  * with examples: the content order (each caption then its image, the
    CANDIDATE heading, the candidate, then the usual text), the captions per
    verdict, the one system-prompt sentence, and that only the candidate's
    text is sent;
  * ``plan_calls`` — contrastive pairing at three images, one example per
    call at two, the call cap; ``combine`` — any / all / mean;
  * ``description_validator`` — the describe call's answer;
  * the ``auto`` selection — the catalogue entry and text, the one-image
    selection prompt, and ``selection_validator``.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from analysis.references import combine, plan_calls
from config import DOCUMENT_TEXT_HEADING, HINT_RUBRICS
from llm.prompts import CANDIDATE_HEADING, ReferenceExample, build_llm_prompt
from llm.validate import description_validator

# sha256 of json.dumps(build_llm_prompt(*args, **kwargs), sort_keys=True,
# ensure_ascii=False) (default env). The two calls WITHOUT document text are
# still the hashes from before references were added; the two WITH text were
# regenerated when the DOCUMENT TEXT block moved ahead of the rubric and the
# criterion for vLLM prefix caching (and the presence rubric's "provided
# below" became "provided above").
GOLDEN = [
    ((("ZmFrZQ==", "has a house"), {}),
     "e577ab6fc567d7a15314ea972227b3d4c4066d1fb19964f14f74ac2b524c2249"),
    ((("ZmFrZQ==", "has a house", "presence", "Total due $5"), {"document_kind": "pdf"}),
     "ff79fee2611e44ddcd6c16030c944c64c3bc3d67763b7a3cae1613d83a682f6e"),
    (((None, "mentions a warranty", "auto", "Limited Warranty text"),
      {"document_kind": "txt", "text_truncated": True}),
     "096e85e3692b0d506ccb57ae9fa89d190dda1edbb9b4f8cbfde00cafc038af1f"),
    ((("ZmFrZQ==", "image sharpness", "quality"), {}),
     "08766fcc8c87747f99262f29aa162eae3c5b03755e370df3a291622c3810c20c"),
]


def _hash(prompt: dict) -> str:
    raw = json.dumps(prompt, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("call, digest", GOLDEN)
@pytest.mark.parametrize("references", ["omitted", None, []])
def test_no_references_is_byte_identical_to_before(call, digest, references):
    args, kwargs = call
    if references != "omitted":
        kwargs = {**kwargs, "references": references}
    assert _hash(build_llm_prompt(*args, **kwargs)) == digest


@pytest.mark.parametrize("document_kind, image", [("pdf", "ZmFrZQ=="), ("txt", None)])
def test_criteria_on_one_page_share_a_prefix_through_the_document_text(document_kind, image):
    """Prefix caching: different names AND different hints on the same image
    and text layer serialize to messages that agree on everything up to the
    rubric — system prompt, image, context line and the WHOLE text block —
    so vLLM can serve that prefix from cache for every criterion on a page."""
    text = "Account 0042\nAmount due $193.33\n" + "Line item text. " * 400
    first = build_llm_prompt(image, "has a house", "presence", text,
                             document_kind=document_kind)
    second = build_llm_prompt(image, "image sharpness is acceptable", "quality", text,
                              document_kind=document_kind)
    a = json.dumps(first["messages"], ensure_ascii=False)
    b = json.dumps(second["messages"], ensure_ascii=False)
    common = 0
    while common < min(len(a), len(b)) and a[common] == b[common]:
        common += 1
    shared = a[:common]

    assert first["messages"][0] == second["messages"][0]  # same system prompt
    # The shared prefix runs through the whole text block (json-escaped as
    # it is serialized) and stops at the rubric, the first per-criterion part.
    block = json.dumps(f"{DOCUMENT_TEXT_HEADING}:\n---\n{text}\n---\n",
                       ensure_ascii=False)[1:-1]
    assert block in shared
    assert HINT_RUBRICS["presence"]["heading"] not in shared
    assert HINT_RUBRICS["quality"]["heading"] not in shared
    assert "CRITERION:" not in shared
    if image:
        assert "ZmFrZQ==" in shared
    # And the per-criterion parts still follow, in order.
    user = first["messages"][1]["content"][-1]["text"]
    assert (user.index(DOCUMENT_TEXT_HEADING)
            < user.index(HINT_RUBRICS["presence"]["heading"])
            < user.index("CRITERION: has a house")
            < user.index("Score this one criterion"))


def _example(verdict="PASS", score=10, reason="two storeys", whole=False, image="UEFTUw=="):
    return ReferenceExample(criterion="has a house", verdict=verdict, score=score,
                            reason=reason, image_b64=image, whole_page=whole)


def test_examples_come_first_then_the_candidate_then_the_text():
    plain = build_llm_prompt("Q0FORA==", "has a house", "presence", "Total due $5")
    guided = build_llm_prompt(
        "Q0FORA==", "has a house", "presence", "Total due $5",
        references=[_example(), _example("FAIL", 2, "a barn", image="RkFJTA==")],
    )
    content = guided["messages"][1]["content"]
    assert [p["type"] for p in content] == [
        "text", "image_url", "text", "image_url", "text", "image_url", "text"]
    assert content[1]["image_url"]["url"].endswith("UEFTUw==")
    assert content[3]["image_url"]["url"].endswith("RkFJTA==")
    assert content[4]["text"].endswith(f"{CANDIDATE_HEADING}:")
    assert "Do not reward resemblance the criterion does not ask about" in content[4]["text"]
    assert content[5]["image_url"]["url"].endswith("Q0FORA==")
    # The text part is exactly the unguided one — only the candidate's text layer.
    assert content[6] == plain["messages"][1]["content"][1]
    assert guided["messages"][0]["content"].startswith(plain["messages"][0]["content"].split(
        "\nReasoning strength")[0])
    assert "score ONLY the image marked CANDIDATE" in guided["messages"][0]["content"]
    for key in ("model", "max_tokens", "response_format"):
        assert guided[key] == plain[key]


def test_captions_per_verdict():
    assert _example().caption() == (
        "REFERENCE EXAMPLE — in this reference 'has a house' was PASS (10): two storeys; "
        "the coloured box outlines it."
    )
    assert "the whole image is the example" in _example(whole=True).caption()
    fail = _example("FAIL", 2, "a barn").caption()
    assert "was FAIL (2) — an example of what does NOT satisfy the criterion: a barn" in fail
    assert "was MARGINAL (5) — a borderline case" in _example("MARGINAL", 5).caption()
    assert _example(reason="").caption().startswith(
        "REFERENCE EXAMPLE — in this reference 'has a house' was PASS (10); ")


def test_a_text_only_candidate_still_gets_the_heading():
    guided = build_llm_prompt(None, "mentions a warranty", "auto", "Warranty",
                              document_kind="txt", references=[_example()])
    types = [p["type"] for p in guided["messages"][1]["content"]]
    assert types == ["text", "image_url", "text", "text"]


# ── plan_calls / combine ───────────────────────────────────────────────────


def _ex(*polarities):
    return [{"polarity": p} for p in polarities]


def test_plan_calls_pairs_pass_with_fail_at_three_images():
    assert plan_calls(_ex("pass", "fail"), 3) == [[0, 1]]
    assert plan_calls(_ex("fail", "pass", "pass"), 3) == [[1, 0], [2]]
    assert plan_calls(_ex("marginal", "fail", "fail"), 3) == [[0, 1], [2]]
    assert plan_calls(_ex("pass"), 3) == [[0]]


def test_plan_calls_one_per_call_at_two_images_and_the_cap():
    assert plan_calls(_ex("pass", "fail", "pass"), 2) == [[0], [1], [2]]
    assert plan_calls(_ex("pass", "fail", "pass"), 2, max_calls=2) == [[0], [1]]
    assert plan_calls(_ex("pass"), 1) == [] and plan_calls([], 3) == []


def _answer(score, reason="r", confidence=50):
    from utils import verdict_from_score

    return {"score": score, "verdict": verdict_from_score(score),
            "confidence": confidence, "reason": reason}


def test_combine_rules():
    answers = [(0, _answer(4, "low", 40)), (1, _answer(9, "high", 90)), (2, _answer(9, "tie"))]
    best, chosen = combine(answers, "any")
    assert (best["score"], best["reason"], chosen) == (9, "high", 1)
    worst, chosen = combine(answers, "all")
    assert (worst["score"], worst["reason"], chosen) == (4, "low", 0)
    mean, _ = combine(answers[:2], "mean")
    assert mean["score"] == 6 and mean["verdict"] == "MARGINAL"   # round(6.5) == 6
    assert mean["confidence"] == 65


# ── description_validator ──────────────────────────────────────────────────


def test_description_validator():
    validate = description_validator(40)
    assert validate({"description": "  A brick   house\nwith a porch. "}) == (
        "A brick house with a porch.")
    cut = validate({"description": "word " * 30})
    assert len(cut) <= 40 and not cut.endswith(" ")
    for bad in ({}, {"description": ""}, {"description": 3}, ["x"]):
        with pytest.raises(ValueError):
            validate(bad)


# ── the auto selection: catalogue, prompt, validator ───────────────────────


def _ref(rid="r0123456789ab", **record_criteria):
    from references.model import Reference

    criteria = record_criteria or {
        "has a house": {"usable": True, "expected": {"verdict": "PASS", "score": 10}},
        "Net 30": {"usable": False, "expected": None},
    }
    return Reference(
        id=rid, status="ready", source_kind="document", created_at="t", updated_at="t",
        title="Brick house", description="A two-storey brick house.", tags=["houses", "brick"],
        record={"criteria": criteria},
    )


def test_the_catalogue_lists_usable_criteria_only():
    from references.render import catalogue_entry, catalogue_text

    entry = catalogue_entry(_ref())
    assert entry == {
        "reference_id": "r0123456789ab", "title": "Brick house",
        "description": "A two-storey brick house.", "tags": ["houses", "brick"],
        "criteria": [{"name": "has a house", "verdict": "PASS", "score": 10}],
    }
    text = catalogue_text([entry, {**entry, "reference_id": "rfedcba987654", "title": None,
                                   "description": None, "tags": [], "criteria": []}])
    assert text.splitlines() == [
        "- id: r0123456789ab",
        "  title: Brick house",
        "  description: A two-storey brick house.",
        "  tags: houses, brick",
        "  criteria: 'has a house' was PASS (10)",
        "- id: rfedcba987654",
        "  criteria: (none)",
    ]


def test_the_selection_prompt_carries_one_image_and_the_catalogue():
    from config import REFERENCE_SELECT_MAX_TOKENS
    from llm.prompts import build_reference_selection_prompt

    prompt = build_reference_selection_prompt(
        "Q0FORA==", "- id: r0123456789ab\n  criteria: (none)", ["has a house", "a roof"],
    )
    content = prompt["messages"][1]["content"]
    assert [p["type"] for p in content] == ["image_url", "text"]
    assert content[0]["image_url"]["url"].endswith("Q0FORA==")
    text = content[1]["text"]
    assert "'has a house', 'a roof'" in text
    assert "CATALOGUE:\n- id: r0123456789ab" in text
    assert '"matches"' in text and "an empty list is a valid answer" in text
    assert prompt["max_tokens"] == REFERENCE_SELECT_MAX_TOKENS
    assert prompt["response_format"] == {"type": "json_object"}


def test_the_selection_validator():
    from llm.validate import selection_validator

    validate = selection_validator(["r0000000000a1", "r0000000000b2", "r0000000000c3"])
    out = validate({"matches": [
        {"id": "r0000000000a1", "confidence": 40, "reason": "a bit"},
        {"id": "rnotinthepool", "confidence": 99},
        {"id": "r0000000000b2", "confidence": 250, "reason": "very"},
        {"id": "r0000000000a1", "confidence": 99, "reason": "duplicate — first wins"},
        {"id": "r0000000000c3", "confidence": "high"},
        "not an object",
    ]})
    assert out["matches"] == [
        {"id": "r0000000000b2", "confidence": 100, "reason": "very"},
        {"id": "r0000000000a1", "confidence": 40, "reason": "a bit"},
        {"id": "r0000000000c3", "confidence": 0, "reason": ""},
    ]
    assert out["dropped"] == ["rnotinthepool"]
    assert out["raw"]["matches"][1]["id"] == "rnotinthepool"
    assert validate({"matches": []})["matches"] == []
    for bad in ({}, {"matches": "r0000000000a1"}, ["x"]):
        with pytest.raises(ValueError):
            validate(bad)
