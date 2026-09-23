"""The prompts: how a criterion is explained to the model.

Three asks, all of them here so their wording stays consistent with each
other:

    _build_scaffold()     — a pre-filled JSON response template with the
                            criterion names as keys, so the model cannot
                            invent, merge, or rename one.
    build_llm_prompt()    — the SCORING call: criteria grouped by hint, one
                            rubric section per group (from
                            ``config.HINT_RUBRICS``), the document's extracted
                            text, and at most ONE page image.
    build_bbox_prompt()   — the enforcement loop's "where is it?": one
                            criterion, one image, a box on a 0-1000 grid, and
                            the previous attempts' rejections as feedback.
    build_verify_prompt() — the enforcement loop's check: the CROP alone, "is
                            <criterion> visible in this?", 1-10.
    _system_prompt()      — the shared system prompt for the two small calls,
                            carrying the ``Reasoning strength`` line.

The loop itself is ``llm.boxes``; only the wording lives here, next to the
scoring prompt it has to stay consistent with. Both small calls are separate
from the scoring call ON PURPOSE: folding "and give me a box" into the scoring
prompt would change the JSON the model has to produce for every criterion on
every job, which is the one prompt in this service that is tuned and working.

ONE IMAGE PER PROMPT. The vision model (muse-glimmer) is served by vLLM
WITHOUT ``--limit-mm-per-prompt``, which means a request may contain at most
one image — a second one fails the whole call. ``build_llm_prompt`` therefore
takes a single ``image_b64`` (or None, for a text-only document) no matter how
many pages the document has; ``analysis.llm_eval`` decides which page that is.
The same rule binds the loop: ``build_bbox_prompt`` attaches the ONE page
image the scoring call used, and ``build_verify_prompt`` attaches the ONE crop
— never the page and the crop together. Phase 2: set
``--limit-mm-per-prompt image=N`` on the vLLM container, then these functions
can take a list.

The rubric strings (HINT_RUBRICS) and the extracted-text block heading
(DOCUMENT_TEXT_HEADING) live in config.py § LLM prompt text, so prompt wording
can be tuned without touching the assembly logic here.

Process flow position: called by ``analysis.pipeline`` (the scoring prompt)
and ``llm.boxes`` (the other two); the result goes to ``llm.client``.
"""

import json

from config import (
    DOCUMENT_TEXT_HEADING,
    HINT_RUBRICS,
    LLM_BBOX_GRID,
    LLM_BBOX_MAX_TOKENS,
    VISION_LLM_MAX_TOKENS,
    VISION_LLM_MODEL,
    VISION_LLM_REASONING_STRENGTH,
)
from api.schemas import CriterionInput
from logger import logger


def _build_scaffold(criteria: list[CriterionInput]) -> str:
    """Build a pre-filled JSON response template with criterion names as keys.

    Pre-defining the keys prevents the LLM from grouping or renaming criteria.
    Only the criteria passed in are included — CV-resolved criteria are handled
    before this is called and are excluded from the LLM prompt.

    Args:
        criteria: The LLM-bound criteria for this request.

    Returns:
        A JSON string with 0-valued placeholders for the model to fill in.
    """
    per_criterion = {
        c.name: {"score": 0, "verdict": "...", "confidence": 0, "reason": "..."}
        for c in criteria
    }
    return json.dumps(
        {
            "assessment": {
                "overall_verdict": "...",
                "overall_score": 0,
                "per_criterion_scores": per_criterion,
            }
        },
        indent=2,
    )


def build_llm_prompt(
    image_b64: str | None,
    criteria: list[CriterionInput],
    document_text: str = "",
    *,
    document_kind: str = "image",
    page_index: int | None = None,
    page_count: int = 1,
    text_truncated: bool = False,
) -> dict:
    """Assemble the full vLLM chat completion request for a set of criteria.

    All criteria passed here are LLM-bound (type="llm", or type="cv" with no
    matching detector).  A unified rubric is used — the LLM infers from the
    criterion name whether to score quality or detect presence:
      - Quality criteria (e.g. "image sharpness"): score 1-10 for quality level.
      - Presence criteria (e.g. "has solar panels"): 10=present, 5=uncertain, 1=absent.

    The prompt applies four reliability improvements:
      1. Pre-filled scaffold    — criterion keys defined in advance.
      2. Explicit key list      — reinforces expected keys.
      3. "Do not group" rule    — system prompt forbids merging criteria.
      4. Verification step      — model self-checks before responding.

    Document handling:
      * ``document_text`` (already truncated to CLASSIFIER_TEXT_CHAR_BUDGET by
        the caller) is appended as a clearly-labelled block, and the system
        prompt tells the model it may use image and text together.
      * At most ONE image is attached — vLLM rejects multi-image requests
        while muse-glimmer runs without ``--limit-mm-per-prompt``. Pass None
        for a text-only document (.txt / .docx); the content array then holds
        text only and the JSON response format is unchanged.

    Args:
        image_b64:      Base64-encoded JPEG of one (resized) page image, or
                        None when the document has no images.
        criteria:       LLM-bound CriterionInput objects.
        document_text:  Extracted text for the whole document ("" if none).
        document_kind:  "image" | "pdf" | "txt" | "docx", for context.
        page_index:     Which page the attached image came from (0-based).
        page_count:     How many pages the document has in total.
        text_truncated: True when document_text was cut at the char budget.

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    logger.debug(
        "build_llm_prompt: image_b64[%s] kind=%s page=%s/%s text=%d chars criteria=%s hints=%s",
        f"{len(image_b64)} chars" if image_b64 else "none",
        document_kind,
        page_index,
        page_count,
        len(document_text),
        [c.name for c in criteria],
        {c.name: c.hint for c in criteria},
    )

    # --- Group criteria by hint and emit one rubric section per group ---
    # Ordering: quality → presence → auto, so explicit hints come first.
    hint_order = ["quality", "presence", "auto"]
    sections = []
    for hint_val in hint_order:
        group = [c for c in criteria if c.hint == hint_val]
        if not group:
            continue
        rubric_def = HINT_RUBRICS[hint_val]
        names = "\n".join(f"  - {c.name}" for c in group)
        section = f"{rubric_def['heading']}:\n  {rubric_def['rubric']}"
        if rubric_def["extra"]:
            section += f"\n  {rubric_def['extra']}"
        section += f"\n{names}"
        sections.append(section)
    criteria_text = "\n\n".join(sections)

    # --- Improvement 1: pre-filled scaffold ---
    scaffold = _build_scaffold(criteria)

    # --- Improvement 2: explicit key list ---
    key_list = ", ".join(f'"{c.name}"' for c in criteria)
    n = len(criteria)

    # --- Document context: what the model is actually looking at ---
    # A one-line header so the model knows whether the attached image is the
    # whole document or one page of many (and that the rest is in the text).
    if document_kind == "image":
        context_line = "You are assessing a single image."
    elif image_b64 and page_count > 1:
        context_line = (
            f"You are assessing a {page_count}-page {document_kind} document. "
            f"The attached image is page {(page_index or 0) + 1} of {page_count}; "
            "the extracted text below covers every page."
        )
    elif image_b64:
        context_line = f"You are assessing a 1-page {document_kind} document."
    else:
        context_line = (
            f"You are assessing a {document_kind} document that has no page images. "
            "Judge every criterion from the extracted text below."
        )

    # --- Extracted text block (truncation already applied by the caller) ---
    text_block = ""
    if document_text.strip():
        truncation_note = (
            "\n[text truncated at the configured character budget — later pages "
            "may be missing]"
            if text_truncated
            else ""
        )
        text_block = (
            f"\n\n{DOCUMENT_TEXT_HEADING}:\n"
            "---\n"
            f"{document_text}{truncation_note}\n"
            "---\n"
        )

    # --- Full user message (improvements 1, 2, and 4) ---
    user_text = (
        f"{context_line}\n\n"
        f"{criteria_text}"
        f"{text_block}\n\n"
        "Fill in the following JSON structure. "
        "The keys in per_criterion_scores are already defined — "
        "do NOT change, rename, merge, or add any keys:\n\n"
        f"{scaffold}\n\n"
        f"Required keys in per_criterion_scores ({n} total): {key_list}\n\n"
        # Improvement 4: self-verification step
        f"Before returning, verify your JSON contains exactly those {n} keys in "
        "per_criterion_scores — no more, no fewer, with names spelled exactly as shown. "
        "If any key is missing or renamed, revise before responding."
    )

    # --- System prompt (improvement 3: do-not-group rule) ---
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
            "You are given a document as extracted text only — it has no page images. "
            "Judge every criterion from that text. It may be OCR output and can "
            "contain recognition errors — judge meaning, not exact spelling. "
        )
    else:
        source_sentence = "Analyze the provided image and score it against each criterion listed below. "

    system_prompt = (
        "You are a document assessment expert. "
        f"{source_sentence}"
        "Score each criterion independently — do NOT group multiple criteria under a "
        "single key or summarise them together. "
        "Set confidence to a number 0-100: 0 = completely uncertain, 100 = completely certain. "
        "Return ONLY a valid JSON object."
    )
    if VISION_LLM_REASONING_STRENGTH:
        # Muse Glimmer reads its reasoning depth from this system-prompt line
        # (see ai/vllm/VLLM.md "Parsers and sampling"). Other models ignore it.
        system_prompt += f"\nReasoning strength: {VISION_LLM_REASONING_STRENGTH}"

    # ONE image maximum — see the module docstring. A text-only document
    # (.txt / .docx, or an image-less request) sends a text-only content array,
    # which vLLM accepts from a multimodal model without complaint.
    user_content: list[dict] = []
    if image_b64:
        user_content.append(
            # Embed the image as a data URI so vLLM can process it
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
            }
        )
    user_content.append({"type": "text", "text": user_text})

    prompt = {
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
        # decoding, so the old 0.1 is deliberately gone. Consistency comes from
        # the JSON schema constraint below plus validate_and_clamp().
        "response_format": {"type": "json_object"},  # forces valid JSON output
    }
    logger.debug(
        "build_llm_prompt: returning prompt — %d llm criteria, scaffold keys=%s",
        len(criteria),
        [c.name for c in criteria],
    )
    return prompt


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
) -> dict:
    """Ask for ONE criterion's bounding box on the attached page image.

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

    Returns:
        A dict ready to POST to the vLLM /v1/chat/completions endpoint.
    """
    span = int(grid)
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
        f"{retry_block}\n"
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
        "build_bbox_prompt: '%s' grid=%d feedback=%d line(s)",
        name, span, len(feedback or []),
    )
    return prompt


def build_verify_prompt(crop_b64: str, name: str) -> dict:
    """Ask whether ``name`` is visible in a CROP, with no other context.

    This is the half of the loop that makes a box mean something. A model
    that names a plausible region of a photo it has already been told
    contains solar panels is not evidence; a model that sees 300×300 pixels
    with no surroundings and still says "solar panels" is.

    The crop is the ONE image in this call — the page it came from is
    deliberately absent, both because vLLM accepts one image per request and
    because showing the page back would reintroduce exactly the context the
    check is trying to remove.

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
