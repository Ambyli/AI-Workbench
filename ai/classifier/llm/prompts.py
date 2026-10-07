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
                            the previous attempts' rejections as feedback.
    build_verify_prompt() — the enforcement loop's check: the CROP alone, "is
                            <criterion> visible in this?", 1-10.
    build_describe_prompt() — a reference example's catalogue line: ONE page
                            image, "what is this, in a sentence or two" — no
                            criterion, no judgement.
    build_reference_selection_prompt() — ``references: "auto"``: ONE image (the
                            candidate page) and the text catalogue of the
                            candidate references; "which of these are useful
                            examples for this page?", ranked with confidences.
    _system_prompt()      — the shared system prompt for the two small calls,
                            carrying the ``Reasoning strength`` line.

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
themselves are a text catalogue, not images). Only a scoring call WITH
reference examples carries more — the example composite(s) first, then the
candidate — and never more than VISION_LLM_MAX_IMAGES_PER_PROMPT, which must
match muse-glimmer's ``--limit-mm-per-prompt`` (3: a PASS example, a FAIL
example and the candidate; ``analysis.llm_eval`` plans the calls to fit).
``build_llm_prompt(..., references=None)`` is byte-for-byte the plain prompt
— no examples, no extra system sentence — which a test pins by hash.

PREFIX CACHING. The scoring prompt puts everything that depends only on the
ITEM first — system prompt, page image, context line, DOCUMENT TEXT block —
and the per-criterion parts (rubric, ``CRITERION:``, instructions) last, so
every criterion scored on one page with the same text layer shares a
byte-identical prefix that muse-glimmer's ``--enable-prefix-caching`` serves
from cache. The text block used to sit after the criterion, which made vLLM
re-prefill up to ~15k tokens of identical text per criterion. Keep it first;
``classifier_llm_cached_prompt_tokens_total{kind="score"}`` shows the saving.

The rubric strings (HINT_RUBRICS) and the extracted-text block heading
(DOCUMENT_TEXT_HEADING) live in config.py § LLM prompt text, so prompt wording
can be tuned without touching the assembly logic here.

Process flow position: called by ``analysis.llm_eval`` (the scoring prompt),
``llm.boxes`` (the loop's two) and ``analysis.references`` (the describe
call); the result goes to ``llm.client``.
"""

import json
from dataclasses import dataclass
from typing import Optional

from config import (
    DOCUMENT_TEXT_HEADING,
    HINT_RUBRICS,
    LLM_BBOX_GRID,
    LLM_BBOX_MAX_TOKENS,
    REFERENCE_DESCRIBE_MAX_TOKENS,
    REFERENCE_DESCRIPTION_MAX_CHARS,
    REFERENCE_SELECT_MAX_TOKENS,
    VISION_LLM_MAX_TOKENS,
    VISION_LLM_MODEL,
    VISION_LLM_REASONING_STRENGTH,
)
from logger import logger

# The answer the scoring call is asked for, verbatim in the prompt.
SCORING_SCAFFOLD: dict = {"score": 0, "verdict": "...", "confidence": 0, "reason": "..."}

# The text block that separates the examples from what is being scored.
CANDIDATE_HEADING = "CANDIDATE — score only this image"

# Said once, beside the examples: what an example is for and what it is not.
REFERENCE_GUARD = (
    "The image(s) above are reference examples with known answers, shown so you know "
    "what this criterion means here. Do not reward resemblance the criterion does not "
    "ask about, and do not score the examples."
)


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


def build_llm_prompt(
    image_b64: str | None,
    name: str,
    hint: str = "auto",
    document_text: str = "",
    *,
    document_kind: str = "image",
    text_truncated: bool = False,
    references: Optional[list[ReferenceExample]] = None,
) -> dict:
    """Assemble the vLLM chat completion request for ONE criterion.

    The user text is laid out context line → DOCUMENT TEXT block → rubric →
    ``CRITERION:`` → instructions + answer scaffold, so system prompt, image,
    context and text form a prefix that is byte-identical for every
    criterion on the same item and text layer (vLLM prefix caching; see the
    comment at ``user_text``).

    Document handling:
      * ``document_text`` (already truncated to CLASSIFIER_TEXT_CHAR_BUDGET by
        the caller) is placed as a clearly-labelled block right after the
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

    Args:
        image_b64:      Base64-encoded JPEG of the (resized) page image, or
                        None when the document has none.
        name:           The criterion.
        hint:           "quality" | "presence" | "auto" — selects the rubric.
        document_text:  The text layer for this criterion ("" if none).
        document_kind:  "image" | "pdf" | "svg" | "txt" | "docx", for context.
        text_truncated: True when document_text was cut at the char budget.
        references:     Worked examples to show before the candidate.

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    logger.debug(
        "build_llm_prompt: '%s' hint=%s image_b64[%s] kind=%s text=%d chars "
        f"examples={len(references or [])}",
        name,
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
    if document_kind == "image":
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

    if document_text.strip():
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
    if VISION_LLM_REASONING_STRENGTH:
        # Muse Glimmer reads its reasoning depth from this system-prompt line
        # (see ai/vllm/VLLM.md "Parsers and sampling"). Other models ignore it.
        system_prompt += f"\nReasoning strength: {VISION_LLM_REASONING_STRENGTH}"

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

    return {
        "model": VISION_LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        # Budget covers reasoning + the JSON answer; reasoning is stripped
        # server-side by --reasoning-parser so `content` is JSON only.
        "max_tokens": VISION_LLM_MAX_TOKENS,
        # No temperature override: the server's --generation-config auto applies
        # Meta's published sampling for Muse Glimmer (temperature 1.0, top_p
        # 0.95, top_k 64). The model card warns against greedy / near-greedy
        # decoding. Consistency comes from json_object mode plus
        # llm.validate.clamp_answer().
        "response_format": {"type": "json_object"},
    }


def _system_prompt(instruction: str) -> str:
    """A system prompt for a single-question call, with the reasoning line.

    Muse Glimmer reads its reasoning depth from a system-prompt line rather
    than a chat-template kwarg (see ai/vllm/VLLM.md "Parsers and sampling"),
    and these calls are small enough that its budget matters — so the same
    directive the scoring prompt sets is repeated here. Other models ignore
    the line.
    """
    if VISION_LLM_REASONING_STRENGTH:
        return f"{instruction}\nReasoning strength: {VISION_LLM_REASONING_STRENGTH}"
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

    Args:
        image_b64: Base64 JPEG of the ONE page image (see the module
                   docstring — never a second image alongside it).
        name:      The criterion to locate.
        feedback:  One sentence per previous rejected attempt, newest last.
                   Empty on attempt 1.
        grid:      Grid span (default 1000).
        gridlines: The attached image carries a labelled grid every
                   ``grid_step`` units (common.vision.draw_grid_overlay).
        grid_step: Spacing of that grid, for the sentence that describes it.
        zoomed:    The attached image is a crop of a larger page.

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    span = int(grid)
    aids = ""
    if gridlines:
        aids += (
            f"\nA coordinate grid is drawn over the image: thin red lines every "
            f"{int(grid_step)} units, numbered along every edge. Read x off the numbers "
            "along the top and bottom, y off the numbers along the left and right, and "
            "interpolate between lines.\n"
        )
    if zoomed:
        aids += (
            "\nThis image is a zoomed-in crop of a larger document, and the feature "
            f"should be inside it. Answer on THIS image's 0 to {span} grid.\n"
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
        f"Locate this feature in the attached image: {name}\n\n"
        f"The image spans 0 to {span} in BOTH directions, whatever its real "
        "pixel size. Answer with the tightest rectangle that contains the "
        f"feature, as [x1, y1, x2, y2] with x1 < x2 and y1 < y2, every number "
        f"between 0 and {span}.\n"
        f"{retry_block}{aids}\n"
        "Rules:\n"
        f"  - A box covering the whole image is NOT an answer. Box the feature, "
        "not the photograph.\n"
        "  - If the feature is not visible in THIS image, answer with "
        '"bbox": null — do not guess a location.\n'
        "  - 'confidence' is 0-100: how sure you are that the box contains the "
        "feature.\n"
        "  - 'reason' names what you see inside the box, in one sentence.\n\n"
        "Return ONLY this JSON object:\n\n"
        f"{scaffold}"
    )

    prompt = {
        "model": VISION_LLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": _system_prompt(
                    "You locate features in images. You answer with coordinates "
                    "and nothing else. Return ONLY a valid JSON object."
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
        # Small budget: the answer is ~60 tokens of JSON. It is still four
        # figures because reasoning tokens are spent before it — a budget
        # exhausted mid-reasoning returns empty content, which call_vllm_json
        # treats as a parse failure.
        "max_tokens": LLM_BBOX_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
    logger.debug(
        "build_bbox_prompt: '%s' grid=%d feedback=%d line(s) gridlines=%s zoomed=%s",
        name, span, len(feedback or []), gridlines, zoomed,
    )
    return prompt


def build_verify_prompt(crop_b64: str, name: str) -> dict:
    """Ask whether ``name`` is visible in a CROP, with no other context.

    This is the half of the loop that makes a box mean something. A model
    that names a plausible region of a photo it has already been told
    contains solar panels is not evidence; a model that sees 300×300 pixels
    with no surroundings and still says "solar panels" is.

    The crop is the ONE image in this call — the page it came from is
    deliberately absent, because showing the page back would reintroduce
    exactly the context the check is trying to remove.

    Args:
        crop_b64: Base64 JPEG of the padded crop, taken from the ORIGINAL
                  page image (not the ≤1000-px working copy — the crop of a
                  downscaled page is too soft to judge).
        name:     The criterion being verified.

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    scaffold = json.dumps({"score": 0, "reason": "..."}, indent=2)
    user_text = (
        f"Is this visible in the attached image: {name}?\n\n"
        "The image is a close crop of a larger photo or document. Judge only "
        "what you can see here.\n\n"
        "Score 1-10:\n"
        "  10 = clearly and unmistakably visible\n"
        "   7 = visible\n"
        "   4 = something that might be it, but you are not sure\n"
        "   1 = not visible at all\n\n"
        "'reason' says what you actually see, in one sentence.\n\n"
        "Return ONLY this JSON object:\n\n"
        f"{scaffold}"
    )
    return {
        "model": VISION_LLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": _system_prompt(
                    "You say what is in an image crop. You judge only the crop "
                    "you are shown. Return ONLY a valid JSON object."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{crop_b64}"},
                    },
                    {"type": "text", "text": user_text},
                ],
            },
        ],
        "max_tokens": LLM_BBOX_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }


def build_describe_prompt(image_b64: str, *, document_kind: str = "image") -> dict:
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
    return {
        "model": VISION_LLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": _system_prompt(
                    "You write short, factual catalogue descriptions of images. "
                    "Return ONLY a valid JSON object."
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
        "max_tokens": REFERENCE_DESCRIBE_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }


def build_reference_selection_prompt(
    image_b64: str,
    catalogue: str,
    criteria: list[str],
    *,
    document_kind: str = "image",
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
    return {
        "model": VISION_LLM_MODEL,
        "messages": [
            {
                "role": "system",
                "content": _system_prompt(
                    "You match an image to stored reference examples. You rank catalogue "
                    "entries; you do not score the image. Return ONLY a valid JSON object."
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
        "max_tokens": REFERENCE_SELECT_MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }
