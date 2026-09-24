# Region accuracy report

`regions_report.py` runs the classifier's Postman collection as a test suite
and renders what came back as a page you can check by eye.

The classifier already tells you *where* it found something. What it cannot
tell you is whether that place is **right** — and neither can a reviewer
reading `regions.json`. So the script submits every collection request, polls
the job, downloads the layers, and then **re-draws the geometry independently**
onto the original fixture before putting the two pictures side by side:

```
submit → poll → GET /jobs/{id}/artifacts → download layers
                                         → re-draw on the original fixture
                                         → compare with regions_expected.json
                                         → index.html + summary.json + exit code
```

The independence is the whole point. The service's own `p0.preview.jpg` was
drawn by the same code that produced the regions, so it *cannot* disagree with
them. The annotated JPEG beside it is drawn from `regions.json` by
[`common.vision.annotate`](../../shared/common/src/common/vision/annotate.py)
onto the fixture as it exists on disk. If the two differ, one of them is
wrong, and you can see that without reading a coordinate.

---

## Setup

### Against the box (the normal case)

Two keys in the repo-root `.env`, both already present:

| Key | What it should be |
|---|---|
| `CLASSIFIER_BASE_URL` | The LiteLLM base URL, **without** the `/v1/classifier` suffix — e.g. `http://192.168.5.233:4001`. The collection's items carry the pass-through prefix themselves |
| `CLASSIFIER_API_KEY` | A LiteLLM virtual key scoped to the `/v1/classifier` pass-through. Falls back to `DEFAULT_LITELLM_MASTER_KEY` when unset |

Both are read through [`common.env`](../../shared/common/src/common/env.py),
which walks up from the working directory to find the `.env`, so the script
runs from anywhere in the repo. `--base-url` / `--api-key` override them.

What has to be **running** depends on which folders you run:

| You need | For |
|---|---|
| `classifier` | everything |
| `muse-glimmer` (`VISION_LLM_API`) | any item with an `llm` criterion, and every `regions.llm_boxes` item. Without it those jobs **fail** — `call_vllm` raises rather than degrading |
| `detector` (`DETECTOR_URL`) | the `regions.detector` items. Without it the job still **succeeds** and `artifacts.notes` says the service is not configured |

Check before a run:
`curl $CLASSIFIER_BASE_URL/v1/classifier/document-kinds -H "Authorization: Bearer $KEY"`
→ `regions.detector.configured` and `ocr.available` are the live answers.

### Locally, with no model at all

`--local` mounts `ai/classifier/main.py` in-process with FastAPI's
`TestClient` on a throwaway `DB_PATH` / `PAYLOAD_DIR` /
`CLASSIFIER_ARTIFACT_DIR` under the output directory, with
`CLASSIFIER_OCR_ENGINE=rapidocr`. No container, no GPU, no network. It
verifies everything that is not the vision model: collection parsing,
submission, the job queue, OCR, the OpenCV detectors, text matching, artifact
writing, the annotation pass, and the report itself.

Two consequences, both stated in the report rather than hidden:

* **`llm` criteria are dropped** from a request before it is sent, so the
  `cv` / `text` / OCR geometry — the part that *can* be checked without a GPU
  — still runs. The report notes exactly which criteria were dropped, and
  their expectations are marked **skipped**, not met.
* **A request that cannot be trimmed fails.** If every criterion is `llm`
  (the enforcement-loop item), or a `cv` / bare-string feature falls through
  to the model because no detector service is configured (the
  `regions.detector` item, and `/locate` with `llm_boxes` on), the job fails
  with `502: vLLM call failed`. That is recorded as **skipped** too — but only
  in `--local`, and only when the error names the model server. The same
  failure against the box counts as a failure.

---

## Usage

```bash
# Everything, against the box
uv run --package classifier python unit-tests/classifier/regions_report.py

# Everything, in-process, no model needed
uv run --package classifier python unit-tests/classifier/regions_report.py --local

# One fixture
uv run --package classifier python unit-tests/classifier/regions_report.py --only photo_of_letter

# One folder
uv run --package classifier python unit-tests/classifier/regions_report.py \
    --folders "Documents + regions"

# Keep the jobs on the box so you can poke at them afterwards
uv run --package classifier python unit-tests/classifier/regions_report.py \
    --keep-jobs --out /tmp/regions-run
```

| Flag | Default | Notes |
|---|---|---|
| `--base-url` | `CLASSIFIER_BASE_URL`, else `http://localhost:4001` | LiteLLM base URL |
| `--api-key` | `CLASSIFIER_API_KEY`, else `DEFAULT_LITELLM_MASTER_KEY` | Bearer token |
| `--collection` | `ai/classifier/classifier.postman_collection.json` | Any collection with the same shape works |
| `--folders` | `Documents,Regions,Documents + regions` | Comma-separated. The **Artifacts** folder is not runnable — its items are parametrised on a `:jobId` that only exists after a submission, and every case here exercises those endpoints itself. When `--pipeline` is given and this is not, no collection folder runs |
| `--only` | _(all)_ | Case-insensitive substring of the item name. Does not filter pipeline stages |
| `--pipeline` | _(none)_ | Run a chained pipeline — see [§ Pipelines](#pipelines). Repeatable; `utility-bill` is the one that exists |
| `--document` | `documents/utility_bill.jpeg` | The document a pipeline runs on. With an ad-hoc file the `amount_due` expectation is **skipped**, not failed — there is nothing to be right against |
| `--out` | `unit-tests/classifier/reports/<UTC timestamp>/` | Gitignored |
| `--timeout` | `600` | Seconds to wait for one job to reach a terminal phase |
| `--expect` | `unit-tests/classifier/regions_expected.json` | |
| `--parallel` | `1` | Keep it at or below the box's `CLASSIFIER_MAX_CONCURRENT` (2). A deeper queue does not go faster — it just makes every elapsed number meaningless |
| `--local` | off | In-process, see above |
| `--keep-jobs` | off | Skip the `DELETE /jobs/{id}` cleanup at the end |

Only `POST` items are run. `GET`/`DELETE` items are skipped by design.

**Exit code** is `0` when every expectation was met (skipped ones included)
and `1` otherwise, so the script drops straight into a CI step or a
`&& echo ok`.

---

## Output

```
reports/2026-09-23T09-48-12Z/
├── index.html                          the report — open this
├── summary.json                        the same thing, machine-readable
├── 01-invoice-native-pdf-native-text.../
│   └── job.json
├── 03-invoice-native-pdf-pdf-text-.../
│   ├── job.json  manifest.json  regions.json
│   ├── p0.svg  p1.svg                  the service's overlays
│   ├── p0.preview.jpg  p1.preview.jpg  the service's previews
│   └── p0.annotated.jpg  p1.annotated.jpg   ← drawn by this script
└── _service/                           --local only: the throwaway DB + artifacts
```

### Reading `index.html`

**The summary table** — one row per item: endpoint, phase, overall verdict and
score, elapsed, the region count **by source**, LLM attempts/accepted,
detector calls, how many expectations held, and any notes. The source counts
are the fastest signal in the whole report: `pdf-text:6` on a native PDF and
`ocr:6` on the same invoice scanned is the two code paths agreeing; a zero
where you expected a number means the geometry never got collected.

**Per item**, in order:

1. **Request** — the `regions` option, the `ocr` mode, the fixture, and the
   criteria as a table (type / hint / match / pattern / weight / depends_on).
2. **Result** — every criterion with method, score, verdict, confidence,
   reason, region count, which pages, the `localization` summary
   (attempts / accepted / calls), and ✓ or ✗ against its expectation. A
   criterion whose expectation is `null` says *not asserted* rather than a
   tick — a green tick you did not earn is worse than no tick.
3. **Examples and change detection** (compare jobs only) — per-example
   similarity, combined score, `aligned`, `inliers`, the added/removed/changed
   counts, and links to the `diff-e{i}-p{n}.*` and `e{i}.p{n}.*` layers.
4. **Geometry, drawn twice** — the annotated page on the left, the service's
   own preview on the right. Each region carries a caption:

   ```
   Notice to Owner · 10 PASS · ocr
   has solar panels · 9 PASS · llm · attempt 2 verify=9
   has solar panels · 9 PASS · llm · attempt 1 ✗        (drawn dashed)
   added · diff · e0
   ```

   Colour is one hue per criterion, hashed from the name by
   `common.vision.palette` — the same hue the service uses, so the two
   pictures are comparable at a glance. Stroke says the source: solid
   `cv`/`detector`/`pdf-text`, dashed `ocr`, dotted `llm`, double `diff`. A
   **rejected** LLM attempt is drawn dashed and unfilled, from the
   `bbox_grid` the model literally answered rather than the clamped box, so
   an overshoot looks like an overshoot.
5. **Links** to `p{n}.svg`, `regions.json`, `job.json`.
6. **What the collection says to look for** — the Postman item's own
   description, verbatim, so the expected box is next to the drawn one.

### What to actually look at

* **Do the boxes sit on the thing?** That is the measurement. Everything else
  is bookkeeping.
* **Do the two pictures agree?** They are drawn by different code from the
  same numbers. A difference is a bug in one of them.
* **Did a criterion with no geometry get `artifacts: null`** rather than an
  empty object? The region count column shows `—` for those.
* **Rejected LLM attempts**: how many, and where did they land? A model that
  needs three attempts on an easy fixture will need more than three on a real
  one.

---

## Expectations

[`regions_expected.json`](regions_expected.json), keyed by Postman item name:

```json
{
  "photo_of_letter.png — OCR line polygons on a skewed page": {
    "verdict": "PASS",
    "criteria": {
      "Notice to Owner": { "verdict": "PASS", "min_regions": 1 }
    }
  },
  "unsupported_legacy.doc — expect 400": { "http_status": 400 }
}
```

| Field | Meaning |
|---|---|
| `verdict` | The job's overall verdict (`aggregate.combined_verdict` for a compare). Omit or `null` to assert nothing |
| `http_status` | The item is expected to be **rejected at submit time**; nothing else is asserted |
| `criteria.<name>.verdict` | The criterion's verdict. **`null` asserts nothing** |
| `criteria.<name>.min_regions` | At least this many regions on that criterion |

Keys starting with `_` are ignored — `_about` in the file carries the same
rules inline, since JSON has no comments.

**Why every `llm` row says `"verdict": null`.** Its score comes from a model.
A suite that pins a model's judgement to a fixed answer is not measuring the
model, it is measuring whether the model changed — and it will go red on an
upgrade that made things better. `min_regions` is still asserted for those
rows, but only as `0`: "you may produce boxes, and if you do they will be
drawn". The deterministic paths — `cv`, `text`, `ocr`, `pdf-text`, `diff` —
carry real numbers, taken from
[`documents/README.md`](documents/README.md) and
[`regions/README.md`](regions/README.md).

An item with **no entry at all** fails its coverage check, so a new Postman
item cannot quietly go unasserted.

---

## Pipelines

The collection can express any single request. It cannot express *"crop the
region the last job found and send that"* — a request whose body depends on
the previous answer. A **pipeline** is a short chain of such calls, built at
run time by the script. Every stage is still an ordinary case: it goes through
the same submit → poll → download → annotate path, gets its own section in the
report and its own entry in `regions_expected.json`. Only the glue between
stages lives in `regions_report.py`.

```bash
# The committed bill, against the box (all three stages, LLM included)
uv run --package classifier python unit-tests/classifier/regions_report.py --pipeline utility-bill

# The same chain in-process — no model; the deterministic OCR half decides
uv run --package classifier python unit-tests/classifier/regions_report.py --pipeline utility-bill --local

# Another bill. The amount-due expectation is skipped; the value is still reported
uv run --package classifier python unit-tests/classifier/regions_report.py \
    --pipeline utility-bill --document ~/Downloads/some_bill.jpg --keep-jobs
```

### `utility-bill` — read the amount due off a photographed bill

The fixture is [`documents/utility_bill.jpeg`](documents/utility_bill.jpeg), a
phone photo of an Ohio Edison bill (5712×4284, EXIF orientation 6 — the
service and the script both read it upright at 4284×5712). It prints
**Amount Due $80.49** three times: the header, the account-summary table, and
the payment stub.

| Stage | Call | Request | What decides it |
|---|---|---|---|
| 1 · is this a utility bill? | `POST /assess` | `ocr=always`, four fuzzy/regex `text` criteria (`Amount Due`, `Account Number`, `billing period`, a usage unit such as `kWh`), an `llm` presence criterion for the document as a whole, an `llm` legibility criterion | Overall **PASS** and the llm presence criterion **PASS**. Under `--local` the llm criteria are dropped, so the text criteria decide alone — `gate.basis` in `summary.json` says which it was. Anything else stops the chain: an amount located on a document that is not a bill is *a* number, which is worse than none |
| 2 · where is the amount due? | `POST /locate` | fuzzy `text` features `Amount Due` and `Total Due` → OCR line polygons in original page pixels; an `llm` presence feature with `regions.llm_boxes` → the enforcement loop's box | **Candidates**, best first: a label line that already carries a dollar figure (the header), other label lines, then the model's accepted box. Two features on one line collapse to one candidate. None at all stops the chain |
| 3 · read the figure inside that region | `POST /assess` on a **crop** | The script cuts the top candidate out of the original page — a page-wide strip three line-heights tall around a label line (the figure is on the same row, sometimes at the far right of a table, and a photographed page is rarely level), or a lightly padded box around the model's — and submits it as a new document with a regex `text` criterion for a currency figure, the `Amount Due` label again, and an `llm` criterion asked to state the figure | Every currency figure on every OCR line of the crop, **nearest the label first** (label-heights vertically, crop-widths horizontally; `$`-prefixed before bare decimals, negatives last). The top one is the answer. The model's stated figure is kept beside it as a second opinion and only becomes the answer when OCR read nothing. A crop with no figure moves to the next candidate, up to three — each attempt is its own case |

The report gets a **Pipeline** section above the cases: the answer, what each
stage decided in one line, the candidate table, every figure read with its
distance, the stage-2 page with all candidates drawn on it beside the stage-3
crop with the figures drawn on it, and the chain-level checks. `summary.json`
carries the same under `pipelines[]`.

Expectations, in `regions_expected.json`:

```json
"pipeline: utility bill": { "is_utility": true, "region_found": true, "amount_due": "$80.49" }
```

plus the usual per-stage entries under the three stage names. The chain's
answer is decided by OCR and a regex, so `amount_due` is a real assertion in
both modes — the llm rows stay `null` as everywhere else.

**Adding a pipeline** is code, not collection: a `run_<name>_pipeline()` in
the § Pipelines section that builds `Case`s and feeds them to `run_case()`,
an entry in the `runners` dict in `main()`, a name in `PIPELINES`, and the
expectations entries. Keep the stage names stable — they are the keys.

---

## Adding a case

Two steps, and neither is in this script:

1. Add the item to the Postman collection, in one of the folders being run.
   Follow the conventions in the repo `CLAUDE.md`: `src` paths relative to the
   repo root, `:name` path params, and a description that says what to look at
   and what to expect.
2. Add an entry to `regions_expected.json` under the item's exact name.

The collection **is** the suite, so that is all. Two things to know:

* A **base64 body** (`/assess/compare` takes JSON, not multipart) references a
  `{{something_b64}}` collection variable. Add the variable to the collection
  *and* a `variable name → repo-relative fixture path` line to `B64_FIXTURES`
  in `regions_report.py`. An unlisted `*_b64` variable is a hard error, not a
  guess — a convention that is right four times out of six will silently
  encode the wrong file the first time someone adds the fifth.
* A **new fixture** should be generated, not committed as an opaque binary:
  `documents/make_fixtures.py` and `regions/make_fixtures.py` both take a
  recipe edit and a re-run, and both are seeded so the bytes are stable.

---

## Related

* [`ai/classifier/API.md`](../../ai/classifier/API.md) — the request/response
  reference, § Regions and layers in particular.
* [`documents/README.md`](documents/README.md) — the document fixtures, their
  criteria and expected results, and § Marks on the letter for the logo /
  stamp / signature boxes.
* [`regions/README.md`](regions/README.md) — the region fixtures, expected
  geometry per file, and the list of things that should never happen.
* [`shared/common/src/common/vision/annotate.py`](../../shared/common/src/common/vision/annotate.py)
  — the drawing, which is shared rather than local to this script so the
  classifier can adopt it later.
