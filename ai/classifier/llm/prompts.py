"""The prompts: how a criterion is explained to the model.

Three asks, all of them here so their wording stays consistent with each
other:

    build_llm_prompt()    — the SCORING call: ONE criterion, the rubric for
                            its hint (from ``config.HINT_RUBRICS``), the text
                            layer that criterion's OCR settings produced, and
                            the ONE candidate page image — preceded, when the
                            request lists references, by one or two worked
                            EXAMPLES (``ReferenceExample``), each a captioned
                            image with its known answer. The answer is one
                            flat JSON object — score, verdict, confidence,
                            reason.
    ReferenceExample      — one example as the prompt needs it: a plain
                            dataclass, so ``llm`` never imports ``references``.
    build_bbox_prompt()   — the enforcement loop's "where is it?": one
                            criterion, one image, a box on a 0-1000 grid, and
                            the previous attempts' rejections as feedback —
                            preceded, behind the CLASSIFIER_REFERENCE_BOX_*
                            knobs, by up to two captioned EXAMPLES
                            (``BoxExample``) showing what to box.
    build_verify_prompt() — the enforcement loop's check: the CROP alone, "is
                            <criterion> visible in this?", 1-10 — or, behind
                            its knob, after example crops of the feature.
    BoxExample            — one example as the loop's prompts need it: a
                            composite or a region crop, with its verdict.
    build_describe_prompt() — a reference example's catalogue line: ONE page
                            image, "what is this, in a sentence or two" — no
                            criterion, no judgement.
    build_reference_selection_prompt() — ``references: "auto"``: ONE image (the
                            candidate page) and the text catalogue of the
                            candidate references; "which of these are useful
                            examples for this page?", ranked with confidences.
    _system_prompt()      — the shared system prompt for the small calls (plus
                            the model's reasoning line, for the rare
                            ``system_line`` model).

EVERY BUILDER TAKES ``model`` — the registry entry (``llm.models.ModelSpec``)
the request is for; None means the registry default. The entry supplies the
body's ``model`` (``served_name``), the token budget for the call kind under
its ``token_param``, ``response_format`` when ``json_mode``, its
``extra_body``, and its reasoning setting (``ModelSpec.body``). Reasoning
depth is normally a chat-template variable in the body — Muse Glimmer's
``chat_template_kwargs: {"reasoning_strength": "low"}`` — which its template
renders as "Reasoning strength: low." in the system text itself. The prompts
here therefore carry NO reasoning line for it: one in the system prompt would
override the variable. Only a model whose registry entry says
``reasoning.style: system_line`` (one with no template variable) gets a line,
appended verbatim.

A TEXT-ONLY model (``vision: false``) gets the scoring prompt without the
page image, worded for text ("You are assessing the text of a single
document page…"); the other builders always attach an image, and the submit
refuses to route a text-only model to them.

One criterion per scoring call, not all of them in one prompt: each criterion
is an independent unit of work (see ``analysis.scheduler``), so one that the
model cannot answer fails alone, a criterion's OCR settings decide what text
IT sees, and the answer needs no scaffold of keys the model could rename or
merge. The loop's small calls stay separate from the scoring call for the
same reason they always were — "and give me a box" would change the JSON the
scoring call has to produce.

IMAGES PER PROMPT. Every call is about ONE item (one page of one document),
and every call that is not reference-guided attaches exactly one image (or
none, for .txt / .docx): the plain scoring call the page, ``build_bbox_prompt``
that page (gridded) or a refine crop, ``build_verify_prompt`` the ONE crop,
``build_describe_prompt`` the reference page,
``build_reference_selection_prompt`` the candidate page (the references
themselves are a text catalogue, not images). Only a call WITH reference
examples carries more — the example image(s) first, then the candidate — and
never more than the model's ``max_images_per_prompt``, which must match its
server's ``--limit-mm-per-prompt`` (muse-glimmer: 3, a PASS example, a FAIL
example and the candidate). A guided scoring call shows composites
(``analysis.llm_eval`` plans the calls to fit); a guided box-loop call shows
at most one PASS-side and one FAIL example (``analysis.references.
box_selection``): the ask composites (region crops under the position check),
the refine and verify region crops.
``build_llm_prompt(..., references=None)``, ``build_bbox_prompt`` and
``build_verify_prompt`` without references are byte-for-byte the plain
prompts — no examples, no extra system sentence — which tests pin by hash.

PREFIX CACHING. The scoring prompt puts everything that depends only on the
ITEM first — system prompt, page image, context line, DOCUMENT TEXT block —
and the per-criterion parts (rubric, ``CRITERION:``, instructions) last, so
every criterion scored on one page with the same text layer shares a
byte-identical prefix that muse-glimmer's ``--enable-prefix-caching`` serves
from cache. The text block used to sit after the criterion, which made vLLM
re-prefill up to ~15k tokens of identical text per criterion. Keep it first;
``classifier_llm_cached_prompt_tokens_total{kind="score"}`` shows the saving.

The rubric strings (HINT_RUBRICS) and the extracted-text block heading
(DOCUMENT_TEXT_HEADING) live in config.py § LLM prompt text, and the wording
the examples add (captions, guards, headings, system sentences —
``REFERENCE_GUARD``, ``BOX_CAPTIONS`` and the rest) in config.py § Prompt
wording — the reference examples, so prompt wording can be tuned without
touching the assembly logic here. This module imports those names, so
``from llm.prompts import BOX_CANDIDATE_HEADING`` still works.
``SCORING_SCAFFOLD`` stays here: it is the answer contract ``llm.validate``
parses, not wording.

Process flow position: called by ``analysis.llm_eval`` (the scoring prompt),
``llm.boxes`` (the loop's two) and ``analysis.references`` (the describe
call); the result goes to ``llm.client``.
"""

import json
from dataclasses import dataclass
from typing import Optional

from config import (
    BOX_CANDIDATE_HEADING,
    BOX_CAPTIONS,
    BOX_REFERENCE_GUARD,
    BOX_SYSTEM_SENTENCE,
    CANDIDATE_HEADING,
    DOCUMENT_TEXT_HEADING,
    HINT_RUBRICS,
    LLM_BBOX_GRID,
    REFERENCE_DESCRIPTION_MAX_CHARS,
    REFERENCE_GUARD,
    VERIFY_CANDIDATE_HEADING,
    VERIFY_REFERENCE_NOTE,
    VERIFY_SYSTEM_SENTENCE,
)
from llm.models import ModelSpec, get_registry
from logger import logger

# The answer the scoring call is asked for, verbatim in the prompt. NOT
# wording, and deliberately not in config.py with the prompt text: it is the
# answer CONTRACT that llm/validate.py parses (find_answer looks for "score";
# clamp_answer reads score / confidence / reason and recomputes verdict).
# Renaming, adding or dropping a key here is a code change on both sides,
# and it changes the scoring prompt's golden hashes in
# unit-tests/classifier/test_reference_prompts.py.
SCORING_SCAFFOLD: dict = {"score": 0, "verdict": "...", "confidence": 0, "reason": "..."}


def _model(model: Optional[ModelSpec]) -> ModelSpec:
    """``model``, or the registry default when None."""
    return model if model is not None else get_registry().default


@dataclass(frozen=True)
class ReferenceExample:
    """One worked example for the scoring prompt.

    Attributes:
        criterion:  The reference criterion's name (the example's question).
        verdict:    Its expected verdict — PASS, MARGINAL or FAIL.
        score:      Its expected 1-10 score.
        reason:     The expected answer's reason ("" when none was given).
        image_b64:  The composite: the reference's working image with the
                    criterion's regions drawn on it.
        whole_page: True when no region was drawn — the whole image is the
                    example.
    """

    criterion: str
    verdict: str
    score: int
    reason: str
    image_b64: str
    whole_page: bool = False

    def caption(self) -> str:
        """The sentence shown just before this example's image."""
        where = (
            "the whole image is the example" if self.whole_page
            else "the coloured box outlines it"
        )
        why = f": {self.reason}" if self.reason else ""
        if self.verdict == "FAIL":
            return (
                f"REFERENCE EXAMPLE — in this reference '{self.criterion}' was FAIL "
                f"({self.score}) — an example of what does NOT satisfy the criterion"
                f"{why}; {where}."
            )
        if self.verdict == "MARGINAL":
            return (
                f"REFERENCE EXAMPLE — in this reference '{self.criterion}' was MARGINAL "
                f"({self.score}) — a borderline case{why}; {where}."
            )
        return (
            f"REFERENCE EXAMPLE — in this reference '{self.criterion}' was PASS "
            f"({self.score}){why}; {where}."
        )


@dataclass(frozen=True)
class BoxExample:
    """One example for the box loop's ask, refine or verify call.

    Only examples WITH a region reach the loop (a whole page teaches no
    location), chosen by ``analysis.references.box_selection``; like
    ``ReferenceExample`` it is plain data, so ``llm`` never imports
    ``references`` or ``analysis``.

    Attributes:
        criterion: The reference criterion's name (the example's question).
        verdict:   Its expected verdict — PASS, MARGINAL or FAIL.
        image_b64: The composite (the reference's working image with the
                   criterion's outline drawn on it) or, when ``crop``, the
                   reference's ORIGINAL page cut around its first region
                   (``llm.boxes.example_crop``), no outline drawn.
        crop:      True for a region crop — it selects the crop captions.
    """

    criterion: str
    verdict: str
    image_b64: str
    crop: bool = False

    def caption(self) -> str:
        """The sentence shown just before this example's image (``BOX_CAPTIONS``;
        a verdict outside the three reads as PASS, as ``ReferenceExample``'s
        does)."""
        template = BOX_CAPTIONS.get((self.verdict, self.crop)) or BOX_CAPTIONS[("PASS", self.crop)]
        return template.format(criterion=self.criterion)


def _example_parts(references: list[BoxExample]) -> list[dict]:
    """Each example as its caption then its image — the loop's guided prompts
    open with these, before the guard and the candidate."""
    parts: list[dict] = []
    for example in references:
        parts.append({"type": "text", "text": example.caption()})
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{example.image_b64}"},
            }
        )
    return parts


def build_llm_prompt(
    image_b64: str | None,
    name: str,
    hint: str = "auto",
    document_text: str = "",
    *,
    document_kind: str = "image",
    text_truncated: bool = False,
    references: Optional[list[ReferenceExample]] = None,
    model: Optional[ModelSpec] = None,
) -> dict:
    """Assemble the chat completion request for ONE criterion.

    The user text is laid out context line → DOCUMENT TEXT block → rubric →
    ``CRITERION:`` → instructions + answer scaffold, so system prompt, image,
    context and text form a prefix that is byte-identical for every
    criterion on the same item and text layer (vLLM prefix caching; see the
    comment at ``user_text``).

    Document handling:
      * ``document_text`` (already truncated to the model's
        ``text_char_budget`` by the caller) is placed as a clearly-labelled block right after the
        context line, ahead of the rubric and the criterion, and the system
        prompt tells the model it may use image and text together.
      * The candidate is at most ONE image. Pass None for a text-only
        document (.txt / .docx); the content array then holds text only and
        the response format is unchanged.
      * ``references`` (worked examples) come FIRST, each as its caption then
        its image, followed by the CANDIDATE heading and the candidate image,
        then the usual text — and one sentence in the system prompt. Only the
        candidate's text layer is ever sent. None or [] is byte-for-byte the
        prompt without references (see the module docstring).
      * A text-only ``model`` (``vision: false``) never gets the image: the
        page's text layer is all it is shown, and the wording says so. The
        caller refuses references for such a model at submit.

    Args:
        image_b64:      Base64-encoded JPEG of the (resized) page image, or
                        None when the document has none.
        name:           The criterion.
        hint:           "quality" | "presence" | "auto" — selects the rubric.
        document_text:  The text layer for this criterion ("" if none).
        document_kind:  "image" | "pdf" | "svg" | "txt" | "docx", for context.
        text_truncated: True when document_text was cut at the char budget.
        references:     Worked examples to show before the candidate.
        model:          The registry entry the request is for (None: default).

    Returns:
        A dict ready to POST to the model's chat-completions endpoint.
    """
    spec = _model(model)
    # A text-only model reads the text of an image page; the page itself is
    # not sent.
    text_only_page = bool(image_b64) and not spec.vision
    if text_only_page:
        image_b64 = None
    logger.debug(
        "build_llm_prompt: '%s' model=%s hint=%s image_b64[%s] kind=%s text=%d chars "
        f"examples={len(references or [])}",
        name,
        spec.id,
        hint,
        f"{len(image_b64)} chars" if image_b64 else "none",
        document_kind,
        len(document_text),
    )

    rubric_def = HINT_RUBRICS.get(hint) or HINT_RUBRICS["auto"]
    rubric = f"{rubric_def['heading']}:\n  {rubric_def['rubric']}"
    if rubric_def["extra"]:
        rubric += f"\n  {rubric_def['extra']}"

    # --- Document context: what the model is actually looking at ---
    if text_only_page:
        what = "photo" if document_kind == "image" else f"{document_kind} page"
        context_line = (
            f"You are assessing the text of a single document page (a {what}); the "
            "page image is not shown to you. Judge the criterion from the extracted "
            "text below."
        )
    elif document_kind == "image":
        context_line = "You are assessing a single image."
    elif image_b64:
        context_line = f"You are assessing a 1-page {document_kind} document."
    else:
        context_line = (
            f"You are assessing a {document_kind} document that has no page image. "
            "Judge the criterion from the extracted text below."
        )

    # --- Extracted text block (truncation already applied by the caller) ---
    text_block = ""
    if document_text.strip():
        truncation_note = (
            "\n[text truncated at the configured character budget]"
            if text_truncated
            else ""
        )
        text_block = (
            f"\n\n{DOCUMENT_TEXT_HEADING}:\n"
            "---\n"
            f"{document_text}{truncation_note}\n"
            "---\n"
        )

    scaffold = json.dumps(SCORING_SCAFFOLD, indent=2)
    # ORDER IS DELIBERATE — prefix caching. Everything up to the rubric
    # (system prompt, page image, context line, DOCUMENT TEXT block) depends
    # only on the item and the criterion's text layer, so every criterion on
    # the same page with the same text layer sends a byte-identical prefix and
    # muse-glimmer's --enable-prefix-caching serves it from cache instead of
    # re-prefilling up to ~15k tokens of text per criterion. Only the rubric,
    # the CRITERION line and the instructions differ. Do not move the text
    # block back below the criterion. (With reference examples the examples
    # precede the candidate, so the prefix differs per criterion anyway.)
    user_text = (
        f"{context_line}"
        f"{text_block}\n\n"
        f"{rubric}\n\n"
        f"CRITERION: {name}\n\n"
        "Score this one criterion. Return ONLY this JSON object, filled in — "
        "'score' is 1-10 on the rubric above, 'verdict' is PASS (7-10), "
        "MARGINAL (4-6) or FAIL (1-3), 'confidence' is 0-100, and 'reason' "
        "is one or two sentences of evidence:\n\n"
        f"{scaffold}"
    )

    if text_only_page:
        source_sentence = (
            "You are given one page of a document as extracted text only — you cannot "
            "see the page. Judge the criterion from that text; a criterion about how the "
            "page LOOKS cannot be judged from it, so say so and score it 5 (uncertain). "
            "The text may be OCR output and can contain recognition errors — judge "
            "meaning, not exact spelling. "
        )
    elif document_text.strip():
        # Both modalities are available: say so explicitly, and warn that the
        # text may be OCR output so the model treats near-misses sensibly.
        source_sentence = (
            "You are given a document as an image and as extracted text. Use BOTH: "
            "the image for anything visual (legibility, lighting, framing, stamps, "
            "signatures) and the text for anything about content (wording, amounts, "
            "dates, clauses). The text may be OCR output and can contain recognition "
            "errors — judge meaning, not exact spelling. "
            if image_b64
            else
            "You are given a document as extracted text only — it has no page image. "
            "Judge the criterion from that text. It may be OCR output and can "
            "contain recognition errors — judge meaning, not exact spelling. "
        )
    else:
        source_sentence = "Analyze the provided image and score it against the criterion. "

    system_prompt = (
        "You are a document assessment expert. "
        f"{source_sentence}"
        "Set confidence to a number 0-100: 0 = completely uncertain, 100 = completely certain. "
        "Return ONLY a valid JSON object."
    )
    if references:
        system_prompt += (
            " Reference examples with known answers come first; score ONLY the image "
            "marked CANDIDATE."
        )
    # Reasoning depth normally rides in the body (chat_template_kwargs, see
    # the module docstring); only a `system_line` model gets a line here.
    system_prompt = _system_prompt(system_prompt, spec)

    user_content: list[dict] = []
    for example in references or []:
        user_content.append({"type": "text", "text": example.caption()})
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{example.image_b64}"},
            }
        )
    if references:
        user_content.append(
            {"type": "text", "text": f"{REFERENCE_GUARD}\n\n{CANDIDATE_HEADING}:"}
        )
    if image_b64:
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
            }
        )
    user_content.append({"type": "text", "text": user_text})

    # The budget (models.yaml max_tokens.score) covers reasoning + the JSON
    # answer; reasoning is stripped server-side by --reasoning-parser so
    # `content` is JSON only. No temperature override: the server's
    # --generation-config auto applies the model's published sampling (Muse
    # Glimmer: temperature 1.0, top_p 0.95, top_k 64 — its card warns against
    # greedy decoding). Consistency comes from json_object mode plus
    # llm.validate.clamp_answer().
    return spec.body(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "score",
    )


def _system_prompt(instruction: str, model: Optional[ModelSpec] = None) -> str:
    """A system prompt, plus the model's reasoning line when it has one.

    Only a registry entry with ``reasoning.style: system_line`` (a model whose
    chat template has no reasoning variable) has a line. Muse Glimmer's depth
    is ``chat_template_kwargs.reasoning_strength`` in the body, which its
    template renders into the system text itself — a line here would OVERRIDE
    that variable, so it never gets one (``llm.models`` refuses the
    combination at load).
    """
    line = _model(model).reasoning_line
    if line:
        return f"{instruction}\n{line}"
    return instruction


def build_bbox_prompt(
    image_b64: str,
    name: str,
    *,
    feedback: list[str] | None = None,
    grid: float = LLM_BBOX_GRID,
    gridlines: bool = False,
    grid_step: int = 100,
    zoomed: bool = False,
    references: Optional[list[BoxExample]] = None,
    model: Optional[ModelSpec] = None,
) -> dict:
    """Ask for ONE criterion's bounding box on the attached page image.

    Two optional aids, both measured to matter (see config.LLM_BBOX_GRIDLINES):
    ``gridlines`` says a labelled coordinate grid is drawn on the attached
    image and tells the model to read positions off it; ``zoomed`` says the
    image is a crop of a larger page (the refine pass) and that the answer
    is on THIS image's grid. Neither changes the answer's shape.

    One criterion per call rather than all of them at once: a model that has
    to place eight boxes in one JSON object places them worse than a model
    asked about one thing, and a rejected box needs criterion-specific
    feedback on the retry, which a shared call cannot carry.

    The answer is requested on a 0-``grid`` square of the ATTACHED image —
    the ≤1000-px working page, the same pixels the scoring call saw. The grid
    is relative, so ``common.vision.grid_to_pixels`` converts it straight into
    ORIGINAL page pixels without the working scale ever appearing in the
    prompt.

    ``references`` (the ``CLASSIFIER_REFERENCE_BOX_ASK`` / ``_REFINE`` knobs,
    ``llm.boxes``) come FIRST, each as its caption then its image, followed by
    ``BOX_REFERENCE_GUARD``, the CANDIDATE heading and the candidate, then the
    usual text with "the attached image" / "THIS image" naming "the CANDIDATE
    image" ("the zoomed CANDIDATE image" on the refine pass), and
    ``BOX_SYSTEM_SENTENCE`` in the system prompt. They show the model WHAT to
    box — the loop's weakness is identification (a logo asked for, the
    company heading boxed), not placement. The feedback block is unchanged.
    None or [] is byte-for-byte the prompt without them (pinned by hash).

    Args:
        image_b64:  Base64 JPEG of the ONE page image (or refine crop) the
                    box is asked on — the only image unless ``references``.
        name:       The criterion to locate.
        feedback:   One sentence per previous rejected attempt, newest last.
                    Empty on attempt 1.
        grid:       Grid span (default 1000).
        gridlines:  The attached image carries a labelled grid every
                    ``grid_step`` units (common.vision.draw_grid_overlay).
        grid_step:  Spacing of that grid, for the sentence that describes it.
        zoomed:     The attached image is a crop of a larger page.
        references: Examples to show before the candidate — composites, or
                    region crops (the refine pass, and the ask under the
                    position check).

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    span = int(grid)
    # How the prompt names the image it asks about. Unguided these are the
    # exact strings it always had; with examples the candidate is one image
    # of several, so every sentence about it names it.
    if references:
        attached = "the zoomed CANDIDATE image" if zoomed else "the CANDIDATE image"
        this_image = grid_over = "the CANDIDATE image"
        spans, crop_is = "The CANDIDATE image spans", "The CANDIDATE image is"
    else:
        attached, this_image, grid_over = "the attached image", "THIS image", "the image"
        spans, crop_is = "The image spans", "This image is"
    aids = ""
    if gridlines:
        aids += (
            f"\nA coordinate grid is drawn over {grid_over}: thin red lines every "
            f"{int(grid_step)} units, numbered along every edge. Read x off the numbers "
            "along the top and bottom, y off the numbers along the left and right, and "
            "interpolate between lines.\n"
        )
    if zoomed:
        aids += (
            f"\n{crop_is} a zoomed-in crop of a larger document, and the feature "
            f"should be inside it. Answer on {this_image}'s 0 to {span} grid.\n"
        )
    scaffold = json.dumps(
        {"bbox": [0, 0, 0, 0], "confidence": 0, "reason": "..."}, indent=2
    )
    retry_block = ""
    if feedback:
        retry_block = (
            "\n\nYour previous answer(s) were rejected:\n"
            + "\n".join(f"  - {line}" for line in feedback)
            + "\nGive a DIFFERENT box this time.\n"
        )

    user_text = (
        f"Locate this feature in {attached}: {name}\n\n"
        f"{spans} 0 to {span} in BOTH directions, whatever its real "
        "pixel size. Answer with the tightest rectangle that contains the "
        f"feature, as [x1, y1, x2, y2] with x1 < x2 and y1 < y2, every number "
        f"between 0 and {span}.\n"
        f"{retry_block}{aids}\n"
        "Rules:\n"
        f"  - A box covering the whole image is NOT an answer. Box the feature, "
        "not the photograph.\n"
        f"  - If the feature is not visible in {this_image}, answer with "
        '"bbox": null — do not guess a location.\n'
        "  - 'confidence' is 0-100: how sure you are that the box contains the "
        "feature.\n"
        "  - 'reason' names what you see inside the box, in one sentence.\n\n"
        "Return ONLY this JSON object:\n\n"
        f"{scaffold}"
    )

    system_prompt = (
        "You locate features in images. You answer with coordinates "
        "and nothing else. Return ONLY a valid JSON object."
    )
    user_content: list[dict] = []
    if references:
        system_prompt += f" {BOX_SYSTEM_SENTENCE}"
        user_content.extend(_example_parts(references))
        user_content.append(
            {
                "type": "text",
                "text": f"{BOX_REFERENCE_GUARD.format(span=span)}\n\n{BOX_CANDIDATE_HEADING}:",
            }
        )
    user_content.append(
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
        }
    )
    user_content.append({"type": "text", "text": user_text})

    # Small budget (models.yaml max_tokens.box): the answer is ~60 tokens of
    # JSON. It is still four figures for a reasoning model because reasoning
    # tokens are spent before it — a budget exhausted mid-reasoning returns
    # empty content, which call_model_json treats as a parse failure.
    prompt = _model(model).body(
        [
            {"role": "system", "content": _system_prompt(system_prompt, model)},
            {"role": "user", "content": user_content},
        ],
        "box",
    )
    logger.debug(
        "build_bbox_prompt: '%s' grid=%d feedback=%d line(s) gridlines=%s zoomed=%s "
        "examples=%d",
        name, span, len(feedback or []), gridlines, zoomed, len(references or []),
    )
    return prompt


def build_verify_prompt(
    crop_b64: str,
    name: str,
    *,
    references: Optional[list[BoxExample]] = None,
    model: Optional[ModelSpec] = None,
) -> dict:
    """Ask whether ``name`` is visible in a CROP, with no other context.

    This is the half of the loop that makes a box mean something. A model
    that names a plausible region of a photo it has already been told
    contains solar panels is not evidence; a model that sees 300×300 pixels
    with no surroundings and still says "solar panels" is.

    The candidate crop is the only image of the CANDIDATE in this call — the
    page it came from is deliberately absent, because showing the page back
    would reintroduce exactly the context the check is trying to remove.

    ``references`` (the ``CLASSIFIER_REFERENCE_BOX_VERIFY`` knob,
    ``llm.boxes``) are region crops of OTHER pages — the reference's feature
    cut from its own original — shown first, each captioned, then the
    CANDIDATE heading, the candidate crop, and the question asked of "the
    CANDIDATE crop" with ``VERIFY_REFERENCE_NOTE``; ``VERIFY_SYSTEM_SENTENCE``
    joins the system prompt. An example crop is not page context: it says
    nothing about where the candidate crop came from, only what the feature
    looks like, which is the blind spot the verify has (shown only the
    company heading and asked about a logo, it can say yes). The 1-10
    contract is unchanged. None or [] is byte-for-byte the prompt without
    them (pinned by hash).

    Args:
        crop_b64:   Base64 JPEG of the padded crop, taken from the ORIGINAL
                    page image (not the ≤1000-px working copy — the crop of a
                    downscaled page is too soft to judge).
        name:       The criterion being verified.
        references: Example region crops to show before the candidate crop.

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    scaffold = json.dumps({"score": 0, "reason": "..."}, indent=2)
    if references:
        opening = (
            f"Is this visible in the CANDIDATE crop: {name}?\n\n"
            f"{VERIFY_REFERENCE_NOTE.format(name=name)}\n\n"
            "The CANDIDATE crop is a close crop of a larger photo or document. Judge "
            "only what you can see in it.\n\n"
        )
        seen = "'reason' says what you actually see in the CANDIDATE crop, in one sentence."
    else:
        opening = (
            f"Is this visible in the attached image: {name}?\n\n"
            "The image is a close crop of a larger photo or document. Judge only "
            "what you can see here.\n\n"
        )
        seen = "'reason' says what you actually see, in one sentence."
    user_text = (
        f"{opening}"
        "Score 1-10:\n"
        "  10 = clearly and unmistakably visible\n"
        "   7 = visible\n"
        "   4 = something that might be it, but you are not sure\n"
        "   1 = not visible at all\n\n"
        f"{seen}\n\n"
        "Return ONLY this JSON object:\n\n"
        f"{scaffold}"
    )
    system_prompt = (
        "You say what is in an image crop. You judge only the crop "
        "you are shown. Return ONLY a valid JSON object."
    )
    user_content: list[dict] = []
    if references:
        system_prompt += f" {VERIFY_SYSTEM_SENTENCE}"
        user_content.extend(_example_parts(references))
        user_content.append({"type": "text", "text": f"{VERIFY_CANDIDATE_HEADING}:"})
    user_content.append(
        {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{crop_b64}"},
        }
    )
    user_content.append({"type": "text", "text": user_text})
    return _model(model).body(
        [
            {"role": "system", "content": _system_prompt(system_prompt, model)},
            {"role": "user", "content": user_content},
        ],
        "box",
    )


def build_describe_prompt(
    image_b64: str, *, document_kind: str = "image", model: Optional[ModelSpec] = None,
) -> dict:
    """Ask for a short, neutral description of ONE reference page.

    The description is a catalogue line: it is what ``references: "auto"``
    reads to decide which stored examples are worth showing beside a
    candidate, so it names what the page IS and what is distinctive about it
    — never how good it is, and never an answer to a criterion (the
    reference's answers are stored separately, and a description that
    repeated them would bias the selection).

    Args:
        image_b64:     Base64 JPEG of the working page image — the one image.
        document_kind: "image" | "pdf" | "svg", for the opening sentence.

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    what = "photo or image" if document_kind == "image" else f"page of a {document_kind}"
    scaffold = json.dumps({"description": "..."}, indent=2)
    user_text = (
        f"Describe this {what} in one or two sentences for a catalogue of example "
        "documents: what kind of document or scene it is, and the visible features "
        "that set it apart (layout, notable objects, headings). Be factual and "
        "specific. Do not judge its quality and do not guess at anything you "
        f"cannot see. At most {REFERENCE_DESCRIPTION_MAX_CHARS} characters.\n\n"
        "Return ONLY this JSON object:\n\n"
        f"{scaffold}"
    )
    return _model(model).body(
        [
            {
                "role": "system",
                "content": _system_prompt(
                    "You write short, factual catalogue descriptions of images. "
                    "Return ONLY a valid JSON object.",
                    model,
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {"type": "text", "text": user_text},
                ],
            },
        ],
        "describe",
    )


def build_reference_selection_prompt(
    image_b64: str,
    catalogue: str,
    criteria: list[str],
    *,
    document_kind: str = "image",
    model: Optional[ModelSpec] = None,
) -> dict:
    """Ask which stored references are useful examples for ONE candidate page.

    The candidate page is the ONE image; the references are described in
    text (``references.render.catalogue_text``: id, title, description, tags,
    and each usable criterion with its expected verdict). The model ranks
    the references that show the same kind of document or scene — whose
    answers would teach it what the criteria mean HERE — with a 0-100
    confidence each. It does not score anything: the scoring calls come
    after, with the chosen examples' images.

    Args:
        image_b64:     Base64 JPEG of the candidate's working page image.
        catalogue:     The pool as catalogue text.
        criteria:      The names of the criteria the examples are for.
        document_kind: "image" | "pdf" | "svg", for the opening sentence.

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    what = "photo or image" if document_kind == "image" else f"{document_kind} page"
    names = ", ".join(f"'{n}'" for n in criteria)
    scaffold = json.dumps(
        {"matches": [{"id": "r0123456789ab", "confidence": 0, "reason": "..."}]}, indent=2
    )
    user_text = (
        f"The attached {what} is about to be assessed against these criteria: {names}.\n\n"
        "Below is a catalogue of stored reference examples, each with its known answers. "
        "Pick the references that show the SAME kind of document or scene as the attached "
        "image, so that their known answers are useful worked examples for judging it. A "
        "reference about a different kind of thing is not useful, however its answers "
        "read.\n\n"
        f"CATALOGUE:\n{catalogue}\n\n"
        "Rules:\n"
        "  - List only references that apply, best first; an empty list is a valid answer.\n"
        "  - 'id' is copied exactly from the catalogue.\n"
        "  - 'confidence' is 0-100: how sure you are the reference is a useful example here.\n"
        "  - 'reason' says in one sentence what the image and the reference have in common.\n\n"
        "Return ONLY this JSON object:\n\n"
        f"{scaffold}"
    )
    logger.debug(
        "build_reference_selection_prompt: %d criteria, catalogue %d chars",
        len(criteria), len(catalogue),
    )
    return _model(model).body(
        [
            {
                "role": "system",
                "content": _system_prompt(
                    "You match an image to stored reference examples. You rank catalogue "
                    "entries; you do not score the image. Return ONLY a valid JSON object.",
                    model,
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                    {"type": "text", "text": user_text},
                ],
            },
        ],
        "select",
    )
