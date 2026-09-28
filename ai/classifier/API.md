# Classifier API

FastAPI service that assesses **single-page documents** — JPEG/PNG photos, a
one-page PDF (native or scanned), plain text, and .docx — against a list of
criteria, each answered by an OpenCV detector, deterministic text matching,
the open-vocabulary detector, or the vision LLM. The LLM is Meta's
Muse-Glimmer-30B served by the `muse-glimmer` vLLM container (`VISION_LLM_API`
/ `VISION_LLM_MODEL` / `VISION_LLM_MAX_TOKENS` in the `## Classifier` block of
`.env`); see [VLLM.md § Muse Glimmer 30B](../vllm/VLLM.md#muse-glimmer-30b--tensor-parallel--dflash-speculative-decoding).
Muse Glimmer's reasoning depth is set per call from `VISION_LLM_REASONING_STRENGTH`
(default `low`, driven by `CLASSIFIER_REASONING_STRENGTH` in `.env`); its thinking
is stripped server-side by the `muse_glimmer` reasoning parser, so only the JSON
answer reaches the classifier.

Base URL (direct): `http://<host>:8005`
Base URL (via LiteLLM passthrough): `http://<host>:4001/v1/classifier`

All passthrough requests require `Authorization: Bearer <master-key>`.

**One analysis endpoint.** `POST /assess` is the only one. `/locate` and
`/assess/compare` were removed and answer **404** like any unknown route.
`/locate`'s job — geometry without a judgement — is now `"score": false` on a
criterion of `/assess` (see [§ Locate without judging](#locate-without-judging--score-false)).

---

## Module layout

One level of subpackages, imported by bare name — the container sets
`PYTHONPATH=/app/ai/classifier` and runs `uvicorn main:app`, so a module reads
`from analysis.pipeline import analyze_document`, never `from classifier.…`.

```
ai/classifier/
  main.py              FastAPI app, lifespan, router registration — nothing else
  config.py            every constant and env knob in the service, one file
  logger.py            the one "classifier" logger every module imports
  middleware.py        correlation IDs: the ContextVar, the filter, the middleware
  metrics.py           the Prometheus objects produced in one module, read in none
  utils.py             verdict_from_score — the PASS/MARGINAL/FAIL line
  api/                 the HTTP layer
    schemas.py         AssessRequest, DocumentInput, CriterionInput + the cross-criterion rules
    criterion_options.py  LLMOptions / TextOptions / CVOptions / DetectorOptions: defaults, caps
    assess.py          POST /assess — JSON or multipart, one model, the submit-time checks
    introspection.py   GET /criterion-types, /hints, /cv-detectors, /document-kinds, /health
    artifacts.py       the four /jobs/{id}/artifacts routes and the lazy layer renderer
  analysis/            the engine
    loading.py         bytes → Document: content type, EXIF, kind, URL + SSRF, single-page rule
    ocr.py             the OCR engine singleton
    geometry.py        the ≤1000-px working image and its PageGeometry
    context.py         DocumentContext: what every criterion shares, read-only; the OCR memo
    outcome.py         Outcome — the one shape every evaluator returns
    cv_eval.py         the `cv` evaluator (and its fallback)
    text_eval.py       the `text` evaluator
    llm_eval.py        the `llm` evaluator: one scoring call, then maybe the box loop
    detector_eval.py   the `detector` evaluator
    scheduler.py       dependency waves, the per-job cap, error isolation
    weighting.py       the weighted overall score, and `complete`
    pipeline.py        analyze_document: context → schedule → weigh → store → assemble
  regions/             "where did you find it" → files and result fields
    store.py           the one ArtifactStore instance for the process
    collect.py         visible_regions, inline_regions
    artifacts.py       regions.json, text.<key>.json, the base image, the manifest, render_layer
    sweeper.py         the TTL task, the DELETE hook, the disk gauges
  llm/
    prompts.py         the single-criterion scoring prompt, and the loop's two small ones
    client.py          LLM_CALLS (the process-wide limit), call_vllm, call_vllm_json, the encoder
    validate.py        find the answer, clamp it, derive the verdict from the score
    boxes.py           the bounding-box enforcement loop
  cv/                  REGISTRY + get_detector in __init__.py
    quality.py         check_blur, check_exposure — whole-page, no regions
    features.py        vegetation, sky, faces, water, text — each with regions
    regions.py         the two region shapes the detectors build
  detector/
    client.py          the ai/detector HTTP client (transport only)
  jobs/
    payloads.py        the one payload shape (schema 2)
    runners.py         run_assess
    queue.py           ClassifierQueue + the registry / queue / sweeper singletons
  bin/
    grounding_experiment.py   operator tool — runs where the model is
```

Import direction is strictly one way:

```
config → logger / metrics / utils → api.criterion_options → api.schemas
       → cv, llm, detector, regions → analysis → jobs → api (routers) → main
```

`analysis` may import `regions`, `llm`, `cv` and `detector`; `regions` must
never import `analysis` — that is what keeps storing geometry additive, so
asking *where* can never move a score (`analysis.pipeline` hands `regions`
plain outcomes and a callback). `api/__init__.py` deliberately imports
nothing: `regions`, `analysis` and `llm` all import `api.schemas`, and a
router import there would close the cycle back through `jobs`.
`api.criterion_options` imports `cv` (to know whether a `cv` name has an
OpenCV detector) and `detector.client` (whether DETECTOR_URL is set); neither
imports back.

---

## Async job pattern

`POST /assess` returns **202 Accepted** immediately with a job ID. Poll
`GET /jobs/{job_id}` until `phase` is `"completed"` or `"failed"`, then read
the `result` field.

```
POST /assess  →  {"job_id": "abc123", "phase": "pending"}
                         ↓  poll
GET /jobs/abc123  →  {"phase": "completed", "result": {...}, "metadata": {...}}
```

Job tracking is powered by the shared `common.jobs` package
(`shared/common/src/common/jobs/`), which the interceptor service also
uses. The endpoint shapes and response fields are documented there.

### Concurrency and durability

The jobs table **is** the queue. Inside a job, every criterion is its own unit
of work — criterion + document in, one result out — so there are four limits,
the last three process-wide (`common.jobs.limits.ConcurrencyLimit`, an
`asyncio.Semaphore` that is safe across event loops, since every worker runs
in the one process):

| Knob | Bounds | Default |
|---|---|---|
| `CLASSIFIER_MAX_CONCURRENT` | jobs at once — worker tasks each atomically claiming the oldest `pending` row | 4 |
| `CLASSIFIER_MAX_CRITERIA_PER_JOB` | criteria ONE job evaluates at once. A dependant waiting on its `depends_on` does not hold a slot | 2 |
| `CLASSIFIER_MAX_LLM_CALLS` | vision-model requests in flight across ALL jobs, every call type — scoring, box ask, refine, verify. Acquired in `llm/client.py` around the HTTP request itself, so no call path gets around it; a retry takes a fresh slot. Size it to the model: `muse-glimmer` runs `--max-num-seqs 4` | 4 |
| `CLASSIFIER_OCR_WORKERS` | OCR passes at once (one ONNX inference each, in a worker thread) | 4 |

Phases: `staging` → `pending` → `processing` → `completed` | `failed`.
`staging` is the few milliseconds between the row being created and its
payload landing on disk; workers never claim it.

Job inputs (the document bytes, resolved AT SUBMIT, plus the validated
criteria) are written to `PAYLOAD_DIR` (default `/data/payloads`, same volume
as the DB) and deleted when the job reaches a terminal phase. On startup the
service requeues any `processing` rows a previous container left behind and
sweeps orphaned payload files, so a restart mid-job re-runs the job rather
than losing it.

**Retention.** `JOB_TTL_HOURS` (24) is enforced: the artifact sweeper deletes a
terminal job's artifact directory **and its row** past the TTL, on startup and
every `CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S`. A job still `pending` or
`processing` is never swept, however old.

Metrics: `classifier_job_queue_depth` (rows in `pending`),
`classifier_jobs_in_flight` (rows being processed), `classifier_llm_calls_total`
and `classifier_llm_latency_seconds` (every model request).

### Deploying this version

**Drain the queue before recreating the container.** Payloads are now
`schema: 2` (one request shape, per-criterion options); a payload queued by an
older container — an old-shape assess, or any `compare` / `locate` job — is
refused by the new runner with a message naming why, and the job fails rather
than half-running. Wait for `classifier_job_queue_depth` to reach 0 (or
`GET /jobs?phase=pending` to come back empty) before `up -d --force-recreate`.

---

## Documents

Every upload is normalised by
[`common.documents`](../../shared/common/src/common/documents/__init__.py) into
**one page** that may carry an image, a text layer, or both. Criteria then run
against whichever of those they need, so the same criteria list works across
kinds.

| Kind | Extensions | Detected by | Page image | Native text |
|---|---|---|---|---|
| `image` | `.jpg` `.jpeg` `.png` | magic bytes `FF D8 FF` / `89 50 4E 47 0D 0A 1A 0A` | yes (EXIF-rotated) | no — needs OCR |
| `pdf` | `.pdf` | `%PDF-` in the first 1 KB | yes, rendered at `CLASSIFIER_PDF_RENDER_DPI` (150) | yes when the PDF is digital; a scan has none |
| `txt` | `.txt` | decodes as UTF-8 (BOM ok), no NUL bytes, mostly printable | **no** | yes |
| `docx` | `.docx` | ZIP magic `PK\x03\x04` containing `word/document.xml` | **no** | yes (paragraphs + table cells in document order) |

**Detection is by content, never by filename or `Content-Type`.** A PDF
uploaded as `image/png` still loads as a PDF. The declared content type of a
multipart part is only used for an early allowlist reject (`image/jpeg`,
`image/png`, `application/pdf`, `text/plain`, the .docx type,
`application/octet-stream`, and `application/msword` — the last one only so a
legacy `.doc` reaches the magic-byte check and gets its specific "convert to
.docx" 400). The kind is detected on the `POST /assess` request itself, so an
unsupported file is a 400 at submit time and never becomes a job.

`GET /document-kinds` returns this table live from a running container, plus
the OCR engine's actual availability.

### Limitations

* **Single page only.** A PDF with more than one page is refused at submit —
  HTTP 400, `only single-page PDFs are supported (this one has N pages)`. The
  page count is read with PyMuPDF from the cross-reference table
  (`common.documents.pdf_page_count`), without rendering anything. Photos,
  `.txt` and `.docx` are one page by nature (python-docx reads XML, not a
  laid-out page).
* **Legacy `.doc` is not supported.** An OLE2 file (`D0 CF 11 E0 A1 B1 1A E1`)
  is rejected with HTTP 400 telling the caller to convert to `.docx`.
* **`.txt` and `.docx` have no page image**, so `cv` and `detector` criteria
  against them come back `status: "skipped"` and are excluded from the
  weighted score — "not applicable", not "failed" — the `llm` prompt is
  text-only, and a `score: false` criterion is refused (there is nowhere to
  locate anything).
* **One image per LLM prompt** — see below.

### OCR

Scans and photos have no text layer, so a `text` criterion would have nothing
to search and the vision model would have to read pixels alone. The bundled
OCR engine (RapidOCR / PP-OCRv6 ONNX, `CLASSIFIER_OCR_ENGINE=rapidocr`, models
baked into the image at build time) fills that gap.

OCR is a **per-criterion** setting — `options.ocr` on every `text` and `llm`
criterion — and it decides the text layer that criterion reads:

| `options.ocr` | The criterion's text layer |
|---|---|
| `auto` (default) | Recognised when the page has an image and its native text is under `CLASSIFIER_OCR_MIN_NATIVE_CHARS` (20); the native text otherwise. A native PDF / .txt / .docx therefore never loads the models. |
| `always` | Recognised whenever the page has an image, regardless of native text. |
| `never` | Native text only. A `text` criterion on an image-only document scores 1/FAIL with `"No text available for this document under ocr=never…"`. |

**One pass per setting.** The text layers are memoised per job by their
resolved settings (`analysis.context`): criteria with identical settings share
ONE recognition pass; different settings each get their own, run concurrently
up to `CLASSIFIER_OCR_WORKERS`. OCR runs only for criteria that need text — a
job of `cv` criteria never touches the engine. The layers are produced by
`common.documents.recognize_text_layer`, which never mutates the page, which
is what lets two settings coexist on one document.

`CLASSIFIER_OCR_ENGINE=none` disables OCR deployment-wide (`GET
/document-kinds` reports `ocr.available: false`); an `auto` / `always`
criterion then gets the native layer, and `document_info.ocr.layers[].note`
says OCR was wanted but unavailable.

Every layer the job produced is listed in `document_info.ocr.layers` (`key`,
`mode`, `ran`, `source`, `chars`, `confidence`, `note`) and stored as
`text.<key>.json` — see [§ The text a criterion searched](#the-text-a-criterion-searched).

### What the vision model sees

Each `llm` criterion is **one scoring call of its own**: the page image (the
≤1000-px working copy) plus the text layer its `options.ocr` produced — a
`DOCUMENT TEXT (extracted, may contain OCR errors)` block, truncated at
`CLASSIFIER_TEXT_CHAR_BUDGET` (60 000 characters) with a note when truncated —
and the rubric its `options.hint` selects. The answer is one flat JSON object,
`{"score", "verdict", "confidence", "reason"}`; the verdict is always
recomputed from the clamped score. What was sent is in the result's
`detail`: `{hint, image_sent, text_sent: {chars, truncated, budget, source}}`.

> **One image per prompt.** `muse-glimmer` is served **without**
> `--limit-mm-per-prompt`, so vLLM accepts a single image per request; a second
> one fails the whole call. Documents are single-page, so the page image is
> the one image.

A document with no page image sends a text-only prompt (still
`response_format: json_object`), and the system prompt tells the model to judge
from the text.

---

## Request

One request, accepted two ways, parsed into ONE pydantic model
(`api.schemas.AssessRequest`) — so a JSON caller and a multipart caller can
never be treated differently.

**JSON** (`Content-Type: application/json`):

```json
{
  "document": {"type": "base64", "data": "<base64>", "filename": "invoice.pdf"},
  "criteria": [ … ]
}
```

| `document.type` | `data` is | Notes |
|---|---|---|
| `base64` | the file, base64-encoded | |
| `url` | an `http(s)` URL | Fetched **at submit** (the single-page check needs the bytes), after the SSRF check (`common.net`: private, loopback and link-local addresses refused). 400 when blocked, 502 when the fetch fails |
| `text` | the document text itself | Treated as a `.txt`, UTF-8; `document_info.filename` is `inline.txt` |

`filename` is optional and only recorded in `document_info`.

**Multipart** (`Content-Type: multipart/form-data`):

| Field | Required | Description |
|---|---|---|
| `file` | one of `file` / `text` | The document. The legacy name `image` is still accepted |
| `text` | one of `file` / `text` | Inline text, same as the JSON `type: "text"` |
| `criteria` | no | The same JSON array as the JSON body's `criteria`, as a string. Omitted: the four default quality criteria |

Any other form field is a 400 — the removed `ocr` and `regions` fields are
named, with where each went (`options.ocr` on each criterion; regions are
always stored). A `Content-Type` that is neither JSON nor multipart is
**415**.

### `CriterionInput`

The top level holds only what every type shares; everything type-specific is
in `options`:

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string (1–200) | — | The criterion. Unique in the request. For `text` it is also the default `pattern`; for `cv` it is matched against the OpenCV registry; for `detector` it is the text prompt |
| `type` | `llm` \| `text` \| `cv` \| `detector` | `llm` | The evaluation path — see below |
| `weight` | number > 0 | `1` | Relative weight in the overall score |
| `depends_on` | string \| null | `null` | Another SCORED criterion that must **PASS** first — see [§ Dependencies](#dependencies) |
| `score` | bool | `true` | `false` = locate without judging — see [§ Locate without judging](#locate-without-judging--score-false) |
| `options` | object | `{}` | Per type, below. `GET /criterion-types` serves each type's JSON schema, resolved defaults and caps |

| `type` | `options`, with defaults |
|---|---|
| `llm` | `hint` (`quality` \| `presence` \| `auto`, default `auto`), `boxes` (default **`false`** — the [bounding-box loop](#the-llm-enforcement-loop)), `max_attempts` (default and cap `CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS`; may be lowered, never raised), `ocr` (`auto` \| `always` \| `never`, default `auto` — the text layer sent with the prompt) |
| `text` | `pattern` (default: the name; max 500 characters), `match` (`contains` default \| `exact` \| `regex` \| `fuzzy`), `case_sensitive` (`false`), `fuzzy_threshold` (`0.85`, 0–1), `min_count` (`1`, 1–1000), `ocr` (`auto`) |
| `cv` | `fallback` (`detector` \| `llm`) — what answers when no OpenCV detector matches the name. Default: `detector` when `DETECTOR_URL` is configured on this container, `llm` otherwise |
| `detector` | `threshold` (0–1, default `DETECTOR_MIN_SCORE`) |

**Validation** — every one is a **400 at submit**, naming the field, and
nothing is queued:

* an unknown option key, a value of the wrong type (options are strict: `"2"`
  is not an integer, `"yes"` is not a boolean), or a value past a cap
  (`criteria.0: options.max_attempts: max_attempts 9 exceeds this server's cap
  (CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS=3)…`);
* an old top-level key (`hint`, `pattern`, `match`, `case_sensitive`,
  `fuzzy_threshold`, `min_count`) — "these keys moved into 'options'";
* an unusable text pattern (empty, over 500 characters, an invalid regex),
  compiled through the matcher's own path;
* duplicate names; a `depends_on` naming an unknown criterion, itself, or a
  `score: false` criterion; a dependency cycle (`dependency cycle: a -> c -> b
  -> a`);
* `score: false` on a criterion that cannot produce geometry, or on a .txt /
  .docx;
* a `detector` criterion (or a `cv` one with an explicit `fallback:
  "detector"` and no OpenCV match) when `DETECTOR_URL` is empty.

Each result echoes `options_used`: the options after defaults and caps, so
`pattern` shows the name it defaulted to and `max_attempts` the cap it was
held to.

### The four types

| `type` | Answered by | Cost |
|---|---|---|
| `llm` | One vision-model call for this criterion — the page image + its text layer, the rubric for its `hint`. With `options.boxes` the enforcement loop runs after | 1 call (+ up to 3 per loop attempt) |
| `text` | `common.documents.match_text` over its text layer (native or OCR'd) | none |
| `cv` | A registered OpenCV detector on the working image (in a worker thread). With no match, its `fallback`: the detector (scored from boxes) or the llm (default llm options: hint `auto`, no boxes, ocr `auto`). `method` says which answered, and `reason` says a fallback was used | none, or the fallback's |
| `detector` | The open-vocabulary detector service, one call with the name as the label, scored from the boxes | one detector call |

### `text` criteria

Deterministic, free (no tokens), and exact about *where* a phrase was found.

| `match` | Semantics | Use it for |
|---|---|---|
| `contains` | Substring anywhere (metacharacters are literal) | The default; known exact wording |
| `exact` | Whole word / whole line, word-boundary anchored | `"Total"` must not match `"Subtotal"` |
| `regex` | Python `re.search` | Case numbers, dates, dollar amounts |
| `fuzzy` | Best sliding-window similarity (`difflib`) over words | **OCR'd text**, where `Notice to Owner` arrives as `Notlce to 0wner` |

| Situation | Score | Verdict |
|---|---|---|
| Found (`count >= min_count`) | `10` | PASS |
| `fuzzy` near miss, best ratio ≥ 0.5 | `2`–`6`, scaled between the 0.5 floor and `fuzzy_threshold` | FAIL / MARGINAL |
| Not found (or a fuzzy ratio below 0.5) | `1` | FAIL |
| The text layer is empty | `1` | FAIL, with a reason naming the `ocr` setting |

`confidence` is 100 for native text, the OCR confidence ×100 for recognised
text, and 0 when there is none. `detail` carries the match record:

```json
"detail": {
  "found": true, "count": 1, "best_ratio": 0.8571, "mode": "fuzzy",
  "pattern": "Notice to Owner",
  "snippets": [{"text": "…33601 NOTICE TO OWNER Under Florida law…", "ratio": 0.8571}],
  "searched_chars": 577,
  "case_sensitive": false, "min_count": 1, "fuzzy_threshold": 0.85, "text_source": "ocr"
}
```

The exact text searched is not inlined — it is linked, as
`artifacts.text` (see [§ The text a criterion searched](#the-text-a-criterion-searched)).

### Scoring rubrics (`llm`)

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

Names are matched exactly (case-insensitive), then by `difflib` at
`CV_NAME_FUZZY_CUTOFF` (0.8) so typos still land — `has textt` → `has text`.
Anything less similar uses the criterion's `fallback`. See `GET /cv-detectors`
for the live list.

### Dependencies

`depends_on` names another **scored** criterion. The scheduler runs criteria
in waves: a dependant is **not evaluated at all** until its dependency has
finished; if the dependency's verdict is not PASS (or it errored, or was
itself skipped) the dependant comes back `status: "skipped"` without being
evaluated — no model call, no OCR pass — with a reason naming the dependency.
Chains propagate (A → B → C all skip when A fails). Each dependant waits on
exactly the criterion it names, so an independent criterion is never held back
by an unrelated slow one.

### Locate without judging — `score: false`

`score: false` keeps the geometry and drops the judgement: the criterion runs
exactly as a scored one would and returns its `regions`, but its `score`,
`verdict` and `confidence` are `null`, it is excluded from the weighted score
and the overall verdict, and no criterion may depend on it. This is what
`/locate` used to be:

```json
"criteria": [
  {"name": "has text", "type": "cv", "score": false},
  {"name": "Notice to Owner", "type": "text", "score": false, "options": {"match": "fuzzy", "ocr": "always"}},
  {"name": "a signature", "type": "llm", "score": false, "options": {"hint": "presence", "boxes": true}}
]
```

An `llm` criterion with `score: false` still makes its presence call — the box
loop gates on it — and then drops the answer. A criterion that cannot produce
geometry is refused at submit: an `llm` criterion without `options.boxes` (or
with a `quality` hint — sharpness is not a place), the whole-page `cv`
measurements (`sharpness`, `exposure`), and a `cv` name that falls back to the
llm.

---

## Result shape

Inside `job.result`, `schema_version: 2`:

```json
{
  "schema_version": 2,
  "document_info": {
    "kind": "pdf", "filename": "notice.pdf", "content_type": "application/pdf",
    "size_bytes": 5102, "width": 1240, "height": 1755, "has_image": true,
    "native_text_chars": 574,
    "ocr": {"engine": "rapidocr", "min_native_chars": 20,
            "layers": [{"key": "auto", "mode": "auto", "ran": false, "source": "native",
                        "chars": 574, "confidence": null, "note": null}]},
    "llm_text_char_budget": 60000,
    "detector": {"configured": false, "url_host": null, "calls": 0, "…": "…", "used": false}
  },
  "assessment": {
    "overall_score": 9,
    "overall_verdict": "PASS",
    "complete": true,
    "weighted_score_breakdown": {
      "formula": "sum(score * weight) / total_weight",
      "total_weight": 7.0, "weighted_sum": 64.0, "unrounded_average": 9.1429,
      "final_score": 9, "partial": false,
      "excluded": {"where is the signature": "score: false"},
      "per_criterion": {
        "Notice to Owner": {"score": 10, "weight": 4.0, "contribution": 40.0},
        "…": "…"
      }
    },
    "per_criterion_scores": {
      "Notice to Owner": {
        "status": "ok", "type": "text", "method": "text", "scored": true,
        "score": 10, "verdict": "PASS", "confidence": 100,
        "reason": "Found 1x via fuzzy match (best ratio 0.86).",
        "detail": {"found": true, "count": 1, "…": "…"},
        "regions": [{"page": 0, "kind": "box", "source": "pdf-text", "…": "…"}],
        "regions_truncated": false,
        "artifacts": {
          "slug": "notice-to-owner-1f3a",
          "regions_url": "/jobs/abc123/artifacts/regions.json?criterion=notice-to-owner-1f3a",
          "layers": {"svg": "/jobs/abc123/artifacts/p0.svg?criterion=notice-to-owner-1f3a", "…": "…"},
          "attempts": [],
          "text": {"key": "auto", "url": "/jobs/abc123/artifacts/text.auto.json",
                   "source": "native", "chars": 574}
        },
        "localization": null,
        "options_used": {"pattern": "Notice to Owner", "match": "fuzzy", "case_sensitive": false,
                         "fuzzy_threshold": 0.85, "min_count": 1, "ocr": "auto"},
        "error": null
      }
    }
  },
  "verdict": "PASS",
  "page_geometry": {"page": 0, "width": 1240, "height": 1755, "working_scale": 0.5694,
                    "pdf_points": [595.2, 842.4]},
  "artifacts": {"files": [ … ], "layers": {"svg": "/jobs/abc123/artifacts/p0.svg", "…": "…"},
                "zip_url": "/jobs/abc123/artifacts.zip", "…": "…"}
}
```

**Every criterion has the same keys**, whatever its type or status:

| Key | Meaning |
|---|---|
| `status` | `ok` \| `error` \| `skipped` |
| `type` | The criterion's `type` |
| `method` | The path that actually answered — for a `cv` criterion with no OpenCV detector, its fallback's (`detector` / `llm`); `null` when skipped by a dependency |
| `scored` | The criterion's `score` flag |
| `score` / `verdict` / `confidence` | The judgement; `null` when not scored (`score: false`, skipped, error) |
| `reason` / `detail` | One or two sentences / structured diagnostics |
| `regions` | Where, in original page pixels; capped at `CLASSIFIER_INLINE_REGIONS_MAX` (`regions_truncated` says so) |
| `artifacts` | Per-criterion links — layer URLs (rendered on first fetch) and `text`, the text layer's link; `null` when there is neither geometry nor text |
| `localization` | `llm` criteria: the enforcement loop's record, `{"attempts": [], "accepted_attempt": null, "calls": 0}` when it did not run. `null` for the other types |
| `options_used` | Options after defaults and caps |
| `error` | Why, when `status` is `error` |

**A failed criterion fails alone.** A model HTTP error, an answer that could
not be parsed after `MAX_LLM_RETRIES`, a detector outage — each gives that
criterion `status: "error"` with the message in `error`; the job still
completes. If any **scored** criterion errored the assessment is incomplete:
`complete: false`, `overall_score` and `overall_verdict` (and `verdict`)
`null`, and the partial weighted score of what did succeed is still in
`weighted_score_breakdown` with `partial: true`. `complete` is `true`
otherwise — including when nothing was scored at all (every criterion `score:
false`), where the breakdown is `null`.

`image_info` was removed: its width, height, content type and size are in
`document_info`. `page_geometry` is a single object (`null` for .txt / .docx);
`document_info` has no per-page lists, `page_count`, `truncated_pages` or
`llm_image_page` any more.

**Skipped by a dependency:**

```json
"meter value is readable": {
  "status": "skipped", "type": "llm", "method": null, "scored": true,
  "score": null, "verdict": null, "confidence": null,
  "reason": "Skipped - dependency 'has electrical meter' did not pass (verdict: FAIL).",
  "…": "…"
}
```

**Not applicable** (a `cv` criterion on a .txt / .docx) is the same shape with
`method: "cv"` and a reason naming the kind.

---

## Regions and layers

A score says *whether*. A **region** says *where*: a box or polygon in the
page's original pixels, attached to the criterion that found it. **Every job
stores them** — there is no option to turn on — and storing them is strictly
additive: the geometry is read off the results the evaluators already
produced, so a score can never move because of it.

### What can localise, and what cannot

| Criterion | Source | What you get |
|---|---|---|
| `cv` — `has faces` | `cv` | One box per Haar detection |
| `cv` — `has vegetation` / `has sky` / `has water` | `cv` | Simplified contour polygons above a minimum area |
| `cv` — `has text` | `cv` | Adjacent high-density 32×32 blocks merged into boxes — roughly one per paragraph or column |
| `cv` — `sharpness` / `exposure` | — | **Nothing.** A whole-page measurement does not fabricate a full-page box |
| `cv` — a name no OpenCV detector matches, `fallback: detector` | `detector` | Boxes from the [open-vocabulary detector](#the-detector), and the criterion is **scored from them** — no LLM call |
| `detector` — any name | `detector` | The same, by name |
| `text` on an OCR'd layer | `ocr` | The OCR line polygon(s) the match landed on, with the recogniser's confidence as `score`. A hit spanning a line break yields one region per line |
| `text` on a native PDF | `pdf-text` | PyMuPDF rectangles — `search_for` for `contains`/`exact`, word-span reconstruction for `regex`/`fuzzy`. Carries `attrs.pdf_rect` in **points** as well as page pixels |
| `text` on `.txt` / `.docx` | — | Nothing — no geometry exists. The hits are still in `detail.snippets` |
| `llm` with `options.boxes`, hint `presence`/`auto`, scored ≥ 7 | `llm` | A box the model drew and then confirmed by looking at a crop of it, plus **every rejected attempt**. See [The LLM enforcement loop](#the-llm-enforcement-loop) |
| `llm` — anything else | — | Nothing. `localization` is the empty shape |

> **Approximation warning.** PDF `regex`/`fuzzy` rectangles are reconstructed
> from the word boxes covering the match. A phrase PyMuPDF splits differently
> from the text layer (hyphenation, a ligature, a column break mid-phrase) does
> not line up, and the region is omitted rather than guessed at.

### Coordinates

Every region is in **original page pixels** — after EXIF transpose for a photo,
after the raster render for a PDF. Detectors work on a ≤1000-px working image
and their output is divided by `working_scale` before it is stored, so a
consumer never has to know the working size existed. The frame is
`page_geometry`:

```json
"page_geometry": {"page": 0, "width": 1240, "height": 1755, "working_scale": 0.5694,
                  "pdf_points": [595.2, 842.4]}
```

`pdf_points` is the page's size in points for a PDF and `null` otherwise.
Regions keep `page: 0` and the layers keep the `p0.` prefix — the shared
`common.vision` code is page-aware, and a single page is simply page 0.

### What a job writes, and what renders on first fetch

| File | When | Notes |
|---|---|---|
| `regions.json` | always | The canonical geometry, keyed by criterion (not a flat list), plus `pages` and what the detector did |
| `manifest.json` | always | What is in the directory, the criterion → slug map (with each criterion's `text_layer` key), `page_geometry`, `dropped`, `notes`, `expires_at` |
| `text.<key>.json` | one per distinct text layer | The exact text a criterion searched — see the next section |
| `p0.base.jpg` | when the page has an image | The un-annotated page, JPEG q85. Every preview is composited from it |
| `p0.svg` / `p0.layer.png` / `p0.preview.jpg` | **on first fetch** | Rendered from regions.json by `regions.artifacts.render_layer`, then cached in the directory |
| `p0.<slug>.svg` / `.layer.png` / `.preview.jpg` | **on first fetch** | One criterion's layer, the same way — fetched by name, or as `p0.svg?criterion=<slug>` |

A job nobody looks at costs a JSON file, its text layers and a JPEG. The
result's `artifacts.layers` (and each criterion's `artifacts.layers`) lists the
layer URLs; they work whether or not the file exists yet. `artifacts.files` is
what is on disk at the time of the result.

### The text a criterion searched

A `text` criterion's verdict is only as good as the text it searched — and on
a scan, that text is OCR output. So every text layer a job produces is stored,
once per distinct setting, as `text.<key>.json`:

* **Key** — a short, stable name for the settings that produced the layer:
  `auto`, `always`, `never` (the criterion's `options.ocr`, which
  `options_used.ocr` agrees with). A non-default OCR setting (none exists per
  criterion today; `min_native_chars` and the engine are server-level) would
  add a hash — `auto-1a2b3c4d` — so two settings can never share a file.
* **Shared** — criteria with identical settings use the same layer and link
  the same file; nothing is duplicated.
* **Written for every job whose criteria read text** — native text included
  (`source: "native"` for a .txt, .docx or digital PDF), written by the OCR
  memo the moment the layer exists. A job of `cv` criteria only has no text
  layers.

```json
{
  "key": "always",
  "settings": {"mode": "always", "min_native_chars": 20, "engine": "rapidocr"},
  "source": "ocr",
  "engine": "rapidocr",
  "chars": 36,
  "confidence": 0.9,
  "text": "NOTICE TO OWNER\nTotal Due $4,850.00",
  "lines": [
    {"text": "NOTICE TO OWNER", "polygon": [[40, 100], [360, 100], [360, 140], [40, 140]], "confidence": 0.95},
    {"text": "Total Due $4,850.00", "polygon": [[40, 200], [520, 200], [520, 240], [40, 240]], "confidence": 0.85}
  ]
}
```

`text` is **byte-for-byte the string `match_text` searched**, so a failed
match can be diagnosed from the file alone. `lines` are the OCR lines with
their polygons in ORIGINAL page pixels, and `text` is exactly their
`"\n".join`; `lines` is empty for native text — a .txt / .docx has no
geometry, and a native PDF's layer is PyMuPDF's reading-order text, whose line
geometry is not recorded (hits on it are still located, as `pdf-text`
regions). `source: "none"` with an empty `text` is a real answer: `ocr:
never` on a scan.

Each `text` criterion's result links its layer — and so does each `llm`
criterion, for the layer its prompt carried:

```json
"artifacts": {"text": {"key": "always", "url": "/jobs/abc123/artifacts/text.always.json",
                       "source": "ocr", "chars": 36}}
```

The text itself is never inlined in the job result (it can be a whole
contract); the link plus `chars` is enough. `GET
/jobs/{id}/artifacts/text.<key>.txt` returns the same `text` as
`text/plain; charset=utf-8`, rendered from the JSON on each request. The
files are in the manifest (whose `criteria.<name>.text_layer` says which key
each criterion used) and the zip, are **never dropped by the byte cap**, and
go with the job.

### The detector

`ai/detector` is a text-prompted object detector: an image plus free-text
labels in, boxes out ([DETECTOR.md](../detector/DETECTOR.md)). It is what lets
an arbitrary `has X` criterion localise — and be *scored* — without a
vision-LLM call. The classifier reaches it at `DETECTOR_URL`; **empty means
off**, and then a `detector` criterion is refused at submit and a `cv`
criterion's fallback resolves to the llm.

| Spelling | What happens |
|---|---|
| `{"name": "has roof vent", "type": "detector"}` | Answered by the detector at `options.threshold`: boxes **and** a score |
| `{"name": "has roof vent", "type": "cv"}` | The same, when no OpenCV detector matches the name and the fallback is `detector` (the default while DETECTOR_URL is set), at `DETECTOR_MIN_SCORE` |

**Scoring from boxes.** A detector-scored criterion comes back with
`method: "detector"`:

| Best box score | Score | Verdict |
|---|---|---|
| ≥ `0.5` | 10 | PASS |
| ≥ the threshold (`DETECTOR_MIN_SCORE`, 0.25) | 7 | PASS — a real finding, but not one to build on |
| nothing above the threshold | 1 | FAIL |

`confidence` is the best box score as a percentage, and `detail` carries
`detector_matches`, `best_score` and `min_score`. Each region carries
`attrs.detector_score`. **A negative gets no LLM second opinion** — if you want
the model's judgement, give the criterion `"type": "llm"`.

**A failure fails the criterion.** A connection refused, a timeout, a 503
while the model loads: the criterion comes back `status: "error"` with the
client's sentence, and the job is `complete: false`. (There is no silent
fall-through to the LLM any more — a caller who asked for the detector is told
it did not answer.)

**Cost.** One HTTP call per detector criterion on the ≤1000-px working page;
the boxes are rescaled into original page pixels on arrival. What ran is in
`document_info.detector` and at the top of `regions.json`
(`model`, `device`, `calls`, `labels`, `detections`, `elapsed_ms`, `errors`).

### The LLM enforcement loop

A box from a vision model is a **claim**, not a measurement. Asked "where are
the solar panels?", a model will answer when there are none, when it cannot
tell, and when the honest answer is "somewhere in the upper half" — and every
one of those is four numbers that look exactly like a correct one. So nothing
here trusts the first answer:

```
attempt 1 ── ask ────── one criterion, the page image its scoring call used —
   │                    with a labelled 0–1000 coordinate grid drawn on it
   │                    (CLASSIFIER_LLM_BBOX_GRIDLINES, lines every
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
   └── retry ────────── re-ask with the rejection as feedback, up to the
                        criterion's options.max_attempts (default and cap
                        CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS, 3)
```

The verify call is what makes a box mean something: the page is deliberately
*not* shown alongside the crop, because a model that names a plausible region
of a photo it was already told contains the feature is not evidence, while one
that recognises the feature in 300×300 isolated pixels is.

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
by default and both are recorded on the attempt. What the second pass cannot
fix is *identification*: asked for a logo, a model that boxes the heading
naming the same company 90 units lower is precisely placed on the wrong thing.

**When it runs.** All four have to hold, or the loop costs nothing:

| Condition | Why |
|---|---|
| `options.boxes: true` | Off by default — up to 3 extra calls per attempt |
| hint `presence` or `auto` | "image sharpness" is a property of the whole page; boxing it would be a fabrication |
| the criterion's scoring call gave it **≥ 7** | Below that the model has just said the feature is absent |
| the document has a page image | `.txt` / `.docx` have no pixel space for a box |

**It never changes a score or a verdict.** The loop runs *after* the
criterion's scoring call, reads that call's score to decide whether to run at
all, and only adds keys. A criterion whose every attempt is rejected keeps the
score it had.

**Every attempt comes back**, accepted or not — a rejected box is evidence
about the model, and it is renderable on its own:

```json
"has solar panels": {
  "status": "ok", "method": "llm", "score": 9, "verdict": "PASS", "confidence": 85,
  "regions": [
    {"kind": "box", "points": [[216, 448], [378, 602]], "source": "llm", "score": 9,
     "attrs": {"attempt": 2, "accepted": true, "verify_score": 9, "refined": true}},
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
       "verify_reason": "a roof covered in panels",
       "coarse_bbox_grid": [230, 600, 430, 880], "refine_window_grid": [80, 400, 580, 1000],
       "refined": true}
    ],
    "accepted_attempt": 2,
    "calls": 4
  },
  "artifacts": {
    "slug": "has-solar-panels-9c1e",
    "layers": {"svg": "/jobs/abc123/artifacts/p0.svg?criterion=has-solar-panels-9c1e", "…": "…"},
    "attempts": [
      {"attempt": 1, "accepted": false,
       "svg": "/jobs/abc123/artifacts/p0.svg?criterion=has-solar-panels-9c1e&attempt=1", "…": "…"},
      {"attempt": 2, "accepted": true,
       "svg": "/jobs/abc123/artifacts/p0.svg?criterion=has-solar-panels-9c1e&attempt=2", "…": "…"}
    ]
  },
  "options_used": {"hint": "presence", "boxes": true, "max_attempts": 3, "ocr": "auto"}
}
```

`accepted_attempt` is `null` when nothing was accepted; `regions` then holds
only rejected boxes and the criterion's score is untouched.

`coarse_bbox_grid`, `refine_window_grid` and `refined` appear on an attempt
whose coarse box validated and so went through the refine pass: `bbox_grid`
is then the refined box when `refined` is `true`, and the coarse box kept
(with a `refine_reject` sentence) when it is `false`.

`attrs.detector_iou` (the IoU against the detector's best box for the same
criterion) is still computed by `llm.boxes.locate_criterion` when it is handed
detector regions — `bin/grounding_experiment.py` does — but an `/assess` job
no longer runs the detector alongside an `llm` criterion, so it does not
appear in job results.

**The combined layer shows the accepted box only.** `p0.svg` and any
`?criterion=` render with no explicit `attempt` show the accepted attempt.
`?attempt=n` renders attempt *n* (accepted or not), and `?accepted=false`
renders the rejected ones. `bbox_grid` is what the model literally said;
`bbox_px` is the same box in original page pixels, clamped to the page.

**Rejection wording is the retry's prompt.** Each `reject` is written to be
read by the model on the next attempt. Changing them changes the retry
behaviour, not just the log.

**Cost.** One ask per attempt, plus one refine and one verify per attempt
whose coarse box validated — at most `3 × max_attempts` small calls per
located criterion (`2 ×` with `CLASSIFIER_LLM_BBOX_REFINE=false`), each capped
at `CLASSIFIER_LLM_BBOX_MAX_TOKENS` (1024) and each counted against
`CLASSIFIER_MAX_LLM_CALLS`. Metric:
`classifier_llm_bbox_attempts_total{outcome=accepted|rejected_invalid|rejected_verify|exhausted}`.

**Before trusting it on a new model**, run the grounding experiment:

```bash
# On the box (it talks to VISION_LLM_API directly)
docker compose exec classifier python bin/grounding_experiment.py
docker compose exec classifier python bin/grounding_experiment.py \
  --images /data/roof-photos --criteria /data/criteria.json --out /data/grounding.json
```

If attempt-1 validity is under ~50%, prefer `detector` criteria as the primary
source and keep `llm` criteria with `options.boxes` for labels the detector
cannot name.

### Layer formats

| Format | File | Notes |
|---|---|---|
| SVG overlay | `p0.svg` | `viewBox` is the original page, so it composites over the page image at any size with no transform. One `<g id="c-<slug>" data-criterion="…" data-source="…">` per criterion, `<rect>`/`<polygon>` with `data-source`/`data-score` and a `<title>` tooltip. Legend embedded |
| Transparent PNG | `p0.layer.png` | Exactly the page's pixel size, alpha everywhere except strokes and translucent fills |
| Composited preview | `p0.preview.jpg` | `p0.base.jpg` with the layer burned in, JPEG q85, with a corner legend |
| Regions JSON | `regions.json` | Always written. Keyed by criterion |

Colour is one stable hue per criterion (hashed from the name); the source
shows in the stroke — solid for `cv`/`detector`/`pdf-text`, dashed for `ocr`,
dotted for `llm`.

### Tying a criterion to its artifacts

Every criterion gets a **slug**: its name lowercased with non-alphanumerics
collapsed to `-`, plus four hex digits of a hash of the exact name. The slug is
what appears in file names, in SVG group ids, and in the `?criterion=` query
parameter; `manifest.json` carries the slug ↔ name map.

```json
{
  "job_id": "abc123",
  "pages": [{"page": 0, "width": 900, "height": 700, "working_scale": 1.0, "pdf_points": null}],
  "detector": null,
  "criteria": {
    "has water": {
      "slug": "has-water-4b21", "type": "cv", "sources": ["cv"], "count": 2,
      "regions": [
        {"page": 0, "kind": "polygon", "points": [[90, 518], [430, 518], [430, 651], [90, 651]],
         "label": "has water", "score": 0.0713, "source": "cv",
         "attrs": {"area_px": 44812, "flat": true}}
      ],
      "localization": null,
      "text_layer": null
    }
  }
}
```

Each criterion result carries its own `artifacts` block — `null` when the
criterion found nothing and read no text, so an empty object never implies
URLs that would 404. `layers` holds one URL per format, filtered to the
criterion; `attempts` is empty for every path but `llm`.

### Inline vs the directory

| Data | In the artifact directory | Inline in the job result |
|---|---|---|
| Per-criterion regions | `regions.json`, complete | `regions`, capped at `CLASSIFIER_INLINE_REGIONS_MAX` (50), `regions_truncated` |
| Per-criterion links | derived from the manifest | `artifacts` |
| The text searched | `text.<key>.json` | only the link and `chars` (`artifacts.text`) |
| LLM localization | `regions.json`, per criterion | `localization` — **not capped**: every attempt, always |
| Page geometry | `manifest.json`, `regions.json` | `page_geometry` |
| Rendered layers | once fetched | **never** — only URLs |

### Disk and retention

`CLASSIFIER_ARTIFACT_MAX_BYTES` (50 MB) caps each job, enforced after the job
writes its directory and after every lazily rendered layer is cached. Over it,
files are dropped in a fixed order — PNG layers first, then previews and
`p0.base.jpg` (without which a preview cannot be composited: a 404 that says
so) — and `regions.json`, `manifest.json`, the `text.<key>.json` layers and
the SVGs are never dropped. The manifest's `dropped` list and
`artifacts.dropped` say what went.

A background sweeper runs at startup and every
`CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S` (600 s). It deletes artifact
directories whose job is gone or expired **and the expired job rows
themselves**. Terminal jobs only. Gauges: `classifier_artifact_bytes`,
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

### `GET /criterion-types`

Every criterion type's options, for THIS container — the schema is generated
from the same pydantic models `/assess` validates against, so it cannot drift.

```bash
curl http://localhost:4001/v1/classifier/criterion-types -H "Authorization: Bearer sk-1234"
```

```json
{
  "shared_fields": {"name": "…", "type": "…", "weight": "…", "depends_on": "…", "score": "…"},
  "types": {
    "llm": {
      "options_schema": {"type": "object", "additionalProperties": false,
                         "properties": {"hint": {"…": "…"}, "boxes": {"…": "…"},
                                        "max_attempts": {"…": "…"}, "ocr": {"…": "…"}}},
      "defaults": {"hint": "auto", "boxes": false, "max_attempts": 3, "ocr": "auto"},
      "caps": {"max_attempts": 3}
    },
    "text": {"defaults": {"pattern": "<name>", "match": "contains", "case_sensitive": false,
                          "fuzzy_threshold": 0.85, "min_count": 1, "ocr": "auto"},
             "caps": {"pattern_max_chars": 500, "min_count": 1000}, "…": "…"},
    "cv": {"defaults": {"fallback": "detector"}, "caps": {}, "…": "…"},
    "detector": {"defaults": {"threshold": 0.25}, "caps": {}, "…": "…"}
  },
  "detector_configured": true
}
```

`text.defaults.pattern` shows `<name>` because it defaults to the criterion's
name. `cv.defaults.fallback` is `detector` only while DETECTOR_URL is set.

---

### `GET /hints`

Returns every `options.hint` value and the rubric the model is given for it
(`config.HINT_RUBRICS`, verbatim).

```bash
curl http://localhost:4001/v1/classifier/hints -H "Authorization: Bearer sk-1234"
```

```json
{
  "hints": {
    "quality":  {"heading": "...", "rubric": "...", "extra": ""},
    "presence": {"heading": "...", "rubric": "...", "extra": "...evidence-first instruction..."},
    "auto":     {"heading": "...", "rubric": "...", "extra": "..."}
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

What this container can accept and do right now: the four kinds (with
`pdf.pages` saying single-page only), the `.doc` rejection, the accepted
inputs, the four `text_match_modes`, the live OCR status, the limits, and the
regions block.

```json
{
  "kinds": [{"kind": "image", "…": "…"}, {"kind": "pdf", "pages": "1 — a PDF with more than one page is refused at submit (400)", "…": "…"}, "…"],
  "unsupported": [{"kind": "doc", "…": "…"}],
  "inputs": {"json": ["base64", "url", "text"], "multipart": ["file (or legacy 'image')", "text"]},
  "text_match_modes": {"contains": "…", "exact": "…", "regex": "…", "fuzzy": "…"},
  "ocr": {"engine": "rapidocr", "available": true, "min_native_chars": 20,
          "modes": ["auto", "always", "never"], "default": "auto",
          "set_on": "each llm / text criterion's options.ocr",
          "text_layer_artifact": "text.<key>.json per distinct setting"},
  "limits": {"max_pages": 1, "pdf_render_dpi": 150, "llm_text_char_budget": 60000,
             "images_per_llm_prompt": 1, "max_concurrent_jobs": 4,
             "max_criteria_per_job": 2, "max_llm_calls": 4, "ocr_workers": 4},
  "regions": {
    "always_stored": true, "layers": ["png", "preview", "svg"],
    "layers_rendered": "on first fetch, then cached",
    "sources": ["cv", "ocr", "pdf-text", "detector", "llm"],
    "llm_boxes": {"set_on": "each llm criterion's options.boxes", "max_attempts": 3,
                  "verify_pass": 7, "min_area": 0.002, "max_area": 0.95,
                  "presence_min_score": 7, "grid": 1000.0},
    "detector": {"configured": true, "url_host": "detector:8000", "…": "…"},
    "inline_max_per_criterion": 50, "artifact_dir": "/data/artifacts",
    "artifact_max_bytes": 50000000, "artifact_ttl_hours": 24, "sweep_interval_seconds": 600.0
  }
}
```

`ocr.available` loads the engine to answer honestly — `false` when
`CLASSIFIER_OCR_ENGINE=none` **and** when the engine failed to load.

---

### `POST /assess`

Submit an assessment job. **Returns** `202 Accepted` —
`{"job_id": "abc123", "phase": "pending"}`; poll `GET /jobs/{job_id}`.
The request is in [§ Request](#request) and the result in
[§ Result shape](#result-shape).

**Multipart:**

```bash
curl http://localhost:4001/v1/classifier/assess \
  -H "Authorization: Bearer sk-1234" \
  -F "file=@notice.pdf" \
  -F 'criteria=[
    {"name":"Notice to Owner", "type":"text","weight":4.0,"options":{"match":"fuzzy"}},
    {"name":"case number",     "type":"text","weight":2.0,"options":{"match":"regex","pattern":"CASE-\\d{5}"}},
    {"name":"signature is present","type":"llm","weight":3.0,"depends_on":"Notice to Owner",
     "options":{"hint":"presence","boxes":true}},
    {"name":"sharpness","type":"cv"}
  ]'
# → {"job_id": "abc123", "phase": "pending"}
```

**JSON, the same request:**

```bash
curl http://localhost:4001/v1/classifier/assess \
  -H "Authorization: Bearer sk-1234" -H "Content-Type: application/json" \
  -d '{"document": {"type": "url", "data": "https://example.com/notice.pdf"},
       "criteria": [{"name": "Notice to Owner", "type": "text", "options": {"match": "fuzzy"}}]}'
```

**Inline text:** `{"document": {"type": "text", "data": "…"}}`, or `-F "text=…"`
instead of a file part.

**Errors** — all at submit, nothing queued: **400** for every rule in
[§ Validation](#criterioninput), an empty file, invalid base64, a blocked URL,
a body that is not a JSON object, both or neither of `file` / `text`, a removed
form field, an unsupported kind, and a PDF with more than one page; **415** for
a Content-Type that is neither JSON nor multipart; **502** when a document URL
cannot be fetched; **500** if the payload could not be persisted. A page image
under `CLASSIFIER_MIN_IMAGE_WIDTH` × `CLASSIFIER_MIN_IMAGE_HEIGHT` (32 × 32 px)
fails the JOB with "Image too small".

---

### `GET /jobs/{job_id}/artifacts`

The manifest: `files[]` (what is on disk now — name, bytes, content type,
url), the criterion map (`slug`, `count`, `sources`, `text_layer`),
`page_geometry`, `options` (which layers can be rendered), `dropped`, `notes`,
`total_bytes`, `expires_at`, `zip_url`.

```bash
curl http://localhost:8005/jobs/abc123/artifacts | jq '{files: [.files[].name], criteria}'
```

**404** when the job is unknown or has not written its directory yet (still
queued, running, or failed before it got that far). **410** when its result
says it *did* have artifacts but the directory is gone — swept past the TTL,
or explicitly deleted.

---

### `GET /jobs/{job_id}/artifacts/{name}`

Stream one file with its content type (`application/json`, `image/svg+xml`,
`image/png`, `image/jpeg`, `text/plain; charset=utf-8`). `name` is validated
against a strict pattern — no path traversal, no listing the volume.

* A file on disk is streamed as-is.
* A **layer not rendered yet** — `p0.svg`, `p0.layer.png`, `p0.preview.jpg`,
  or `p0.<slug>.<suffix>` for one criterion — is rendered from `regions.json`
  now and cached into the directory.
* `text.<key>.txt` returns `text.<key>.json`'s `text` as plain text.
* With any filter, `regions.json` or a combined `p0.*` layer is re-rendered for
  that subset by the same renderer:

| Param | Effect |
|---|---|
| `criterion=<slug>` (repeatable) | Only that criterion's / those criteria's regions |
| `source=cv,ocr,pdf-text,llm,detector` | Only regions from those sources |
| `attempt=<n>` | LLM criteria only: the box from [enforcement-loop](#the-llm-enforcement-loop) attempt *n*, accepted or not |
| `accepted=true` \| `false` | LLM criteria only. `true` — the default whenever no `attempt` is given — keeps the accepted attempt; `false` keeps the rejected ones |

```bash
curl http://localhost:8005/jobs/abc123/artifacts/p0.svg -o p0.svg                       # rendered now
curl "http://localhost:8005/jobs/abc123/artifacts/p0.svg?criterion=has-water-4b21" -o w.svg
curl "http://localhost:8005/jobs/abc123/artifacts/regions.json?criterion=has-water-4b21" | jq .
curl http://localhost:8005/jobs/abc123/artifacts/text.auto.json | jq '{source, chars}'
curl http://localhost:8005/jobs/abc123/artifacts/text.auto.txt
```

Only the plain single-criterion view is cached (as `p0.<slug>.<suffix>`) — a
source or attempt filter is a different subset, and caching it under that
name would serve it back to the next caller who asked for the plain one.

Errors: **400** for an unsafe name, an unknown slug, or a filter on a file
that is not `regions.json` / a combined layer; **404** for an unknown or
unfinished job, a missing file that is not a layer, a layer on a document with
no page image, or a preview whose base image the byte cap dropped; **410** when
the directory was swept or deleted.

---

### `GET /jobs/{job_id}/artifacts.zip`

Every file currently in the directory as one zip, built on the fly and
streamed. Layers that were never fetched are not in it — fetch them first.

```bash
curl http://localhost:8005/jobs/abc123/artifacts.zip -o layers.zip && unzip -l layers.zip
```

---

### `DELETE /jobs/{job_id}/artifacts`

Remove the artifact directory but keep the job row and its result — the inline
regions survive. Returns **204**; **404** when the job is unknown or has no
directory. Afterwards the other artifact endpoints answer **410**.

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
  "result": {"schema_version": 2, "…": "…"},
  "error": null
}
```

`phase` values: `staging` → `pending` → `processing` → `completed` | `failed`
(see § Async job pattern). A model outage does **not** fail the job — it fails
the criterion and the job completes with `complete: false`. `failed` means the
job itself could not run: a thumbnail-sized image, an undecodable file, or a
payload from an older container (`metadata.type` `compare` / `locate`, or a
pre-schema-2 assess payload), each with a message saying so.

---

### `GET /jobs`

List recent jobs (newest first). `?phase=` filters, `?limit=` caps the page.

```bash
curl "http://localhost:4001/v1/classifier/jobs?limit=10" -H "Authorization: Bearer sk-1234"
```

---

### `DELETE /jobs/{job_id}`

Delete a job record **and its artifact directory** (text layers included).
Returns `204 No Content`; **404** when the job is unknown. The directory
removal is wired in as `common.jobs.router.build_router`'s `on_delete` hook, so
a deleted row can never leave files that nothing points at.

---

## Verdict thresholds

| Score | Verdict |
|---|---|
| 7–10 | PASS |
| 4–6 | MARGINAL |
| 1–3 | FAIL |

A criterion that was not evaluated has `status: "skipped"` and a `null`
verdict; one that failed has `status: "error"`. Neither counts toward the
weighted score (an error makes the assessment incomplete).

---

## Hand-testing fixtures

[`unit-tests/classifier/documents/`](../../unit-tests/classifier/documents/)
holds one generated fixture per document kind and failure mode — a native PDF,
a scanned PDF, a `.txt`, a `.docx`, a well and a badly photographed letter, a
legacy `.doc`, and a two-page PDF that shows the single-page refusal — each
with the criteria to send and the result to expect, plus curl examples. The
Postman collection's **Documents** folder mirrors it.

[`unit-tests/classifier/regions/`](../../unit-tests/classifier/regions/) does
the same for regions: a synthetic scene with sky, vegetation and a pool in
known places, a two-column text page, and the document fixtures referenced (not
copied) for the `ocr` and `pdf-text` overlays — with the expected geometry per
file, curl examples for all four artifact endpoints, and a list of things that
should never happen. Mirrored by the collection's **Regions** and **Artifacts**
folders; **Documents + regions** re-sends the document fixtures with criteria
chosen for their geometry.

### Running the fixtures as a suite

[`unit-tests/classifier/regions_report.py`](../../unit-tests/classifier/regions_report.py)
executes the collection's folders end to end — submit, poll, download the
geometry and the text layers, fetch the layers (which renders them) — then
**re-draws every region from `regions.json` onto the original fixture** with
`common.vision.annotate` and puts that picture next to the service's own
`p0.preview.jpg`. It writes a self-contained `index.html` (with a link to the
text each text criterion searched), a machine-readable `summary.json`, and
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

The unit tests (`unit-tests/classifier/test_*.py`) script the model at
`llm.client._send` / `call_vllm` / `call_vllm_json` and never touch the
network.
