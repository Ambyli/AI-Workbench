# Classifier API

FastAPI service that assesses **documents** — JPEG/PNG photos, PDFs (native or
scanned), plain text, and .docx — via OpenCV detectors, deterministic text
matching, and vision-LLM scoring. The LLM is Meta's Muse-Glimmer-30B served by the
`muse-glimmer` vLLM container (`VISION_LLM_API` / `VISION_LLM_MODEL` /
`VISION_LLM_MAX_TOKENS` in the `## Classifier` block of `.env`); see [VLLM.md § Muse Glimmer 30B](../vllm/VLLM.md#muse-glimmer-30b--tensor-parallel--dflash-speculative-decoding).
Muse Glimmer's reasoning depth is set per call from `VISION_LLM_REASONING_STRENGTH`
(default `low`, driven by `CLASSIFIER_REASONING_STRENGTH` in `.env`); its thinking
is stripped server-side by the `muse_glimmer` reasoning parser, so only the JSON
answer reaches the classifier.

Base URL (direct): `http://<host>:8005`
Base URL (via LiteLLM passthrough): `http://<host>:4001/v1/classifier`

All passthrough requests require `Authorization: Bearer <master-key>`.

---

## Async job pattern

`POST /assess` and `POST /assess/compare` return **202 Accepted** immediately
with a job ID. Poll `GET /jobs/{job_id}` until `phase` is `"completed"` or
`"failed"`, then read the `result` field.

```
POST /assess  →  {"job_id": "abc123", "phase": "pending"}
                         ↓  poll
GET /jobs/abc123  →  {"phase": "completed", "result": {...}, "metadata": {...}}
```

Job tracking is powered by the shared `common.jobs` package
(`shared/common/src/common/jobs/`), which the interceptor service also
uses. The endpoint shapes and response fields are documented there.

### Concurrency and durability

The jobs table **is** the queue. `CLASSIFIER_MAX_CONCURRENT` worker tasks
(default `2`, set in `.env`) each atomically claim the oldest `pending` row and
run it, so at most that many jobs are analysed at once and a burst of
submissions drains at that rate. Size it to the vision model: a compare job
with N live examples issues N+1 LLM calls of its own, and `muse-glimmer` runs
`--max-num-seqs 4`, so anything past four in-flight requests queues inside
vLLM rather than running in parallel. Set it to `1` for the
old strictly-serial behaviour.

Phases: `staging` → `pending` → `processing` → `completed` | `failed`.
`staging` is the few milliseconds between the row being created and its
payload landing on disk; workers never claim it.

Job inputs (image bytes + criteria, or the full compare request) are written
to `PAYLOAD_DIR` (default `/data/payloads`, same volume as the DB) and deleted
when the job reaches a terminal phase. On startup the service requeues any
`processing` rows a previous container left behind and sweeps orphaned payload
files, so a restart mid-job re-runs the job rather than losing it.

Metrics: `classifier_job_queue_depth` (rows in `pending`) and
`classifier_jobs_in_flight` (rows being processed, never above
`CLASSIFIER_MAX_CONCURRENT`).

---

## Documents

Every upload — a phone photo, a 12-page PDF, a .txt, a .docx — is normalised by
[`common.documents`](../../shared/common/src/common/documents/__init__.py) into
a list of **pages**, each of which may carry an image, a text layer, or both.
Criteria then run against whichever of those they need, so the same criteria
list works across kinds.

| Kind | Extensions | Detected by | Pages | Page images | Native text |
|---|---|---|---|---|---|
| `image` | `.jpg` `.jpeg` `.png` | magic bytes `FF D8 FF` / `89 50 4E 47 0D 0A 1A 0A` | 1 | yes (EXIF-rotated) | no — needs OCR |
| `pdf` | `.pdf` | `%PDF-` in the first 1 KB | up to `CLASSIFIER_DOC_MAX_PAGES` (20) | yes, rendered at `CLASSIFIER_PDF_RENDER_DPI` (150) | yes when the PDF is digital; a scan has none |
| `txt` | `.txt` | decodes as UTF-8 (BOM ok), no NUL bytes, mostly printable | 1 | **no** | yes |
| `docx` | `.docx` | ZIP magic `PK\x03\x04` containing `word/document.xml` | 1 | **no** | yes (paragraphs + table cells in document order) |

**Detection is by content, never by filename or `Content-Type`.** A PDF
uploaded as `image/png` still loads as a PDF. The declared content type is only
used for an early allowlist reject (`image/jpeg`, `image/png`,
`application/pdf`, `text/plain`, the .docx type, `application/octet-stream`, and
`application/msword` — the last one only so a legacy `.doc` reaches the
magic-byte check and gets its specific "convert to .docx" 400). The kind is
detected on the `POST /assess` request itself, so an unsupported file is a 400
at submit time and never becomes a job.

`GET /document-kinds` returns this table live from a running container, plus
the OCR engine's actual availability.

### Limitations

* **Legacy `.doc` is not supported.** An OLE2 file (`D0 CF 11 E0 A1 B1 1A E1`)
  is rejected with HTTP 400 telling the caller to convert to `.docx`. There is
  no server-side conversion step.
* **`.txt` and `.docx` have no page images**, so `cv` criteria against them are
  marked `SKIPPED` (`method: "skipped"`) and excluded from the weighted score
  — "not applicable", not "failed". The LLM call for those kinds is text-only.
* **Only PDFs are multi-page.** python-docx reads XML, not a laid-out page, so
  a .docx collapses to one page.
* **PDF pages past the cap are dropped**, with the count reported in
  `document_info.truncated_pages`.
* **One image per LLM prompt** — see below.

### OCR

Scans and photos have no text layer, so a `text` criterion would have nothing
to search and the vision model would have to read pixels alone. The bundled
OCR engine (RapidOCR / PP-OCRv6 ONNX, `CLASSIFIER_OCR_ENGINE=rapidocr`, models
baked into the image at build time) fills that gap.

| `ocr` | Behaviour |
|---|---|
| `auto` (default) | Recognise only when text is **wanted and missing**: some page carries an image whose native text is under `CLASSIFIER_OCR_MIN_NATIVE_CHARS` (20), *and* the request has a `text` or `llm` criterion. A native PDF / .txt / .docx therefore never loads the models. |
| `always` | Recognise every page image, replacing any native text layer. |
| `never` | No OCR. `text` criteria against an image-only document score 1/FAIL with `"No text available for this document…"`. |

Set it as the `ocr` form field on `/assess` or the `ocr` body field on
`/assess/compare`; it applies to the subject and every live example.
`CLASSIFIER_OCR_ENGINE=none` disables OCR deployment-wide regardless of the
request (`GET /document-kinds` reports `ocr.available: false`).

Per-page provenance comes back in `document_info.pages[*].text_source`
(`native` | `ocr` | `none`) with `ocr_confidence` (0–1) for recognised pages.
A `text` criterion's `confidence` is 100 for native text, the recogniser's
confidence ×100 for OCR'd text, and 0 when there is no text at all.

### What the vision model sees

The LLM call carries the document's extracted text — a
`DOCUMENT TEXT (extracted, may contain OCR errors)` block, truncated at
`CLASSIFIER_TEXT_CHAR_BUDGET` (60 000 characters) with a note when truncated —
plus **at most one page image**.

> **One image per prompt.** `muse-glimmer` is served **without**
> `--limit-mm-per-prompt`, so vLLM accepts a single image per request; a second
> one fails the whole call. The page sent is the first page a `text` criterion
> matched on, else page 0 — reported as `document_info.llm_image_page`.
> **Phase 2:** set `--limit-mm-per-prompt image=N` on the vLLM container and
> the prompt builder can send one image per page.

A document with no page images sends a text-only prompt (still
`response_format: json_object`), and the system prompt tells the model to judge
from the text.

---

## `CriterionInput` — shared input object

Used in both `/assess` (as a JSON array string) and `/assess/compare` (as a
JSON array in the request body).

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | — | The criterion to evaluate. Free-form text — the LLM interprets any name; for `type: "text"` it doubles as the default `pattern`. |
| `type` | `"cv"` \| `"llm"` \| `"text"` | `"llm"` | `"llm"`: scored by the vision LLM (one page image + the extracted text). `"cv"`: run through a registered OpenCV detector by name; falls back to LLM if no detector matches; `SKIPPED` when the document has no page images. `"text"`: deterministic search of the document's text layer — no tokens. |
| `weight` | float (> 0) | `1.0` | Relative weight in the combined weighted score. Higher = matters more. |
| `hint` | `"quality"` \| `"presence"` \| `"auto"` | `"auto"` | Tells the LLM which scoring rubric to use. `"quality"`: score quality 1-10. `"presence"`: detect presence/absence. `"auto"`: LLM infers from the criterion name. Ignored for `cv` criteria that match a detector and for `text` criteria. |
| `depends_on` | string \| null | `null` | Name of another criterion that must **PASS** (score ≥ 7) before this criterion is evaluated. If the dependency does not pass — including if it was itself skipped — this criterion is marked `SKIPPED` and excluded from the weighted score. Chains are supported: A → B → C all skip if A fails. |
| `pattern` | string \| null | `null` → `name` | **`type: "text"` only.** What to search for. Literal text for `contains`/`exact`/`fuzzy`; a Python regular expression for `regex`. Max 500 characters. |
| `match` | `"contains"` \| `"exact"` \| `"regex"` \| `"fuzzy"` | `"contains"` | **`type: "text"` only.** How `pattern` is matched — see the table below. |
| `case_sensitive` | bool | `false` | **`type: "text"` only.** Case-fold both sides when false. |
| `fuzzy_threshold` | float 0–1 | `0.85` | **`type: "text"` only.** Similarity a sliding window must reach to count as a hit in `fuzzy` mode. |
| `min_count` | int ≥ 1 | `1` | **`type: "text"` only.** How many hits are required before the criterion PASSes. |

### `text` criteria

Deterministic, free (no tokens), and exact about *where* a phrase was found.
The text searched is the whole document's text layer — native or OCR'd.

| `match` | Semantics | Use it for |
|---|---|---|
| `contains` | Substring anywhere (metacharacters are literal) | The default; known exact wording |
| `exact` | Whole word / whole line, word-boundary anchored | `"Total"` must not match `"Subtotal"` |
| `regex` | Python `re`, one search per page | Case numbers, dates, dollar amounts |
| `fuzzy` | Best sliding-window similarity (`difflib`) over words | **OCR'd text**, where `Notice to Owner` arrives as `Notlce to 0wner` |

Scoring:

| Situation | Score | Verdict |
|---|---|---|
| Found (`count >= min_count`) | `10` | PASS |
| `fuzzy` near miss, best ratio ≥ 0.5 | `2`–`6`, scaled between the 0.5 floor and `fuzzy_threshold` | FAIL / MARGINAL |
| Not found (or a fuzzy ratio below 0.5) | `1` | FAIL |
| Document has no text layer at all | `1` | FAIL, with a reason naming the cause (`ocr=never`, or the engine is disabled) |

`confidence` is 100 for native text, the OCR confidence ×100 for recognised
text, and 0 when there is none. The result's `detail` carries the full match
record:

```json
"Notice to Owner": {
  "score": 10, "verdict": "PASS", "confidence": 99, "method": "text",
  "reason": "Found 1× on page(s) [0] via fuzzy match (best ratio 0.86).",
  "detail": {
    "found": true, "count": 1, "best_ratio": 0.8571,
    "mode": "fuzzy", "pattern": "Notice to Owner",
    "pages": [0],
    "snippets": [{"page": 0, "text": "…33601 NOTICE TO OWNER Under Florida law…", "ratio": 0.8571}],
    "searched_chars": 577,
    "case_sensitive": false, "min_count": 1, "fuzzy_threshold": 0.85,
    "text_source": "ocr"
  }
}
```

An unusable pattern (empty, longer than 500 characters, or an invalid regex)
is rejected with **HTTP 400 at submit time**, not as a failed job.

### Criterion scoring rubrics

| `hint` | Score mapping | Verdict thresholds |
|---|---|---|
| `quality` | 1-10 quality level | 1-3 = FAIL, 4-6 = MARGINAL, 7-10 = PASS |
| `presence` | 10=clearly present, 5=uncertain, 1=clearly absent | 7-10 = PASS, 4-6 = MARGINAL, 1-3 = FAIL |
| `auto` | LLM infers from name | Same thresholds |

### Built-in `cv` detector names

| Names | Technique |
|---|---|
| `sharpness`, `is sharp`, `is blurry` | Laplacian variance |
| `exposure`, `proper exposure`, `is exposed` | Mean pixel intensity |
| `has trees`, `has vegetation`, `has greenery`, `has plants` | HSV green masking |
| `has sky` | Upper-region blue/grey analysis |
| `has faces`, `has people`, `has person` | OpenCV Haar cascade |
| `has water`, `has pool`, `has swimming pool` | Blue/teal hue + flat-texture |
| `has text`, `has text regions`, `has writing` | Sobel edge density per block |

Fuzzy name matching is applied, so near-matches also work. See `GET /cv-detectors` for the full live list.

---

## Endpoints

### `GET /health`

Liveness check.

```bash
curl http://localhost:8005/health
# → {"status": "ok"}
```

---

### `GET /hints`

Returns all available hint values and their LLM scoring instructions.

```bash
curl http://localhost:4001/v1/classifier/hints -H "Authorization: Bearer sk-1234"
```

```json
{
  "hints": {
    "quality":  {"heading": "...", "rubric": "...", "extra": ""},
    "presence": {"heading": "...", "rubric": "...", "extra": "...chain-of-thought instruction..."},
    "auto":     {"heading": "...", "rubric": "...", "extra": ""}
  }
}
```

---

### `GET /cv-detectors`

Returns all registered CV detector functions and their name aliases.

```bash
curl http://localhost:4001/v1/classifier/cv-detectors -H "Authorization: Bearer sk-1234"
```

```json
{
  "detectors": [
    {"function": "check_blur",        "names": ["is blurry", "is sharp", "sharpness"]},
    {"function": "detect_vegetation", "names": ["has greenery", "has plants", "has trees", "has vegetation"]}
  ],
  "total_names": 20
}
```

---

### `GET /document-kinds`

Returns what this container can accept and what it can do with it: the four
document kinds with their extensions, magic-byte detection rules, and whether
they carry page images or native text; the `.doc` rejection; the four
`text_match_modes`; the live OCR status; and the page/DPI/token limits in
force. Cheap introspection, like `/hints` and `/cv-detectors` — no job is
submitted.

```bash
curl http://localhost:4001/v1/classifier/document-kinds -H "Authorization: Bearer sk-1234"
```

```json
{
  "kinds": [
    {"kind": "image", "extensions": [".jpg", ".jpeg", ".png"],
     "detection": "magic bytes: FF D8 FF (JPEG) / 89 50 4E 47 0D 0A 1A 0A (PNG)",
     "pages": "1", "has_page_images": true, "native_text": false,
     "notes": "EXIF orientation is applied on load. Text criteria need OCR."},
    {"kind": "pdf", "…": "…"},
    {"kind": "txt", "…": "…"},
    {"kind": "docx", "…": "…"}
  ],
  "unsupported": [
    {"kind": "doc", "reason": "Legacy OLE2 Word files are not readable by python-docx. Convert to .docx and re-upload.",
     "detection": "magic bytes: D0 CF 11 E0 A1 B1 1A E1"}
  ],
  "text_match_modes": {
    "contains": "substring anywhere in the document text (default)",
    "exact": "whole word or whole line (word-boundary anchored)",
    "regex": "Python regular expression, max 500 characters",
    "fuzzy": "best sliding-window similarity — use this on OCR'd text"
  },
  "ocr": {"engine": "rapidocr", "available": true, "min_native_chars": 20,
          "modes": ["auto", "always", "never"], "default": "auto"},
  "limits": {"max_pages": 20, "pdf_render_dpi": 150,
             "llm_text_char_budget": 60000, "images_per_llm_prompt": 1}
}
```

`ocr.available` is the honest answer: it is `false` when
`CLASSIFIER_OCR_ENGINE=none` **and** when the engine failed to load, which is
worth checking before blaming a failing `text` criterion on the document.

---

### `POST /assess`

Submit a single-document assessment job.

**Content-Type:** `multipart/form-data`  
**Returns:** `202 Accepted` — poll `GET /jobs/{job_id}` for the result.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `file` | File | Yes* | — | The document: JPEG, PNG, PDF, `.txt`, or `.docx` |
| `image` | File | Yes* | — | Deprecated alias for `file`, still accepted so existing callers keep working |
| `criteria` | string (JSON array) | No | see defaults | JSON array of `CriterionInput` objects |
| `ocr` | `auto` \| `always` \| `never` | No | `auto` | Text-recognition policy — see § OCR |

\* Send one of `file` / `image`. Sending neither is a **400**; `file` wins if
both are present.

**Example request:**

```bash
curl http://localhost:4001/v1/classifier/assess \
  -H "Authorization: Bearer sk-1234" \
  -F "file=@notice.pdf" \
  -F "ocr=auto" \
  -F 'criteria=[
    {"name":"Notice to Owner",                      "type":"text","match":"fuzzy","weight":4.0},
    {"name":"case number",                          "type":"text","match":"regex","pattern":"CASE-\\d{5}","weight":2.0},
    {"name":"signature is present",                 "type":"llm","hint":"presence","weight":3.0,"depends_on":"Notice to Owner"},
    {"name":"sharpness",                            "type":"cv",                  "weight":1.0}
  ]'
# → {"job_id": "abc123", "phase": "pending"}
```

Errors: **400** for an empty criteria list, an empty file, an unusable text
pattern, an invalid `ocr` value, a content type outside the allowlist, or bytes
that are not a supported document (including legacy `.doc`).

**Polling:**

```bash
curl http://localhost:4001/v1/classifier/jobs/abc123 -H "Authorization: Bearer sk-1234"
```

**Result shape** (inside `job.result`):

```json
{
  "image_info": {"width": 1240, "height": 1755, "format": "application/pdf", "size_bytes": 193864},
  "document_info": {
    "kind": "pdf",
    "filename": "notice.pdf",
    "page_count": 2,
    "truncated_pages": 0,
    "pages": [
      {"index": 0, "width": 1240, "height": 1755, "text_source": "ocr", "ocr_confidence": 0.994, "text_chars": 413},
      {"index": 1, "width": 1240, "height": 1755, "text_source": "ocr", "ocr_confidence": 0.993, "text_chars": 161}
    ],
    "ocr": {"mode": "auto", "engine": "rapidocr", "ran": true, "pages_recognised": 2},
    "text_sent_to_llm": {"chars": 574, "truncated": false, "budget": 60000},
    "llm_image_page": 0
  },
  "assessment": {
    "overall_verdict": "PASS",
    "overall_score": 9,
    "per_criterion_scores": {
      "has electrical meter": {
        "score": 10, "verdict": "PASS", "confidence": 95, "method": "llm",
        "reason": "I observe a clearly visible electrical meter on the wall. Therefore has electrical meter is present."
      },
      "meter value is readable": {
        "score": 8, "verdict": "PASS", "confidence": 80, "method": "llm",
        "reason": "The meter dial is visible and digits are legible."
      },
      "three feet of clearance around meter": {
        "score": 7, "verdict": "PASS", "confidence": 70, "method": "llm",
        "reason": "I observe no obstructions within approximately three feet of the meter."
      },
      "sharpness": {
        "score": 9, "verdict": "PASS", "confidence": 100, "method": "cv",
        "detail": "Laplacian variance: 412.3 (threshold: 100.0)"
      }
    },
    "weighted_score_breakdown": {
      "formula": "sum(score * weight) / total_weight",
      "total_weight": 10.5,
      "weighted_sum": 93.5,
      "unrounded_average": 8.9047,
      "final_score": 9,
      "per_criterion": {
        "has electrical meter":                 {"score": 10, "weight": 4.0, "contribution": 40.0},
        "meter value is readable":              {"score": 8,  "weight": 3.0, "contribution": 24.0},
        "three feet of clearance around meter": {"score": 7,  "weight": 2.5, "contribution": 17.5},
        "sharpness":                            {"score": 9,  "weight": 1.0, "contribution": 9.0}
      }
    }
  },
  "verdict": "PASS"
}
```

#### `document_info`

| Field | Meaning |
|---|---|
| `kind` | `image` \| `pdf` \| `txt` \| `docx`, decided from the bytes |
| `filename` | What the caller sent; informational only |
| `page_count` | Pages actually loaded |
| `truncated_pages` | Pages dropped at the `CLASSIFIER_DOC_MAX_PAGES` cap |
| `pages[]` | Per page: `index`, `width`/`height` (0 when there is no image), `text_source` (`native` \| `ocr` \| `none`), `ocr_confidence` (0–1 or null), `text_chars` |
| `ocr` | `mode` (the request's value), `engine`, `ran`, `pages_recognised`. `ran: false` with `mode: auto` means no page needed it |
| `text_sent_to_llm` | `chars`, `truncated`, and the `budget` in force. All zero/false when no LLM criterion ran |
| `llm_image_page` | Which page's image went into the prompt, or `null` for a text-only prompt |

`image_info` is retained unchanged for backwards compatibility: page 0's image
dimensions (0 × 0 for `.txt`/`.docx`), the document's real content type, and
the upload size. Stored `pre_generated_analysis` blobs therefore keep working.

**Skipped criterion example** (when `has electrical meter` fails):

```json
"meter value is readable": {
  "verdict": "SKIPPED",
  "score": null,
  "confidence": null,
  "method": "skipped",
  "reason": "Skipped - dependency 'has electrical meter' did not pass (verdict: FAIL)."
}
```

**Not-applicable criterion example** (a `cv` criterion on a `.txt` / `.docx`,
which has no page images). Same shape as a dependency skip, so consumers need
no new branch, and it is likewise excluded from the weighted score:

```json
"sharpness": {
  "verdict": "SKIPPED",
  "score": null,
  "confidence": null,
  "method": "skipped",
  "reason": "Skipped - document has no page images (docx documents are text-only, so OpenCV criteria cannot be evaluated)."
}
```

---

### `POST /assess/compare`

Submit a comparison job — assess an input document against one or more reference examples.

**Content-Type:** `application/json`  
**Returns:** `202 Accepted` — poll `GET /jobs/{job_id}` for the result.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `image` | `DocumentInput` | Yes | — | The subject document (field name kept as `image` for compatibility) |
| `criteria` | `CriterionInput[]` | No | defaults | List of criterion objects |
| `aggregation` | `mean` \| `min` \| `max` | No | `mean` | How to collapse per-example scores into one aggregate |
| `ocr` | `auto` \| `always` \| `never` | No | `auto` | Text-recognition policy, applied to the subject **and** every live example |
| `examples` | `ExampleInput[]` | Yes (min 1) | — | Reference documents to compare against |

#### `DocumentInput` (was `ImageInput`)

| Field | Type | Description |
|---|---|---|
| `data` | string | Base64-encoded document bytes or a URL (SSRF-checked before fetching). Any supported kind — the bytes decide, not the URL's extension |
| `type` | `"base64"` \| `"url"` | How to obtain `data` |

`ImageInput` remains as an alias of `DocumentInput` in `models.py`, so the
request shape is unchanged.

#### `ExampleInput`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `data` | string | Yes | — | Base64-encoded document bytes or URL |
| `type` | `"base64"` \| `"url"` | Yes | — | How to interpret `data` |
| `weight` | float (0.0–1.0) | No | `0.5` | How much similarity to this example influences its combined score. `0.0` = absolute quality only; `1.0` = similarity only. |
| `pre_generated_analysis` | object | No | `null` | A prior result from `/assess` or `example_results[n].example_analysis`. Skips re-analysis of this example. **Must be from the same classifier version** — see caching note below. |

#### Aggregation options

| Value | Behaviour | Use when |
|---|---|---|
| `mean` | Average of all combined scores | All examples equally important |
| `min` | Lowest combined score wins | Input must be close to every example |
| `max` | Highest combined score wins | Input needs to match any one example |

#### Combined score formula

```
combined = (1 - weight) × input_overall_score + weight × similarity_score
```

**Example request:**

```json
{
  "image": {"data": "<base64>", "type": "base64"},
  "criteria": [
    {"name": "has electrical meter", "type": "llm", "hint": "presence", "weight": 4.0},
    {"name": "meter value is readable", "type": "llm", "hint": "quality", "weight": 3.0, "depends_on": "has electrical meter"},
    {"name": "Notice to Owner", "type": "text", "match": "fuzzy", "weight": 2.0}
  ],
  "aggregation": "mean",
  "ocr": "auto",
  "examples": [
    {"data": "<base64>", "type": "base64", "weight": 0.5},
    {"data": "<base64>", "type": "base64", "weight": 0.5, "pre_generated_analysis": {"image_info": {}, "assessment": {}, "verdict": "PASS"}}
  ]
}
```

**Result shape** (inside `job.result`):

```json
{
  "status": "ok",
  "criteria": [...],
  "aggregation": "mean",
  "ocr": "auto",
  "input_analysis": {
    "image_info": {"width": 480, "height": 640, "format": "image/jpeg", "size_bytes": 112400},
    "assessment": {"overall_verdict": "PASS", "overall_score": 9, "per_criterion_scores": {...}, "weighted_score_breakdown": {...}},
    "verdict": "PASS"
  },
  "example_results": [
    {
      "index": 0,
      "weight": 0.5,
      "pre_generated": false,
      "example_analysis": {"image_info": {...}, "assessment": {...}, "verdict": "PASS"},
      "similarity": {
        "overall_similarity": 0.92,
        "similarity_score": 9.2,
        "per_criterion": {
          "has electrical meter":    {"example_score": 10, "input_score": 10, "similarity": 1.0},
          "meter value is readable": {"example_score": 8,  "input_score": 8,  "similarity": 1.0}
        }
      },
      "combined_score": 9.1,
      "combined_verdict": "PASS"
    }
  ],
  "aggregate": {
    "method": "mean",
    "combined_score": 9.1,
    "combined_verdict": "PASS",
    "per_example_combined_scores": [9.1]
  }
}
```

---

### `GET /jobs/{job_id}`

Poll for the status and result of a submitted job.

```bash
curl http://localhost:4001/v1/classifier/jobs/abc123 -H "Authorization: Bearer sk-1234"
```

```json
{
  "job_id": "abc123",
  "phase": "completed",
  "created_at": "2026-07-30T15:00:00+00:00",
  "updated_at": "2026-07-30T15:00:07+00:00",
  "elapsed_seconds": 7.0,
  "metadata": {"type": "assess", "request_id": "req-xyz"},
  "result": {...},
  "error": null
}
```

`phase` values: `staging` → `pending` → `processing` → `completed` | `failed` (see § Async job pattern).

**Response shape migration note.** As of the `common.jobs` extraction, the
top-level `id` is now `job_id`, `status` is now `phase`, and the previously
separate `type` + `request_id` columns are nested under `metadata`. Callers
that parse the response by field name need to update accordingly.

---

### `GET /jobs`

List recent jobs (newest first, result blobs excluded).

```bash
curl "http://localhost:4001/v1/classifier/jobs?limit=10" -H "Authorization: Bearer sk-1234"
```

---

### `DELETE /jobs/{job_id}`

Delete a job record. Returns `204 No Content`.

---

## Caching example analyses

Re-analysing the same reference image on every request wastes LLM tokens. The recommended pattern is:

1. Call `/assess` once on each reference image and retrieve the result via `GET /jobs/{job_id}`.
2. Save the `result` object from the job.
3. Pass the saved result as `pre_generated_analysis` in subsequent `/assess/compare` calls.

> **Compatibility warning:** `pre_generated_analysis` values must come from the same version of the classifier that is currently running. The internal response structure can change between releases. A stored analysis from an older version will cause a runtime error. Always re-generate stored analyses after upgrading the classifier.

---

## Verdict thresholds

| Score | Verdict |
|---|---|
| 7–10 | PASS |
| 4–6 | MARGINAL |
| 1–3 | FAIL |
| — | SKIPPED (dependency did not pass, or the criterion is not applicable to this document kind) |

SKIPPED criteria are excluded from the weighted score calculation entirely.

---

## Hand-testing fixtures

[`unit-tests/classifier/documents/`](../../unit-tests/classifier/documents/)
holds one generated fixture per document kind and failure mode — a native PDF,
a scanned PDF, a `.txt`, a `.docx`, a well and a badly photographed letter, and
a legacy `.doc` — each with the criteria to send and the result to expect, plus
curl examples. The Postman collection's **Documents** folder mirrors it with
ready-to-run requests.
