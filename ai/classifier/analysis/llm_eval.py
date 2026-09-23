"""What the one LLM call gets to see.

ONE IMAGE PER PROMPT. The vision model (muse-glimmer) is served by vLLM
WITHOUT ``--limit-mm-per-prompt``, so a request may carry at most one image —
a second one fails the whole call. A 12-page PDF therefore contributes exactly
one page image to the prompt (plus all of its text), and choosing which page
that is, is a decision worth making rather than defaulting.

    _document_text_for_prompt() — the extracted text, truncated to
                                  CLASSIFIER_TEXT_CHAR_BUDGET.
    _pick_prompt_page()         — the one page image the model sees: the first
                                  page a text criterion matched on, else page 0.

The prompt wording itself is ``llm.prompts``; this module only decides what
goes into it.

Process flow position: step 6 of ``analysis.pipeline.analyze_document``.
"""

from common.documents import Document

from config import TEXT_CHAR_BUDGET
from logger import logger


def _document_text_for_prompt(doc: Document) -> tuple[str, bool]:
    """Extracted text for the LLM prompt, truncated to the configured budget.

    Returns:
        ``(text, truncated)`` — ``text`` is "" when the document has none.
    """
    text = doc.full_text()
    if TEXT_CHAR_BUDGET and len(text) > TEXT_CHAR_BUDGET:
        logger.info(
            "_document_text_for_prompt: truncating %d chars to the %d-char budget",
            len(text),
            TEXT_CHAR_BUDGET,
        )
        return text[:TEXT_CHAR_BUDGET], True
    return text, False


def _pick_prompt_page(
    working_images: list[tuple[int, object]], text_results: dict
) -> int | None:
    """Choose the ONE page image the vision model gets to see.

    The vision model (muse-glimmer) is served WITHOUT --limit-mm-per-prompt,
    so vLLM accepts at most one image per request — sending more fails the
    whole call. Until that flag is set (Phase 2), one page has to represent
    the document, so we pick the most informative one:

      1. The first page a text criterion actually matched on — if the caller
         asked about "Notice to Owner" and it's on page 3, page 3 is the page
         worth looking at.
      2. Otherwise page 0, the cover/first page.

    Args:
        working_images: ``[(page_index, image), ...]``; empty for txt/docx.
        text_results:   Per-criterion text results, to read matched pages from.

    Returns:
        The chosen page index, or None when the document has no images.
    """
    if not working_images:
        return None
    available = {index for index, _ in working_images}
    for result in text_results.values():
        detail = result.get("detail")
        if isinstance(detail, dict):
            for page_index in detail.get("pages", []) or []:
                if page_index in available:
                    return page_index
    return working_images[0][0]
