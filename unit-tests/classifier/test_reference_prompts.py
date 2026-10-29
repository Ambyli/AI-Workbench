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
  * the box loop's prompts: ``build_bbox_prompt`` and ``build_verify_prompt``
    without examples are BYTE-FOR-BYTE what they were before the loop could
    be shown any (``BBOX_GOLDEN`` / ``VERIFY_GOLDEN``, captured first); with
    them, the captions per verdict for a composite and a crop (a crop is
    "from another document"), the guard, the CANDIDATE wording, both
    system sentences, the guided verify's other-documents note and
    reason-first answer, and the content order (composites in the verify
    too);
  * ``plan_calls`` — contrastive pairing at three images, one example per
    call at two, the call cap; ``combine`` — any / all / mean;
  * ``box_selection`` / ``box_examples`` — what the loop is shown, on a stub
    ModelSpec: PASS + FAIL at three images, one PASS-side at two, none at
    one, a lone FAIL, FAIL only under a presence hint, no whole-page
    examples, the order; loads dropped (never raised); ``crop_b64``'s crop;
  * ``description_validator`` — the describe call's answer;
  * the ``auto`` selection — the catalogue entry and text, the one-image
    selection prompt, and ``selection_validator``.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from analysis.references import box_selection, combine, plan_calls
from config import DOCUMENT_TEXT_HEADING, HINT_RUBRICS
from llm.prompts import (
    BOX_CANDIDATE_HEADING,
    BOX_REFERENCE_GUARD,
    BOX_SYSTEM_SENTENCE,
    CANDIDATE_HEADING,
    VERIFY_CANDIDATE_HEADING,
    VERIFY_REFERENCE_NOTE,
    VERIFY_SYSTEM_SENTENCE,
    BoxExample,
    ReferenceExample,
    build_bbox_prompt,
    build_llm_prompt,
    build_verify_prompt,
)
from llm.validate import description_validator

# sha256 of json.dumps(build_llm_prompt(*args, **kwargs), sort_keys=True,
# ensure_ascii=False), on the registry DEFAULT model (models.yaml
# muse-glimmer). History: the two calls WITHOUT document text kept their
# hashes from before references were added; the two WITH text were
# regenerated when the DOCUMENT TEXT block moved ahead of the rubric and the
# criterion for vLLM prefix caching (and the presence rubric's "provided
# below" became "provided above").
#
# REGENERATED DELIBERATELY with the model registry (models.yaml): Muse
# Glimmer's reasoning depth moved from a "\nReasoning strength: low" line at
# the end of the system prompt to `chat_template_kwargs: {"reasoning_strength":
# "low"}` in the body. Its stock chat template reads that variable and writes
# "Reasoning strength: low." into the system text itself — and a reasoning
# line already IN the system prompt overrides the variable, so the line had
# to go. Nothing else changed: LEGACY below holds the previous hashes, and
# test_the_only_change_is_the_reasoning_mechanism rebuilds each old prompt
# from the new one (line put back, kwargs removed) and matches it exactly.
GOLDEN = [
    ((("ZmFrZQ==", "has a house"), {}),
     "727dd5d757b738fc128706a7cdaea3cbec2a057c2a36a945973a1c68822e037f"),
    ((("ZmFrZQ==", "has a house", "presence", "Total due $5"), {"document_kind": "pdf"}),
     "1f1e265d7a60ff94d6b12fee96bce8c2f226c6fab40ab0665e276f830d5baf0c"),
    (((None, "mentions a warranty", "auto", "Limited Warranty text"),
      {"document_kind": "txt", "text_truncated": True}),
     "6d27054ae72627a9d310b81e7542cadef95d55762bee850cd23df5a3503dda10"),
    ((("ZmFrZQ==", "image sharpness", "quality"), {}),
     "13cf4c1d61fb86423140e47d2e20139c40e8c58b21ff2da3ab5308cc3c4d8433"),
]

# The hashes before the registry (system-prompt reasoning line, no kwargs).
LEGACY = [
    "e577ab6fc567d7a15314ea972227b3d4c4066d1fb19964f14f74ac2b524c2249",
    "ff79fee2611e44ddcd6c16030c944c64c3bc3d67763b7a3cae1613d83a682f6e",
    "096e85e3692b0d506ccb57ae9fa89d190dda1edbb9b4f8cbfde00cafc038af1f",
    "08766fcc8c87747f99262f29aa162eae3c5b03755e370df3a291622c3810c20c",
]

# The box loop's two prompts, pinned the same way and generated from the
# code BEFORE the loop could be shown reference examples (ZT-527): the ask
# (plain, gridded, the zoomed refine, a retry with feedback) and the verify.
# Box bodies carry no confidence keys (``ModelSpec.body``), so no stripping.
_FEEDBACK = [
    "you answered bbox: null — you said the feature is not visible in this image",
    "your previous box [0, 0, 1000, 1000] covered 100% of the image — a full-frame "
    "box is not a location; try again, smaller",
]
BBOX_GOLDEN = [
    ((("ZmFrZQ==", "the utility's logo"), {}),
     "1fe9d673df1ef986c272c50dd18e0827e1a423a7606c8332024c14b25f9debe5"),
    ((("ZmFrZQ==", "the utility's logo"), {"gridlines": True, "grid_step": 100}),
     "17bb85f3533253e8930136410e0cc24e42ad13bc182851293f7aaf5be31cf88a"),
    ((("ZmFrZQ==", "the amount due"), {"gridlines": True, "grid_step": 100, "zoomed": True}),
     "be1f6f141362c265c5f7d0afde85d802e9d911835a418dcfa68ccf79335382b6"),
    ((("ZmFrZQ==", "the account number"),
      {"feedback": _FEEDBACK, "gridlines": True, "grid_step": 50}),
     "1fbf1f3206e5531990317592c30a280fcca24e851184163bde238c1908da4fd8"),
]
VERIFY_GOLDEN = [
    ((("ZmFrZQ==", "the utility's logo"), {}),
     "0210b15fa5b5cf05a6f4076e89e8561d05a85910773164d4e1ca1dd893f39e08"),
]


def _hash(prompt: dict) -> str:
    raw = json.dumps(prompt, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _without_confidence(prompt: dict) -> dict:
    """The prompt minus the keys the default model's ``confidence`` block adds
    to every SCORE body (models.yaml muse-glimmer: ``logprobs: true,
    top_logprobs: 20``), after checking they are exactly those. The hashes
    above predate the measured confidence and still pin everything else:
    the messages did not change, only the request asks for logprobs."""
    from llm.models import get_registry

    added = get_registry().default.confidence.score_body()
    assert {k: prompt[k] for k in added} == added
    return {k: v for k, v in prompt.items() if k not in added}


@pytest.mark.parametrize("call, digest", GOLDEN)
@pytest.mark.parametrize("references", ["omitted", None, []])
def test_no_references_is_byte_identical_to_before(call, digest, references):
    args, kwargs = call
    if references != "omitted":
        kwargs = {**kwargs, "references": references}
    assert _hash(_without_confidence(build_llm_prompt(*args, **kwargs))) == digest


@pytest.mark.parametrize("call, legacy", list(zip([c for c, _ in GOLDEN], LEGACY)))
def test_the_only_change_is_the_reasoning_mechanism(call, legacy):
    """The default model's prompt is the pre-registry prompt with exactly one
    change: no reasoning line in the system prompt, chat_template_kwargs in
    the body instead."""
    args, kwargs = call
    prompt = _without_confidence(build_llm_prompt(*args, **kwargs))
    assert "Reasoning strength" not in json.dumps(prompt)
    assert prompt.pop("chat_template_kwargs") == {"reasoning_strength": "low"}
    prompt["messages"][0]["content"] += "\nReasoning strength: low"
    assert _hash(prompt) == legacy


@pytest.mark.parametrize("call, digest", BBOX_GOLDEN)
@pytest.mark.parametrize("references", ["omitted", None, []])
def test_the_box_ask_without_examples_is_byte_identical_to_before(call, digest, references):
    args, kwargs = call
    if references != "omitted":
        kwargs = {**kwargs, "references": references}
    assert _hash(build_bbox_prompt(*args, **kwargs)) == digest


@pytest.mark.parametrize("call, digest", VERIFY_GOLDEN)
@pytest.mark.parametrize("references", ["omitted", None, []])
def test_the_verify_without_examples_is_byte_identical_to_before(call, digest, references):
    args, kwargs = call
    if references != "omitted":
        kwargs = {**kwargs, "references": references}
    assert _hash(build_verify_prompt(*args, **kwargs)) == digest


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
    # The guided system prompt is the plain one plus one sentence — and, like
    # it, carries no reasoning line (that is chat_template_kwargs now).
    assert guided["messages"][0]["content"].startswith(plain["messages"][0]["content"])
    assert "score ONLY the image marked CANDIDATE" in guided["messages"][0]["content"]
    assert "Reasoning strength" not in guided["messages"][0]["content"]
    for key in ("model", "max_tokens", "response_format", "chat_template_kwargs"):
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


# ── The box loop's guided prompts (ZT-527) ─────────────────────────────────

LOGO = "the utility's logo"


def _box(verdict="PASS", crop=False, image="UEFTUw==", criterion=LOGO):
    return BoxExample(criterion=criterion, verdict=verdict, image_b64=image, crop=crop)


def _parts(prompt: dict) -> list[dict]:
    return prompt["messages"][1]["content"]


def test_box_captions_per_verdict_for_a_composite_and_a_crop():
    assert _box().caption() == (
        "REFERENCE EXAMPLE — 'the utility's logo' was PASS here; the coloured box outlines it.")
    assert _box("MARGINAL").caption() == (
        "REFERENCE EXAMPLE — 'the utility's logo' was MARGINAL here, a borderline case; "
        "the coloured box outlines it.")
    assert _box("FAIL").caption() == (
        "REFERENCE EXAMPLE — 'the utility's logo' was FAIL here — the outlined thing is NOT "
        "the utility's logo; do not box it.")
    # A crop says it is from ANOTHER document and was PASS "there": a reference
    # is normally a different company's page (the composites keep "here").
    assert _box(crop=True).caption() == (
        "REFERENCE EXAMPLE (from another document) — a crop showing 'the utility's logo' "
        "(PASS there).")
    assert _box("MARGINAL", crop=True).caption() == (
        "REFERENCE EXAMPLE (from another document) — a crop showing 'the utility's logo' "
        "(MARGINAL there, a borderline case).")
    assert _box("FAIL", crop=True).caption() == (
        "REFERENCE EXAMPLE (from another document) — a crop of something that is NOT the "
        "utility's logo (FAIL there); do not box it.")
    for verdict in ("PASS", "MARGINAL", "FAIL"):
        crop = _box(verdict, crop=True).caption()
        # A crop has no outline drawn on it, so its caption never claims one.
        assert "coloured box" not in crop and "outlined" not in crop
        assert " here" not in crop and "(from another document)" in crop


def test_the_guided_ask_shows_examples_then_the_guard_then_the_candidate():
    plain = build_bbox_prompt("Q0FORA==", LOGO, gridlines=True, grid_step=100)
    guided = build_bbox_prompt("Q0FORA==", LOGO, gridlines=True, grid_step=100,
                               references=[_box(), _box("FAIL", image="RkFJTA==")])
    content = _parts(guided)
    assert [p["type"] for p in content] == [
        "text", "image_url", "text", "image_url", "text", "image_url", "text"]
    assert content[0]["text"] == _box().caption()
    assert content[1]["image_url"]["url"].endswith("UEFTUw==")
    assert content[2]["text"] == _box("FAIL").caption()
    assert content[3]["image_url"]["url"].endswith("RkFJTA==")
    # The guard's three points, then the heading, in one text part.
    guard = content[4]["text"]
    assert guard == f"{BOX_REFERENCE_GUARD.format(span=1000)}\n\n{BOX_CANDIDATE_HEADING}:"
    assert "shown so you know WHAT to box" in guard
    assert "it may be in a different place, so find it on the CANDIDATE image itself" in guard
    assert "The examples have no grid" in guard and "CANDIDATE image's 0 to 1000 grid" in guard
    assert '"bbox": null is still the answer' in guard
    assert "copy" not in guard   # the position is a fair hint on one layout
    # The candidate, then the usual text naming it.
    assert content[5]["image_url"]["url"].endswith("Q0FORA==")
    text = content[6]["text"]
    assert text.startswith(f"Locate this feature in the CANDIDATE image: {LOGO}\n\n")
    assert "The CANDIDATE image spans 0 to 1000 in BOTH directions" in text
    assert "A coordinate grid is drawn over the CANDIDATE image" in text
    assert "If the feature is not visible in the CANDIDATE image" in text
    assert "attached image" not in text and "THIS image" not in text
    # The system prompt is the plain one plus one sentence; nothing else moves.
    assert BOX_SYSTEM_SENTENCE == (
        "Reference examples come first; locate the feature ONLY in the image marked CANDIDATE.")
    assert guided["messages"][0]["content"] == (
        f"{plain['messages'][0]['content']} {BOX_SYSTEM_SENTENCE}")
    for key in ("model", "max_tokens", "response_format", "chat_template_kwargs"):
        assert guided[key] == plain[key]


def test_the_guided_refine_names_the_zoomed_candidate_and_keeps_the_feedback():
    feedback = ["your previous box [0, 0, 1000, 1000] covered 100% of the image — a "
                "full-frame box is not a location; try again, smaller"]
    plain = build_bbox_prompt("Q0FORA==", "the amount due", feedback=feedback,
                              gridlines=True, zoomed=True)
    guided = build_bbox_prompt("Q0FORA==", "the amount due", feedback=feedback,
                               gridlines=True, zoomed=True,
                               references=[_box(crop=True, criterion="the amount due")])
    content = _parts(guided)
    assert [p["type"] for p in content] == ["text", "image_url", "text", "image_url", "text"]
    assert content[0]["text"] == (
        "REFERENCE EXAMPLE (from another document) — a crop showing 'the amount due' "
        "(PASS there).")
    text, before = content[-1]["text"], _parts(plain)[-1]["text"]
    assert text.startswith("Locate this feature in the zoomed CANDIDATE image: the amount due")
    assert ("The CANDIDATE image is a zoomed-in crop of a larger document, and the feature "
            "should be inside it. Answer on the CANDIDATE image's 0 to 1000 grid.") in text
    # The rejection feedback — the retry's prompt — is the unguided block, verbatim.
    block = before[before.index("\n\nYour previous answer(s)"):
                   before.index("Give a DIFFERENT box this time.\n")]
    assert block in text and feedback[0] in text


def test_the_guided_verify_shows_example_crops_then_the_candidate_crop():
    plain = build_verify_prompt("Q0FORA==", LOGO)
    guided = build_verify_prompt(
        "Q0FORA==", LOGO,
        references=[_box(crop=True), _box("FAIL", crop=True, image="RkFJTA==")],
    )
    content = _parts(guided)
    assert [p["type"] for p in content] == [
        "text", "image_url", "text", "image_url", "text", "image_url", "text"]
    assert content[0]["text"] == _box(crop=True).caption()
    assert content[2]["text"] == _box("FAIL", crop=True).caption()
    assert content[4]["text"] == f"{VERIFY_CANDIDATE_HEADING}:"
    assert content[5]["image_url"]["url"].endswith("Q0FORA==")
    text, before = content[6]["text"], _parts(plain)[-1]["text"]
    assert text.startswith(f"Is this visible in the CANDIDATE crop: {LOGO}?\n\n")
    # The examples are from OTHER documents: the candidate will look different,
    # a name in the criterion is the candidate's own, and a difference from the
    # reference is no reason to score lower.
    note = VERIFY_REFERENCE_NOTE.format(name=LOGO)
    assert note == (
        "The reference image(s) above come from OTHER documents. Each shows an example of "
        "the kind of thing 'the utility's logo' means; on the CANDIDATE it will usually look "
        "different (a different company, design or layout), and any name in the criterion "
        "refers to the CANDIDATE's own document, not to the reference's. Do not score lower "
        "because the CANDIDATE looks different from a reference: score whether the CANDIDATE "
        "crop shows the utility's logo. A reference captioned FAIL shows something that does "
        "NOT count.")
    assert note in text and "looks like here" not in text
    # The 1-10 scale is the unguided one; the guided answer writes its reason first.
    scale = before[before.index("Score 1-10:"):before.index("'reason'")]
    assert scale in text
    assert ("Write 'reason' first, what you actually see in the CANDIDATE crop, in one "
            "sentence; then 'score'.") in text
    guided_scaffold = text[text.index("{"):]
    plain_scaffold = before[before.index("{"):]
    assert guided_scaffold == '{\n  "reason": "...",\n  "score": 0\n}'
    assert list(json.loads(guided_scaffold)) == ["reason", "score"]
    assert list(json.loads(plain_scaffold)) == ["score", "reason"]      # unguided unchanged
    assert VERIFY_SYSTEM_SENTENCE == (
        "Reference examples from other documents come first; judge ONLY the crop marked "
        "CANDIDATE.")
    assert guided["messages"][0]["content"] == (
        f"{plain['messages'][0]['content']} {VERIFY_SYSTEM_SENTENCE}")
    for key in ("model", "max_tokens", "response_format", "chat_template_kwargs"):
        assert guided[key] == plain[key]


def test_the_guided_verify_can_show_composites():
    """``verify_examples`` (the experiment's ``--verify-images composite``):
    the composites with their own captions, in the same order — examples,
    the CANDIDATE heading, the candidate crop, the question."""
    guided = build_verify_prompt(
        "Q0FORA==", LOGO, references=[_box(), _box("FAIL", image="RkFJTA==")],
    )
    content = _parts(guided)
    assert [p["type"] for p in content] == [
        "text", "image_url", "text", "image_url", "text", "image_url", "text"]
    assert content[0]["text"] == (
        "REFERENCE EXAMPLE — 'the utility's logo' was PASS here; the coloured box outlines it.")
    assert content[1]["image_url"]["url"].endswith("UEFTUw==")
    assert content[2]["text"] == _box("FAIL").caption()
    assert content[3]["image_url"]["url"].endswith("RkFJTA==")
    assert content[4]["text"] == f"{VERIFY_CANDIDATE_HEADING}:"
    assert content[5]["image_url"]["url"].endswith("Q0FORA==")
    assert content[6]["text"].startswith(f"Is this visible in the CANDIDATE crop: {LOGO}?")
    assert VERIFY_REFERENCE_NOTE.format(name=LOGO) in content[6]["text"]
    assert guided["messages"][0]["content"].endswith(VERIFY_SYSTEM_SENTENCE)


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


# ── box_selection / box_examples — what the box loop is shown ──────────────

_VERDICT = {"pass": "PASS", "marginal": "MARGINAL", "fail": "FAIL"}


def _framed(polarity, *, hint="presence", whole=False, rid=None):
    """A plan example (``references.resolve._example``'s shape) with a region,
    or a whole-page one."""
    return {
        "reference_id": rid or f"r-{polarity}",
        "title": None,
        "criterion": LOGO,
        "expected": {"verdict": _VERDICT[polarity], "score": 2 if polarity == "fail" else 9},
        "polarity": polarity,
        "region_source": "whole_page" if whole else "caller",
        "regions": [] if whole else [{"page": 0, "kind": "box",
                                      "points": [[100, 200], [500, 400]]}],
        "composite": "c.the-utilitys-logo-0a1b.jpg",
        "geometry": {"page": 0, "width": 1000, "height": 800, "working_scale": 1.0},
        "hint": hint,
    }


def _ids(chosen):
    return [e["reference_id"] for e in chosen]


def test_box_selection_three_images_is_one_pass_and_one_fail_in_that_order():
    chosen, note = box_selection(
        [_framed("fail"), _framed("marginal"), _framed("pass"), _framed("pass", rid="r-pass2")], 3)
    # PASS before MARGINAL for the one PASS-side slot, then the FAIL — not list order.
    assert _ids(chosen) == ["r-pass", "r-fail"] and note is None
    chosen, _ = box_selection([_framed("fail"), _framed("marginal")], 3)
    assert _ids(chosen) == ["r-marginal", "r-fail"]


def test_box_selection_two_images_is_one_pass_side_example():
    assert _ids(box_selection([_framed("fail"), _framed("marginal")], 2)[0]) == ["r-marginal"]
    assert _ids(box_selection([_framed("pass"), _framed("fail")], 2)[0]) == ["r-pass"]


def test_box_selection_one_image_is_none():
    # The submit refuses references for a model below two images
    # (api.schemas), so /assess cannot reach this — only the selector sees it.
    chosen, note = box_selection([_framed("pass")], 1)
    assert chosen == [] and "no room for an example" in note


def test_a_lone_fail_is_shown_at_three_images_only():
    chosen, note = box_selection([_framed("fail")], 3)
    assert _ids(chosen) == ["r-fail"] and note is None
    chosen, note = box_selection([_framed("fail")], 2)
    assert chosen == [] and "PASS-side example only" in note


def test_a_fail_is_shown_only_under_a_presence_hint():
    chosen, _ = box_selection([_framed("pass"), _framed("fail", hint="auto")], 3)
    assert _ids(chosen) == ["r-pass"]
    chosen, _ = box_selection([_framed("pass"), _framed("fail", hint="quality")], 3)
    assert _ids(chosen) == ["r-pass"]
    # A plan queued before `hint` existed has no key at all: not presence.
    old = _framed("fail")
    del old["hint"]
    chosen, note = box_selection([old], 3)
    assert chosen == [] and "hint is presence" in note


def test_whole_page_examples_never_reach_the_loop():
    chosen, note = box_selection([_framed("pass", whole=True), _framed("fail", whole=True)], 3)
    assert chosen == [] and "whole-page" in note
    chosen, _ = box_selection([_framed("pass", whole=True), _framed("marginal")], 3)
    assert _ids(chosen) == ["r-marginal"]


def _job_refs(monkeypatch, *, gone=(), no_page=()):
    """A JobReferences whose loads are scripted: ``gone`` raise the deleted-
    reference EvaluationError, ``no_page`` have no page.jpg."""
    from analysis.outcome import EvaluationError
    from analysis.references import JobReferences

    refs = JobReferences(plan={})

    async def image_b64(example):
        if example["reference_id"] in gone:
            raise EvaluationError(f"reference {example['reference_id']} was deleted after "
                                  "this job was submitted")
        return f"COMP-{example['reference_id']}"

    async def crop_b64(example):
        rid = example["reference_id"]
        return None if rid in no_page else f"CROP-{rid}"

    monkeypatch.setattr(refs, "image_b64", image_b64)
    monkeypatch.setattr(refs, "crop_b64", crop_b64)
    return refs


def _guide(*examples):
    from analysis.references import CriterionGuide

    return CriterionGuide(LOGO, "explicit", {}, list(examples))


def test_box_examples_composites_for_the_ask_crops_for_refine_and_verify(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    refs = _job_refs(monkeypatch)
    spec = SimpleNamespace(max_images_per_prompt=3)     # a stub ModelSpec
    bg = asyncio.run(refs.box_examples(_guide(_framed("fail"), _framed("pass")), spec,
                                       position_check=False))
    assert bg.ask_images == "composite" and bg.note is None
    assert [(e.verdict, e.crop, e.image_b64) for e in bg.ask] == [
        ("PASS", False, "COMP-r-pass"), ("FAIL", False, "COMP-r-fail")]
    assert [(e.verdict, e.crop, e.image_b64) for e in bg.crops] == [
        ("PASS", True, "CROP-r-pass"), ("FAIL", True, "CROP-r-fail")]
    assert bg.records == [
        {"reference_id": "r-pass", "criterion": LOGO, "polarity": "pass",
         "composite": "c.the-utilitys-logo-0a1b.jpg"},
        {"reference_id": "r-fail", "criterion": LOGO, "polarity": "fail",
         "composite": "c.the-utilitys-logo-0a1b.jpg"},
    ]
    # Under the position check the ask is shown the crops too.
    bg = asyncio.run(refs.box_examples(_guide(_framed("pass")), spec, position_check=True))
    assert bg.ask_images == "crop"
    assert [(e.crop, e.image_b64) for e in bg.ask] == [(True, "CROP-r-pass")]
    # Two images: one PASS-side example.
    bg = asyncio.run(refs.box_examples(_guide(_framed("fail"), _framed("pass")),
                                       SimpleNamespace(max_images_per_prompt=2),
                                       position_check=False))
    assert [e.verdict for e in bg.ask] == ["PASS"]


def test_box_examples_drops_what_cannot_be_loaded_and_never_raises(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    spec = SimpleNamespace(max_images_per_prompt=3)
    refs = _job_refs(monkeypatch, gone={"r-pass"}, no_page={"r-fail"})
    bg = asyncio.run(refs.box_examples(_guide(_framed("pass"), _framed("fail")), spec,
                                       position_check=False))
    assert bg.ask == [] and bg.crops == [] and bg.records == []
    assert bg.note.startswith("no example could be loaded for the box loop")
    assert "r-pass: reference r-pass was deleted after" in bg.note
    assert "r-fail: its page image (page.jpg) is missing" in bg.note
    # One lost of two: the other is shown at EVERY step, and the note says so.
    refs = _job_refs(monkeypatch, no_page={"r-fail"})
    bg = asyncio.run(refs.box_examples(_guide(_framed("pass"), _framed("fail")), spec,
                                       position_check=False))
    assert _ids(bg.records) == ["r-pass"] and len(bg.ask) == len(bg.crops) == 1
    assert bg.note.startswith("dropped from the box loop, could not be loaded — r-fail")
    # Nothing selectable: box_selection's note.
    bg = asyncio.run(refs.box_examples(_guide(_framed("pass", whole=True)), spec,
                                       position_check=False))
    assert bg.ask == [] and "whole-page" in bg.note


def test_crop_b64_cuts_the_first_region_out_of_the_original_page():
    """The real crop: page.jpg decoded, the first region's bounds padded by
    LLM_BBOX_CROP_PAD (``llm.boxes.example_crop``, whose working-size cap
    test_llm_boxes.py pins), cached per (reference, criterion); None when the
    page is gone or does not decode."""
    import asyncio
    import base64

    import cv2
    import numpy as np

    from analysis.references import JobReferences
    from config import LLM_BBOX_CROP_PAD
    from references.store import reference_files

    rid = "r00000000c0f1"
    page = np.full((3000, 4000, 3), 255, dtype=np.uint8)
    reference_files.write(rid, "page.jpg", cv2.imencode(".jpg", page)[1].tobytes())
    # A polygon (its bounds are cut), then a second region that is ignored.
    example = {**_framed("pass", rid=rid), "regions": [
        {"page": 0, "kind": "polygon",
         "points": [[1000, 1000], [1400, 1000], [1400, 1200], [1000, 1200]]},
        {"page": 0, "kind": "box", "points": [[0, 0], [4000, 3000]]},
    ]}
    refs = JobReferences(plan={})
    try:
        encoded = asyncio.run(refs.crop_b64(example))
        crop = cv2.imdecode(np.frombuffer(base64.b64decode(encoded), np.uint8), cv2.IMREAD_COLOR)
        # 400×200 padded by LLM_BBOX_CROP_PAD on each side, original pixels.
        assert crop.shape[:2] == (round(200 * (1 + 2 * LLM_BBOX_CROP_PAD)),
                                  round(400 * (1 + 2 * LLM_BBOX_CROP_PAD)))
        reference_files.delete_file(rid, "page.jpg")
        assert asyncio.run(refs.crop_b64(example)) == encoded          # cached
        assert asyncio.run(JobReferences(plan={}).crop_b64(example)) is None
        reference_files.write(rid, "page.jpg", b"not a jpeg")
        assert asyncio.run(JobReferences(plan={}).crop_b64(example)) is None
    finally:
        reference_files.delete(rid)


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
    from llm.models import get_registry
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
    assert prompt["max_tokens"] == get_registry().default.max_tokens["select"]
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
