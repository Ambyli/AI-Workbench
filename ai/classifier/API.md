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

## Module layout

One level of subpackages, imported by bare name — the container sets
`PYTHONPATH=/app/ai/classifier` and runs `uvicorn main:app`, so a module reads
`from analysis.pipeline import analyze_document`, never `from classifier.…`.

```
ai/classifier/
  main.py            FastAPI app, lifespan, router registration — nothing else
  config.py          every constant and env knob in the service, one file
  logger.py          the one "classifier" logger every module imports
  middleware.py      correlation IDs: the ContextVar, the filter, the middleware
  metrics.py         the Prometheus objects produced in one module, read in none
  utils.py           verdict_from_score — the PASS/MARGINAL/FAIL line
  api/               the HTTP layer
    schemas.py       CriterionInput, RegionsOptions, CompareRequest, …
    assess.py        POST /assess, POST /assess/compare
    locate.py        POST /locate, and the `features` field it parses
    introspection.py GET /hints, /cv-detectors, /document-kinds, /health
    artifacts.py     the four /jobs/{id}/artifacts routes
  analysis/          the pipeline, split by concern
    criteria.py      parse the criteria JSON, reject a bad pattern at submit
    loading.py       bytes → Document: content type, EXIF, kind, URL + SSRF
    ocr.py           the OCR engine singleton, and whether this job needs it
    geometry.py      the ≤1000-px working frame and the map back to originals
    cv_eval.py       the `cv` path: one OpenCV detector across every page
    detector_eval.py the `detector` path: the open-vocabulary service
    text_eval.py     the `text` path: deterministic search of the text layer
    llm_eval.py      what the one LLM call gets to see
    weighting.py     depends_on resolution and the weighted overall score
    pipeline.py      analyze_document + analyze_upload / _input / resolve_example
  regions/           "where did you find it" → files and result fields
    store.py         the one ArtifactStore instance for the process
    collect.py       visible_regions, attach_regions, diff_criterion_name
    artifacts.py     regions.json, the rendered layers, the manifest
    sweeper.py       the TTL task, the DELETE hook, the disk gauges
  llm/
    prompts.py       the scoring prompt, and the loop's two small ones
    client.py        call_vllm, call_vllm_json, the image encoder, the counters
    validate.py      key normalisation, score/confidence clamping
    boxes.py         the bounding-box enforcement loop
  cv/                REGISTRY + get_detector in __init__.py
    quality.py       check_blur, check_exposure — whole-page, no regions
    features.py      vegetation, sky, faces, water, text — each with regions
    regions.py       the two region shapes the detectors build
  detector/
    client.py        the ai/detector HTTP client (transport only)
  compare/
    scoring.py       similarity, the quality/similarity blend, the aggregate
    diff.py          change detection: ORB + RANSAC, then a pixel difference
  jobs/
    payloads.py      the JSON-safe dict each endpoint stores at enqueue time
    runners.py       run_assess, run_compare, run_locate
    queue.py         ClassifierQueue + the registry / queue / sweeper singletons
  bin/
    grounding_experiment.py   operator tool — runs where the model is
```

Import direction is strictly one way:

```
config → logger / metrics / utils → api.schemas
       → cv, llm, detector, compare, regions → analysis → jobs → api → main
```

`analysis` may import `regions`, `llm`, `cv` and `detector`; `regions` must
never import `analysis` — that is what keeps region collection additive, so
asking *where* can never move a score. `api/__init__.py` deliberately imports
nothing: `regions`, `analysis` and `llm` all import `api.schemas`, and a router
import there would close the cycle back through `jobs`.

---

## Async job pattern

`POST /assess`, `POST /assess/compare`, and `POST /locate` all return
**202 Accepted** immediately with a job ID. Poll `GET /jobs/{job_id}` until
`phase` is `"completed"` or `"failed"`, then read the `result` field.

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

**Retention.** `JOB_TTL_HOURS` (24) is now enforced, not informational: the
artifact sweeper deletes a terminal job's artifact directory **and its row**
past the TTL, on startup and every `CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S`. A
job still `pending` or `processing` is never swept, however old — a queue that
backed up for a day should drain, not evaporate.

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
| `type` | `"cv"` \| `"llm"` \| `"text"` \| `"detector"` | `"llm"` | `"llm"`: scored by the vision LLM (one page image + the extracted text). `"cv"`: run through a registered OpenCV detector by name; `SKIPPED` when the document has no page images; when no detector matches it goes to the [open-vocabulary detector](#the-detector) if `regions.detector` is set and `DETECTOR_URL` is configured, and to the LLM otherwise. `"detector"`: always the open-vocabulary detector, using the criterion name as the text prompt — boxes and a score, no tokens, whether or not layers were requested; falls back to the LLM when the service is unconfigured or unreachable. `"text"`: deterministic search of the document's text layer — no tokens. |
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

## Regions and layers

A score says *whether*. A **region** says *where*: a box or polygon on a
specific page, in that page's original pixels, attached to the criterion that
found it. Ask for them with the `regions` option on `/assess` and
`/assess/compare`, or use [`/locate`](#post-locate), where they are the whole
point.

**Off by default.** Regions cost extra detector work and the rendered layers
cost disk, so a request that does not mention them behaves exactly as before —
same keys, same scores, same verdicts. Turning them on is strictly additive:
the geometry is read off the results the detectors and text matchers already
produced, so a score can never move because you asked where.

### What can localise, and what cannot

| Criterion path | Source | What you get |
|---|---|---|
| `cv` — `has faces` | `cv` | One box per Haar detection |
| `cv` — `has vegetation` / `has sky` / `has water` | `cv` | Simplified contour polygons above a minimum area |
| `cv` — `has text` | `cv` | Adjacent high-density 32×32 blocks merged into boxes — roughly one per paragraph or column |
| `cv` — `sharpness` / `exposure` | — | **Nothing.** A whole-page measurement does not fabricate a full-page box |
| `cv` — a name no OpenCV detector matches, with `regions.detector` on | `detector` | One box per detection from the [open-vocabulary detector](../detector/DETECTOR.md), and the criterion is **scored from them** — no LLM call. See [The detector](#the-detector) |
| `detector` — any name | `detector` | Same, whether or not layers were requested — the criterion named the path |
| `text` on an OCR'd page | `ocr` | The OCR line polygon(s) the match landed on, with the recogniser's confidence as `score`. A hit spanning a line break yields one region per line |
| `text` on a native PDF page | `pdf-text` | PyMuPDF rectangles — `search_for` for `contains`/`exact`, word-span reconstruction for `regex`/`fuzzy`. Carries `attrs.pdf_rect` in **points** as well as page pixels |
| `text` on `.txt` / `.docx` | — | Nothing — no geometry exists. The hits are still in `detail.snippets` |
| `llm` with `hint: presence`/`auto`, `regions.detector` on | `detector` | Boxes from the detector. The **model still scores** the criterion; the detector only says where |
| `llm` with `hint: presence`/`auto` scored ≥ 7, `regions.llm_boxes` on | `llm` | A box the model drew and then confirmed by looking at a crop of it, plus **every rejected attempt**. See [The LLM enforcement loop](#the-llm-enforcement-loop) |
| `llm` — anything else | — | Nothing. `localization` comes back `{"attempts": [], "accepted_attempt": null, "calls": 0}`, which is the honest answer to "where" for a criterion nobody asked to locate |
| `/assess/compare` with `regions.diff` | `diff` | Polygons of what appeared, disappeared or changed between the subject and each live example. See [Change detection](#change-detection) |

> **Approximation warning.** PDF `regex`/`fuzzy` rectangles are reconstructed
> from the word boxes covering the match. A phrase PyMuPDF splits differently
> from the text layer (hyphenation, a ligature, a column break mid-phrase) does
> not line up, and the region is omitted rather than guessed at.

### Coordinates

Every region is in **original page pixels** — after EXIF transpose for a photo,
after the raster render for a PDF page. Detectors work on a ≤1000-px working
image and their output is divided by that page's `working_scale` before it is
stored, so a consumer never has to know the working size existed. The frame for
each page comes back in `page_geometry`:

```json
"page_geometry": [
  {"page": 0, "width": 1240, "height": 1755, "working_scale": 0.5694,
   "pdf_points": [595.2, 842.4]}
]
```

`pdf_points` is the page's size in points for a PDF and `null` otherwise.

### The `regions` option

Form field on `/assess` and `/locate`, body field on `/assess/compare`:

```json
"regions": {
  "enabled": true,
  "layers": ["svg", "png", "preview"],
  "layers_per_criterion": false,
  "llm_boxes": false,
  "detector": false,
  "diff": false,
  "examples": false
}
```

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `true` (in the object) | Collect regions at all. Implied on `/locate` |
| `layers` | `["svg"]` | Any subset of `svg` / `png` / `preview`. `[]` writes `regions.json` only |
| `layers_per_criterion` | `false` | Pre-render one file per criterion per page at job time instead of on first fetch. For a pipeline that will pull every one anyway — 8 criteria × 20 pages × 3 formats is a lot of files nobody may open |
| `llm_boxes` | `false` | Run the [enforcement loop](#the-llm-enforcement-loop) for every `llm` presence criterion the model scored ≥ 7. Costs up to two small LLM calls per attempt per criterion. Never changes a score |
| `detector` | `false` | Hand the [open-vocabulary detector](#the-detector) the implicit work — see below. Needs `DETECTOR_URL`; without it the request still succeeds and `artifacts.notes` says the service is not configured |
| `diff` | `false` | **Compare only.** [Change detection](#change-detection) against each live single-image example. On `/assess` or `/locate` it is recorded and `artifacts.notes` says it was ignored |
| `examples` | `false` | **Compare only.** Also collect regions and render layers for the live examples, as `e{i}.p{n}.<fmt>` in the same job directory. Off by default because it doubles the artifact volume |

`regions.json` and `manifest.json` are always written when `enabled` is true,
whatever `layers` says.

**Shorthand on the multipart endpoints.** Multipart forms carry strings, so
`regions` accepts four spellings:

| Value | Means |
|---|---|
| absent, `""`, `false`, `0`, `no`, `off` | Off (the default) |
| `true`, `1`, `yes`, `on` | Regions + the SVG layer |
| `svg`, `svg,png`, `svg,png,preview` | Regions + those layers |
| `none` | `regions.json` only, no pictures |
| `{"enabled": true, "layers": []}` | The full object, as a JSON string |

An unknown layer name is a **400** at submit time, not a failed job.

### The detector

`ai/detector` is a text-prompted object detector: an image plus free-text
labels in, boxes out ([DETECTOR.md](../detector/DETECTOR.md)). It is what lets
an arbitrary `has X` feature localise — and be *scored* — without a vision-LLM
call. The classifier reaches it at `DETECTOR_URL`; **empty means off**, and a
request that asks for it anyway still succeeds with a note.

Two ways in:

| Spelling | What happens | Needs `regions.detector`? |
|---|---|---|
| `{"name": "has roof vent", "type": "detector"}` | Always answered by the detector: boxes **and** a score | no — the criterion named the path |
| `{"name": "has roof vent", "type": "cv"}` with `regions.detector: true` | Same, when no OpenCV detector matches the name | yes |
| `{"name": "has solar panels", "type": "llm", "hint": "presence"}` with `regions.detector: true` | Localised only — the **model still scores it** | yes |

> **How a `cv` name is resolved.** Criterion names are matched against the
> OpenCV registry exactly (case-insensitive), then by `difflib` at cutoff
> `CV_NAME_FUZZY_CUTOFF` (0.8) so typos still land — `has textt` → `has text`,
> `has a pool` → `has pool`. Anything less similar falls through to the
> detector service (with `regions.detector`) or the LLM. The cutoff used to be
> 0.6, which sent `has solar panels` to the vegetation detector and
> `has bicycle` to the Haar face cascade; that is fixed. Spell
> `"type": "detector"` when you want the detector regardless of the registry;
> `GET /cv-detectors` lists the aliases the registry will grab.

**Scoring from boxes.** A detector-scored criterion comes back with
`method: "detector"`:

| Best box score | Score | Verdict |
|---|---|---|
| ≥ `0.5` | 10 | PASS |
| ≥ `DETECTOR_MIN_SCORE` (0.25) | 7 | PASS — a real finding, but not one to build on |
| nothing above the floor | 1 | FAIL |

`confidence` is the best box score as a percentage, and `detail` carries
`detector_matches`, `best_score`, `min_score` and `pages`. Each region carries
`attrs.detector_score`.

**A negative gets no LLM second opinion.** A criterion the detector looked for
and did not find is FAILed there and is **not** then passed to the model.
Asking both would charge a token cost for every absent feature — which is most
of them on most documents — and would make the answer depend on which of two
disagreeing sources is consulted last. If you want the model's opinion on a
feature, ask for it: give the criterion `"type": "llm"`.

**Failure is never fatal.** A connection refused, a timeout, a 503 while the
model loads — all of them leave the job intact: the note lands in
`artifacts.notes`, and the criteria that were waiting on the detector fall
back to the vision LLM exactly as they did before this service existed.

**Cost.** One HTTP call per page image, carrying every label wanted for that
page (split at `DETECTOR_MAX_LABELS_PER_CALL`). The image sent is the ≤1000-px
working page — the same pixels the OpenCV detectors and the prompt see — and
the boxes are rescaled into original page pixels on arrival.

**What ran** is reported in `document_info.detector` and, for a job with
artifacts, at the top of `regions.json`:

```json
"detector": {
  "configured": true, "url_host": "detector:8000", "min_score": 0.25,
  "timeout_seconds": 30.0, "max_labels_per_call": 16,
  "model": "google/owlv2-base-patch16-ensemble", "device": "cuda",
  "calls": 1, "labels": 3, "detections": 4, "elapsed_ms": 184.2,
  "errors": [], "requested": true, "notes": []
}
```

`GET /document-kinds` carries the `configured` / `url_host` half of the same
block, so a caller can check before submitting a job that asks for boxes this
container cannot produce.

### The LLM enforcement loop

A box from a vision model is a **claim**, not a measurement. Asked "where are
the solar panels?", a model will answer when there are none, when it cannot
tell, and when the honest answer is "somewhere in the upper half" — and every
one of those is four numbers that look exactly like a correct one. So nothing
here trusts the first answer:

```
attempt 1 ── ask ────── one criterion, the ONE page image the scoring call
   │                    used — with a labelled 0–1000 coordinate grid drawn
   │                    on it (CLASSIFIER_LLM_BBOX_GRIDLINES, lines every
   │                    CLASSIFIER_LLM_BBOX_GRID_STEP units, numbered along
   │                    every edge) — a box as [x1,y1,x2,y2] on that grid
   ├── validate ─────── four finite numbers, x1<x2, y1<y2, inside the grid,
   │                    area between CLASSIFIER_LLM_BBOX_MIN_AREA (0.2%) and
   │                    _MAX_AREA (95%)
   ├── refine ───────── (CLASSIFIER_LLM_BBOX_REFINE) crop the ORIGINAL page
   │                    around the coarse box — _REFINE_ZOOM (2.5) × the box,
   │                    at least _REFINE_MIN_SPAN (20%) of the page — draw
   │                    the grid on the crop, ask again, map the answer back
   │                    into the page frame. An unusable second answer keeps
   │                    the coarse box; both are recorded
   ├── verify ───────── crop that box out of the ORIGINAL page (+25% padding,
   │                    CLASSIFIER_LLM_BBOX_CROP_PAD), send the CROP ALONE:
   │                    "is <criterion> visible in this? 1–10". Accept at
   │                    CLASSIFIER_LLM_BBOX_VERIFY_PASS (7)
   ├── cross-check ──── IoU against the detector's best box for the same
   │                    criterion, when `regions.detector` produced one →
   │                    attrs.detector_iou. Informational: it never accepts
   │                    or rejects
   └── retry ────────── re-ask with the rejection as feedback, up to
                        CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS (3)
```

The verify call is what makes a box mean something: the page is deliberately
*not* shown alongside the crop, because a model that names a plausible region
of a photo it was already told contains the feature is not evidence, while one
that recognises the feature in 300×300 isolated pixels is. (It is also the
one-image-per-prompt rule — vLLM accepts one image per request either way.)

**Why the grid and the second pass.** Measured on the two photographed
utility bills in `unit-tests/classifier/documents/` (80 asks, 8 features, 5
ways of asking), the coordinate contract itself is sound — synthetic markers
come back with slope 0.96–1.03 on both axes at every aspect ratio — but on a
dense real document the model places a text-sized feature with the right x
and a y that is 25–100 grid units off, on a line 30 units tall, so the
verify crop lands on the wood grain above the header. Drawing a labelled
grid on the ask image halved that error; asking a second time on a zoomed
crop of the original with its own grid brought hits on the four "amount due"
lines from 0/8 to 7/8 and the mean y error from 48 units to 2.4. Both are on
by default and both are recorded on the attempt, so a box can always be
traced back to what the model first said. What the second pass cannot fix is
*identification*: asked for a logo, a model that boxes the heading naming the
same company 90 units lower is precisely placed on the wrong thing.

**When it runs.** All four have to hold, or the loop costs nothing:

| Condition | Why |
|---|---|
| `regions.llm_boxes: true` | Up to 2 × 3 extra calls per criterion |
| the criterion is `type: "llm"` with `hint` `presence` or `auto` | "image sharpness" is a property of the whole page; boxing it would be a fabrication |
| the scoring call gave it **≥ 7** | Below that the model has just said the feature is absent. Asking the same model where the thing it cannot see is produces a guess |
| the document has a page image | `.txt` / `.docx` have no pixel space for a box |

**It never changes a score or a verdict.** The loop runs *after* the scoring
call, reads that call's score to decide whether to run at all, and only adds
keys. A criterion whose every attempt is rejected keeps the score it had.

**Every attempt comes back**, accepted or not — a rejected box is evidence
about the model, and it is renderable on its own:

```json
"has solar panels": {
  "score": 9, "verdict": "PASS", "confidence": 85, "method": "llm",
  "regions": [
    {"kind": "box", "points": [[216, 448], [378, 602]], "source": "llm", "score": 9,
     "attrs": {"attempt": 2, "accepted": true, "verify_score": 9, "detector_iou": 0.71}},
    {"kind": "box", "points": [[0, 0], [900, 700]], "source": "llm", "score": null,
     "attrs": {"attempt": 1, "accepted": false,
               "reject": "your previous box [0, 0, 1000, 1000] covered 100% of the image — a full-frame box is not a location; try again, smaller"}}
  ],
  "localization": {
    "attempts": [
      {"attempt": 1, "bbox_grid": [0, 0, 1000, 1000], "bbox_px": [0, 0, 900, 700],
       "valid": false, "accepted": false,
       "reject": "your previous box [0, 0, 1000, 1000] covered 100% of the image — a full-frame box is not a location; try again, smaller"},
      {"attempt": 2, "bbox_grid": [240, 640, 420, 860], "bbox_px": [216, 448, 378, 602],
       "valid": true, "accepted": true, "verify_score": 9,
       "verify_reason": "a roof covered in panels", "detector_iou": 0.71,
       "coarse_bbox_grid": [230, 600, 430, 880], "refine_window_grid": [80, 400, 580, 1000],
       "refined": true}
    ],
    "accepted_attempt": 2,
    "calls": 4
  },
  "artifacts": {
    "slug": "has-solar-panels-9c1e",
    "pages": [{"page": 0, "svg": "/jobs/abc123/artifacts/p0.svg?criterion=has-solar-panels-9c1e"}],
    "attempts": [
      {"attempt": 1, "page": 0, "accepted": false,
       "svg": "/jobs/abc123/artifacts/p0.svg?criterion=has-solar-panels-9c1e&attempt=1"},
      {"attempt": 2, "page": 0, "accepted": true,
       "svg": "/jobs/abc123/artifacts/p0.svg?criterion=has-solar-panels-9c1e&attempt=2"}
    ]
  }
}
```

`accepted_attempt` is `null` when nothing was accepted; `regions` then holds
only rejected boxes and the criterion's score is untouched.

`coarse_bbox_grid`, `refine_window_grid` and `refined` appear on an attempt
whose coarse box validated and so went through the refine pass: `bbox_grid`
is then the refined box when `refined` is `true`, and the coarse box kept
(with a `refine_reject` sentence) when it is `false`. An attempt the
validator rejected never reaches the pass and carries none of the three.
Each stored region from the loop carries `attrs.refined` the same way.

**The combined layer shows the accepted box only.** Three overlapping boxes
for one criterion, two of which the loop itself threw away, is not a picture of
anything — so the stored `p{n}.svg` and any `?criterion=` render with no
explicit `attempt` show the accepted attempt. `?attempt=n` renders attempt *n*
(accepted or not), and `?accepted=false` renders the rejected ones. `bbox_grid`
is what the model literally said; `bbox_px` is the same box in original page
pixels, clamped to the page.

**Rejection wording is the retry's prompt.** Each `reject` is written to be
read by the model on the next attempt — "a full-frame box is not a location;
try again, smaller", "the crop of your previous box … did not show <criterion>
(verify score 3); look elsewhere". Changing them changes the retry behaviour,
not just the log.

**Cost.** One ask per attempt, plus one refine and one verify per attempt
whose coarse box validated — at most nine small calls per located criterion
(six with `CLASSIFIER_LLM_BBOX_REFINE=false`), each capped at
`CLASSIFIER_LLM_BBOX_MAX_TOKENS` (1024, most of which is reasoning). The job's
single scoring call is unchanged. Metric:
`classifier_llm_bbox_attempts_total{outcome=accepted|rejected_invalid|rejected_verify|exhausted}`.

**Before trusting it on a new model**, run the grounding experiment — it
measures, on your images and your criteria, how often attempt 1 validates, how
often the crop confirms it, and how far the accepted boxes are from the
detector's:

```bash
# On the box (it talks to VISION_LLM_API directly)
docker compose exec classifier python bin/grounding_experiment.py
docker compose exec classifier python bin/grounding_experiment.py \
  --images /data/roof-photos --criteria /data/criteria.json --out /data/grounding.json
```

If attempt-1 validity is under ~50%, make the detector the primary source
(`regions.detector`) and keep `llm_boxes` as the fallback for labels it cannot
name.

### Change detection

`/assess/compare` with `regions.diff: true` answers "what is different in the
subject compared to this reference?" — as regions, not a number. Classical CV,
no model:

1. **Align.** ORB keypoints, a ratio-filtered brute-force match, and a RANSAC
   homography that puts the example onto the subject.
2. **Difference.** Warp, blur both to grayscale (`CLASSIFIER_DIFF_BLUR`),
   absolute-difference, Otsu threshold, morphological close, contours over
   `CLASSIFIER_DIFF_MIN_AREA`. A margin around the frame is excluded: two
   photos from slightly different places do not overlap there.
3. **Classify.** Local edge density inside each blob in each image — structure
   in the subject only is `added`, in the reference only is `removed`, in both
   is `changed`.

**Different framing is reported, not guessed at.** Under
`CLASSIFIER_DIFF_MIN_INLIERS` (30) the answer is `aligned: false` with a note
and **no regions at all**. A diff that skips alignment lights up every edge in
the scene, and boxing that would be a lie with a rectangle around it.

```json
"example_results": [
  {
    "index": 0, "weight": 0.5, "pre_generated": false,
    "similarity": { "…": "…" }, "combined_score": 8, "combined_verdict": "PASS",
    "diff": {
      "aligned": true, "inliers": 147,
      "homography": [[1.0, 0.0, 6.1], [0.0, 1.0, 5.9], [0.0, 0.0, 1.0]],
      "changes": {"added": 1, "removed": 2, "changed": 0},
      "regions": [
        {"kind": "polygon", "points": [[130, 509], ["…", "…"]], "source": "diff",
         "score": 0.2078, "label": "_diff:e0",
         "attrs": {"change": "added", "area_px": 17564, "bbox": [130, 509, 340, 623]}}
      ],
      "regions_truncated": false, "note": null,
      "artifacts": {"slug": "diff-e0-…",
                    "pages": [{"page": 0, "svg": "/jobs/abc123/artifacts/diff-e0-p0.svg?criterion=diff-e0-…"}]}
    }
  }
]
```

**Diff regions are not criteria.** They are filed under a synthetic
`_diff:e{i}` key so they have somewhere to live in `regions.json` and a slug
for their layer file, and they never reach the weighted score or the
similarity comparison. `aggregate` is byte-identical with `diff` on and off.

**Skipped, with a reason, when:** the example was supplied as
`pre_generated_analysis` (a stored analysis is JSON, not pixels), or either
side is not a single-image document (a multi-page PDF has no single "the
image"). Each case sets `diff.note` and adds a line to `artifacts.notes`.

Layers land as `diff-e{i}-p{n}.<fmt>` in the subject's artifact directory and
are filterable like any other layer. Metric:
`classifier_diff_jobs_total{aligned=true|false}`.

### Layers for the examples

`regions.examples: true` on `/assess/compare` also collects regions and renders
layers for each **live** example, into the subject's job directory under an
`e{i}.` prefix — an example has no job of its own:

```
/data/artifacts/<job_id>/
├── p0.svg                 the subject
├── diff-e0-p0.svg         change detection against example 0
├── e0.p0.svg              example 0's own regions
└── e0.regions.json        example 0's geometry, keyed by criterion
```

`example_results[i].example_analysis.artifacts` lists exactly those files, and
each of that example's per-criterion `artifacts.regions_url` points at
`e{i}.regions.json`. An example's pages are their own coordinate frame, which
is why it gets its own `regions.json` rather than being mixed into the
subject's `pages` list. Off by default: it roughly doubles the artifact volume.

### Behaviour changes in this build

Two things changed for requests that were already working:

1. **`cv` criteria with no OpenCV detector no longer go straight to the LLM**
   — but only when `regions.detector` is set AND `DETECTOR_URL` is configured.
   Such a criterion is then scored by the detector with `method: "detector"`,
   which means a different score, a different `detail` shape, and **no LLM
   call for it**. A request that does not set `regions.detector`, or a
   deployment with no `DETECTOR_URL`, behaves exactly as before.
2. **`/locate` bare-string features resolve to `detector`** when
   `DETECTOR_URL` is set and no OpenCV detector matches the name — previously
   they resolved to `llm` and came back with no regions at all. `["bicycle"]`
   now returns boxes with no LLM call. A full criterion object is still taken
   at its word.

`CriterionInput.type` also gained the value `"detector"`. This is additive;
nothing that validated before stops validating.

Phases 3 and 4 added two more, both opt-in:

3. **`/locate` bare-string features that resolve to `llm` now cost an LLM
   call** — but only with `regions.llm_boxes` set. The endpoint then makes a
   presence-only scoring call so the enforcement loop has a score to gate on,
   and discards its judgement: `features[name]` still carries no `score`,
   `verdict` or `confidence`, and now carries `localization` and real regions.
   Without `llm_boxes` no call is made, exactly as before.
4. **`per_criterion_scores[name].localization` is present for every `llm`
   criterion** whenever regions are on, as `{"attempts": [],
   "accepted_attempt": null, "calls": 0}` when the loop did not run — so a
   consumer can read `accepted_attempt` without first checking whether the key
   exists. It was the same shape before, minus `calls`.

### Layer formats

| Format | File | Notes |
|---|---|---|
| SVG overlay | `p{n}.svg` | **The default.** `viewBox` is the original page, so it composites over the page image at any size with no transform. One `<g id="c-<slug>" data-criterion="…" data-source="…">` per criterion, `<rect>`/`<polygon>` with `data-source`/`data-score` and a `<title>` tooltip — so a client that downloaded the combined file can still toggle criteria in place. Legend embedded |
| Transparent PNG | `p{n}.layer.png` | Exactly the page's pixel size, alpha everywhere except strokes and translucent fills. The literal "Photoshop layer" |
| Composited preview | `p{n}.preview.jpg` | The page with the layer burned in, JPEG q85, with a corner legend. Also writes `p{n}.base.jpg` — a clean copy of the page, because a burned-in preview cannot be un-burned and a *filtered* preview has to be re-composited from something |
| Regions JSON | `regions.json` | Always written. Keyed by criterion (not a flat list), plus `pages` |

Colour is one stable hue per criterion (hashed from the name, so it is the same
on every page and in every format); the source shows in the stroke — solid for
`cv`/`detector`/`pdf-text`, dashed for `ocr`, dotted for `llm`, double for
`diff`.

### Tying a criterion to its artifacts

Every criterion gets a **slug**: its name lowercased with non-alphanumerics
collapsed to `-`, plus four hex digits of a hash of the exact name, so
`has faces` and `Has Faces!` cannot collide. The slug is what appears in file
names, in SVG group ids, and in the `?criterion=` query parameter;
`manifest.json` carries the slug ↔ name map.

`regions.json` is keyed by criterion:

```json
{
  "job_id": "abc123",
  "pages": [{"page": 0, "width": 900, "height": 700, "working_scale": 1.0, "pdf_points": null}],
  "criteria": {
    "has water": {
      "slug": "has-water-4b21", "type": "cv", "source": "cv", "sources": ["cv"],
      "pages": [0], "count": 2,
      "regions": [
        {"page": 0, "kind": "polygon", "points": [[90, 518], [430, 518], [430, 651], [90, 651]],
         "label": "has water", "score": 0.0713, "source": "cv",
         "attrs": {"area_px": 44812, "flat": true}}
      ],
      "localization": null
    }
  }
}
```

and each criterion result carries its own `artifacts` block — `null` when the
criterion found nothing, so an empty object never implies URLs that would 404:

```json
"has water": {
  "score": 10, "verdict": "PASS", "confidence": 85, "method": "cv",
  "detail": "Qualifying water coverage: 41.8% (263,397 px)",
  "regions": [ … ], "regions_truncated": false,
  "artifacts": {
    "slug": "has-water-4b21",
    "regions_url": "/jobs/abc123/artifacts/regions.json?criterion=has-water-4b21",
    "pages": [
      {"page": 0,
       "svg":     "/jobs/abc123/artifacts/p0.svg?criterion=has-water-4b21",
       "png":     "/jobs/abc123/artifacts/p0.layer.png?criterion=has-water-4b21",
       "preview": "/jobs/abc123/artifacts/p0.preview.jpg?criterion=has-water-4b21"}
    ],
    "attempts": []
  }
}
```

Only pages where *this* criterion has regions are listed, and only formats that
were actually written. `attempts` is empty for every path but `llm`; for an
`llm` criterion that ran the [enforcement loop](#the-llm-enforcement-loop) it
carries one entry per attempt, with `?attempt=n` on each URL so a **rejected**
box can be viewed on its own.

### Inline vs the directory

| Data | In the artifact directory | Inline in the job result |
|---|---|---|
| Per-criterion regions | `regions.json`, complete | `per_criterion_scores[name].regions`, capped at `CLASSIFIER_INLINE_REGIONS_MAX` (50) — past the cap the list is cut and `regions_truncated: true` is set |
| Per-criterion links | derived from the manifest | `per_criterion_scores[name].artifacts` |
| LLM localization | `regions.json`, per criterion | `per_criterion_scores[name].localization` — **not capped**: every attempt, always. At most `CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS` small objects |
| Change detection (compare) | `regions.json` under `_diff:e{i}`, plus `diff-e{i}-p{n}.<fmt>` | `example_results[i].diff` — regions capped at `CLASSIFIER_INLINE_REGIONS_MAX` |
| Page geometry | `manifest.json`, `regions.json` | `page_geometry` |
| Rendered layers | yes | **never** — only their `artifacts.files[]` entries (name, bytes, content type, url) |

Base64 layers are not offered at all — one path to maintain.

### Disk and retention

`CLASSIFIER_ARTIFACT_MAX_BYTES` (50 MB) caps each job. Over it, files are
dropped in a fixed order — PNG layers first, then previews and their base
images — and `regions.json`, `manifest.json` and the SVGs are never dropped,
because everything else re-renders from them. The manifest's `dropped` list
and `artifacts.dropped` say what went.

A background sweeper runs at startup and every
`CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S` (600 s). It deletes artifact
directories whose job is gone or expired **and the expired job rows
themselves** — which makes `JOB_TTL_HOURS` (24) a real retention policy for
the first time. Terminal jobs only: a row still `pending` or `processing` is
never swept, however old. Gauges: `classifier_artifact_bytes`,
`classifier_artifact_dirs`.

`DELETE /jobs/{id}` removes the directory with the row. `DELETE
/jobs/{id}/artifacts` frees the disk but keeps the row and its inline regions.

Hand-testing fixtures with expected geometry per file:
[`unit-tests/classifier/regions/`](../../unit-tests/classifier/regions/).

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
             "llm_text_char_budget": 60000, "images_per_llm_prompt": 1},
  "regions": {
    "enabled_by_default": false,
    "layers": ["png", "preview", "svg"], "default_layers": ["svg"],
    "sources": ["cv", "ocr", "pdf-text", "detector", "llm", "diff"],
    "not_implemented": [],
    "compare_only": ["diff", "examples"],
    "llm_boxes": {"max_attempts": 3, "verify_pass": 7, "min_area": 0.002,
                  "max_area": 0.95, "presence_min_score": 7, "grid": 1000.0},
    "diff": {"min_inliers": 30, "min_area": 0.001, "max_regions": 40,
             "changes": ["added", "removed", "changed"]},
    "detector": {"configured": true, "url_host": "detector:8000",
                 "min_score": 0.25, "timeout_seconds": 30.0,
                 "max_labels_per_call": 16},
    "inline_max_per_criterion": 50, "artifact_dir": "/data/artifacts",
    "artifact_max_bytes": 50000000, "artifact_ttl_hours": 24,
    "sweep_interval_seconds": 600.0
  }
}
```

`regions.detector.configured` is the live answer, not the compiled-in one:
`detector` is a region source only while `DETECTOR_URL` points at something.
Check it before submitting a job that asks for boxes this container cannot
produce. `ocr.available` is the honest answer in the same way: it is `false` when
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
| `regions` | string | No | _(off)_ | Where-did-you-find-it options: `true`, a layer list like `svg,png,preview`, `none` for `regions.json` only, or a JSON object — see [§ Regions and layers](#regions-and-layers) |

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
pattern, an invalid `ocr` value, an unknown `regions` layer, a content type
outside the allowlist, or bytes that are not a supported document (including
legacy `.doc`). A page image under `CLASSIFIER_MIN_IMAGE_WIDTH` ×
`CLASSIFIER_MIN_IMAGE_HEIGHT` (32 × 32 px) on either axis fails the job with
"Image too small" — a thumbnail guard, low enough that a crop of a single
text line submitted as its own document passes.

**With regions** — same call plus one field:

```bash
curl http://localhost:4001/v1/classifier/assess \
  -H "Authorization: Bearer sk-1234" \
  -F "file=@yard.jpg" \
  -F "regions=svg,preview" \
  -F 'criteria=[{"name":"has sky","type":"cv"},{"name":"has water","type":"cv"}]'
```

The result then also carries `page_geometry`, a top-level `artifacts` index,
and `regions` / `regions_truncated` / `artifacts` on each criterion — see
[§ Regions and layers](#regions-and-layers).

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
| `regions` | `RegionsOptions` \| null | No | `null` | Where-did-you-find-it options as an object (no string shorthand here — this endpoint takes JSON). Regions and layers are collected for the **subject** unless `examples: true`; `diff: true` adds [change detection](#change-detection) against each live example. See [§ Regions and layers](#regions-and-layers) |

#### `DocumentInput` (was `ImageInput`)

| Field | Type | Description |
|---|---|---|
| `data` | string | Base64-encoded document bytes or a URL (SSRF-checked before fetching). Any supported kind — the bytes decide, not the URL's extension |
| `type` | `"base64"` \| `"url"` | How to obtain `data` |

`ImageInput` remains as an alias of `DocumentInput` in `api/schemas.py`, so the
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
      "combined_verdict": "PASS",
      "diff": {
        "aligned": true, "inliers": 147,
        "homography": [[1.0, 0.0, 6.1], [0.0, 1.0, 5.9], [0.0, 0.0, 1.0]],
        "changes": {"added": 1, "removed": 2, "changed": 0},
        "regions": [{"kind": "polygon", "source": "diff", "label": "_diff:e0",
                     "attrs": {"change": "added", "bbox": [130, 509, 340, 623]}, "…": "…"}],
        "regions_truncated": false, "note": null,
        "artifacts": {"slug": "diff-e0-…", "pages": [{"page": 0, "svg": "…"}], "attempts": []}
      }
    }
  ],
  "aggregate": {
    "method": "mean",
    "combined_score": 9.1,
    "combined_verdict": "PASS",
    "per_example_combined_scores": [9.1]
  },
  "artifacts": { "…": "…" },
  "page_geometry": [ … ]
}
```

`example_results[i].diff` is present only with `regions.diff: true`; see
[Change detection](#change-detection) for what `aligned: false` means and when
an example is skipped. Diff regions never reach `similarity`,
`combined_score` or `aggregate` — those are identical with `diff` on and off.
`artifacts` / `page_geometry` mirror the subject's, so a caller does not have
to dig through `input_analysis` for the layer URLs.

When `regions` is set, the subject's `artifacts` and `page_geometry` are
mirrored at the top level of the compare result as well as living inside
`input_analysis`, so a caller does not have to dig for the layer URLs.

---

### `POST /locate`

Find features, report no judgement. Same pipeline as `/assess` with the
scoring taken out: no weighted score, no verdict, no dependency resolution,
and **no LLM call at all** in this build. For callers who want boxes for a UI
overlay and should not have to submit criteria, read a verdict, and throw it
away.

**Content-Type:** `multipart/form-data`
**Returns:** `202 Accepted` — poll `GET /jobs/{job_id}` for the result.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `file` | File | Yes* | — | The document: JPEG, PNG, PDF, `.txt`, or `.docx` |
| `image` | File | Yes* | — | Deprecated alias for `file`, accepted for symmetry with `/assess` |
| `features` | string (JSON array) | **Yes** | — | What to find. Entries are either bare names or full `CriterionInput` objects — see below |
| `ocr` | `auto` \| `always` \| `never` | No | `auto` | Text-recognition policy |
| `regions` | string | No | `true` | Layer formats. `enabled` is implied — that is the endpoint — so this only chooses formats |

\* Send one of `file` / `image`.

**Feature resolution.** A bare string has no `type`, and defaulting it to
`llm` would send the common case to a vision model that a registered OpenCV
detector answers for free. So a bare string resolves to whichever path can
produce geometry most cheaply:

1. `cv` — a registered OpenCV detector matches the name.
2. `detector` — `DETECTOR_URL` is configured, so the
   [open-vocabulary detector](#the-detector) can find anything nameable. This
   is what makes `["bicycle"]` return boxes with **no LLM call at all**.
3. `llm` — neither, and the name is given `hint: "presence"` ("where is X" is
   a presence question by construction). With
   `regions={"enabled":true,"llm_boxes":true}` the
   [enforcement loop](#the-llm-enforcement-loop) then runs for it: a
   presence-only scoring call so there is a score to gate on, then ask →
   validate → verify-by-crop → retry, and the feature comes back with real
   regions and a `localization` record but still **no score and no verdict**.
   Without `llm_boxes` no call is made at all and the feature comes back with
   no regions and a reason naming the option to set.

The result's `method` field says which path each feature took. An object
spelled out in full is taken at its word — which is how a `text` feature sets
its `match` mode, and how you force `"type": "detector"` for a name the
OpenCV registry would otherwise fuzzy-match (see the warning under
[The detector](#the-detector)).

```bash
curl http://localhost:4001/v1/classifier/locate \
  -H "Authorization: Bearer sk-1234" \
  -F "file=@letter.png" \
  -F "ocr=always" \
  -F "regions=svg,preview" \
  -F 'features=["has text",
                {"name":"Notice to Owner","type":"text","match":"fuzzy","fuzzy_threshold":0.8}]'
# → {"job_id": "abc123", "phase": "pending"}
```

**Result shape** (inside `job.result`) — `features` where an assessment would
have `assessment`, and no `verdict` anywhere:

```json
{
  "status": "ok",
  "image_info": {"width": 1000, "height": 1300, "format": "image/png", "size_bytes": 412004},
  "document_info": { "…": "…" },
  "features": {
    "has text": {
      "method": "cv",
      "detail": "High-density edge blocks: 35/638 (5.5%)",
      "page": 0,
      "regions": [{"page": 0, "kind": "box", "points": [[96, 192], [352, 288]],
                   "label": "has text", "score": 0.0266, "source": "cv",
                   "attrs": {"blocks": 17}}],
      "regions_truncated": false,
      "artifacts": {"slug": "has-text-2c0d", "regions_url": "…", "pages": [ … ], "attempts": []}
    },
    "Notice to Owner": {
      "method": "text",
      "detail": {"found": true, "count": 1, "…": "…"},
      "regions": [{"page": 0, "kind": "polygon", "points": [[67, 211], [359, 197], [360, 230], [68, 244]],
                   "label": "Notice to Owner", "score": 0.9987, "source": "ocr",
                   "attrs": {"text": "NOTICE TO OWNER", "line": 1, "ratio": 0.93}}],
      "regions_truncated": false,
      "artifacts": { "…": "…" }
    }
  },
  "page_geometry": [ … ],
  "artifacts": { "…": "…" }
}
```

`metadata.type` is `"locate"`, and the job uses the same job store, the same
artifact directory layout, and the same artifact endpoints as an assess job.

An `llm` feature comes back with `"regions": []` and a `reason` naming the
option to set, unless the request carries
`regions={"enabled":true,"llm_boxes":true}` — then the
[enforcement loop](#the-llm-enforcement-loop) runs for it and the feature
carries `regions` and `localization` (but still no `score` or `verdict`).

Errors: **400** for missing/unparseable `features`, an empty array, an invalid
`ocr` value, an unknown `regions` layer, an empty file, or bytes that are not a
supported document.

---

### `GET /jobs/{job_id}/artifacts`

The manifest for a job's artifact directory: `files[]` (name, bytes, content
type, url), the criterion → slug map, `page_geometry`, the request's `regions`
options, `dropped`, `notes`, `total_bytes`, and `expires_at`. The same
information as the result's `artifacts` block, fetchable without pulling the
(possibly large) result.

```bash
curl http://localhost:8005/jobs/abc123/artifacts | jq '{files: [.files[].name], criteria}'
```

```json
{
  "files": ["manifest.json", "p0.base.jpg", "p0.layer.png", "p0.preview.jpg", "p0.svg", "regions.json"],
  "criteria": {
    "has sky":   {"slug": "has-sky-39f0",   "count": 1, "pages": [0], "sources": ["cv"]},
    "has water": {"slug": "has-water-4b21", "count": 2, "pages": [0], "sources": ["cv"]}
  }
}
```

**404** when the job is unknown or never had artifacts (it was submitted
without `regions`). **410** when the job exists and its result says it *did*
have artifacts, but the directory is gone — swept past the TTL, or explicitly
deleted. The distinction matters: a caller holding a stored URL can tell
"expired" from "wrong URL" without guessing.

---

### `GET /jobs/{job_id}/artifacts/{name}`

Stream one file with its content type (`image/svg+xml`, `image/png`,
`image/jpeg`, `application/json`). `name` is validated against a strict pattern
and against the directory — no path traversal, no listing the volume.

With no query parameters the stored file is streamed as-is. With any of them
the layer is **re-rendered from `regions.json`** by the same functions that
produced the stored one, so a filtered view is always consistent with the
combined one.

| Param | Effect |
|---|---|
| `criterion=<slug>` (repeatable) | Only that criterion's / those criteria's regions |
| `source=cv,ocr,pdf-text,llm,detector,diff` | Only regions from those sources |
| `attempt=<n>` | LLM criteria only: the box from [enforcement-loop](#the-llm-enforcement-loop) attempt *n*, accepted or not. This is how a **rejected** box is viewed on its own. Non-LLM regions have no attempt and are dropped by this filter |
| `accepted=true` \| `false` | LLM criteria only. `true` — **the default whenever any filter is given and no `attempt` is** — keeps the accepted attempt, matching what the stored combined layer shows. `false` keeps the rejected ones |

Three families of layer live in one directory and all three take these
filters: `p{n}.<fmt>` (the subject), `diff-e{i}-p{n}.<fmt>`
([change detection](#change-detection)), and `e{i}.p{n}.<fmt>`
([example layers](#layers-for-the-examples), whose geometry comes from
`e{i}.regions.json`).

```bash
# The combined overlay
curl http://localhost:8005/jobs/abc123/artifacts/p0.svg -o p0.svg

# Just one criterion, re-rendered on demand
curl "http://localhost:8005/jobs/abc123/artifacts/p0.svg?criterion=has-water-4b21" -o water.svg

# Just that criterion's geometry, no picture
curl "http://localhost:8005/jobs/abc123/artifacts/regions.json?criterion=has-water-4b21" | jq .
```

SVG renders are cheap enough to serve uncached. A filtered PNG or preview for a
**single** criterion is cached into the directory as `p0.<slug>.layer.png` /
`p0.<slug>.preview.jpg` on first request — the same names
`regions.layers_per_criterion` pre-renders, so the lazy and eager paths cannot
produce different files — and counts against
`CLASSIFIER_ARTIFACT_MAX_BYTES`.

Errors: **400** for an unknown criterion slug or a name that is not a
renderable layer (you cannot filter `manifest.json`); **404** for an unknown
job or a file that is not in the directory; **410** when the directory has been
swept or deleted. A filtered `preview` needs `p{n}.base.jpg`, which exists only
when the job asked for the `preview` layer — without it the response is a 404
that says so and points at the SVG or PNG instead.

---

### `GET /jobs/{job_id}/artifacts.zip`

Every file in the directory as one zip, built on the fly and streamed —
nothing extra is stored. The "download the layers" path. Entries are prefixed
with the job id, so unpacking several jobs side by side does not collide.

```bash
curl http://localhost:8005/jobs/abc123/artifacts.zip -o layers.zip && unzip -l layers.zip
```

Built from the files that are actually there, so a job the byte cap trimmed
produces a zip of what survived rather than failing.

---

### `DELETE /jobs/{job_id}/artifacts`

Remove the artifact directory but keep the job row and its result — the inline
regions (up to `CLASSIFIER_INLINE_REGIONS_MAX`) survive. For a caller that
copied the layers elsewhere and wants the disk back before the TTL. Returns
**204**; **404** when the job is unknown or has no directory. Afterwards the
other artifact endpoints answer **410**.

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
  "metadata": {"type": "assess", "request_id": "req-xyz"},   // type: assess | compare | locate
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

Delete a job record **and its artifact directory**. Returns `204 No Content`;
**404** when the job is unknown. The directory removal is wired in as
`common.jobs.router.build_router`'s `on_delete` hook, so a deleted row can
never leave layer files that nothing points at. A failure in the cleanup is
logged, not raised — the caller asked for the row to be gone, and it will be.

To free the disk while keeping the row, use
[`DELETE /jobs/{job_id}/artifacts`](#delete-jobsjob_idartifacts) instead.

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

[`unit-tests/classifier/regions/`](../../unit-tests/classifier/regions/) does
the same for regions: a synthetic scene with sky, vegetation and a pool in
known places, a two-column text page, and the document fixtures referenced (not
copied) for the `ocr` and `pdf-text` overlays — with the expected geometry per
file, curl examples for all four artifact endpoints, and a list of things that
should never happen (a region outside the page, a different score with
`regions` on, `"artifacts": {}` where it should be `null`). Mirrored by the
Postman collection's **Regions** and **Artifacts** folders.

The collection's **Documents + regions** folder re-sends every document
fixture with `regions` on, so the same scores come back with the geometry that
produced them: `pdf-text` rectangles on both pages of the native invoice,
tilted `ocr` polygons on the scan, all four sources at once on a photographed
letter that now carries a logo, a `RECEIVED` stamp and a signature at
documented coordinates, and the two honest negatives — a whole-page
measurement that produces no box, and a `.txt` / `.docx` with no pixel space
for one.

### Running the fixtures as a suite

[`unit-tests/classifier/regions_report.py`](../../unit-tests/classifier/regions_report.py)
executes the collection's folders end to end — submit, poll, download the
layers — then **re-draws every region from `regions.json` onto the original
fixture** with `common.vision.annotate` and puts that picture next to the
service's own `p{n}.preview.jpg`. The two are produced by different code from
the same numbers, so a difference between them is itself the finding. It
writes a self-contained `index.html`, a machine-readable `summary.json`, and
exits non-zero when an expectation in
[`regions_expected.json`](../../unit-tests/classifier/regions_expected.json)
does not hold.

```bash
uv run --package classifier python unit-tests/classifier/regions_report.py
uv run --package classifier python unit-tests/classifier/regions_report.py --local
```

`--local` mounts this app in-process with `TestClient` on a throwaway `/data`,
so everything but the vision model is verifiable with no container and no GPU.
Setup, how to read the report, and how to add a case:
[`unit-tests/classifier/REGIONS_REPORT.md`](../../unit-tests/classifier/REGIONS_REPORT.md).
