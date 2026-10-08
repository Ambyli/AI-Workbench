# Region accuracy report

`regions_report.py` runs the classifier's Postman collection as a test suite
and renders what came back as a page you can check by eye.

The classifier already tells you *where* it found something. What it cannot
tell you is whether that place is **right** — and neither can a reviewer
reading `regions.json`. So the script submits every collection request, polls
the job, downloads what the job stored (regions.json, the manifest, every
`text.p<n>.<key>.json`) and asks for the layers — which the service renders on
first fetch — and then **re-draws the geometry independently** onto the
original fixture before putting the two pictures side by side:

```
submit → poll → GET /jobs/{id}/artifacts → download regions.json + text layers
                                         → fetch p<n>.svg / p<n>.preview.jpg per item (rendered now)
                                         → re-draw on the original fixture
                                         → compare with regions_expected.json
                                         → index.html + summary.json + exit code
```

The independence is the whole point. The service's own `p<n>.preview.jpg` was
drawn by the same code that produced the regions, so it *cannot* disagree with
them. The annotated JPEG beside it is drawn from `regions.json` by
[`common.vision.annotate`](../../shared/common/src/common/vision/annotate.py)
onto the fixture as it exists on disk. If the two differ, one of them is
wrong, and you can see that without reading a coordinate.

**Several documents, several pages.** A request may carry more than one
document, and every page of every document is one **item** — `n` in `p<n>`,
and a region's `page`. The script sends every `file` part the Postman item
lists (and every `text` field), reads the result's `items` / `documents` map
to find which fixture and which page each item is, and draws **each item's
regions on its own page**: `p1.annotated.jpg` of a two-page PDF is page 2 of
the PDF with page 2's regions, beside the service's `p1.preview.jpg`.

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
| `muse-glimmer` (`VISION_LLM_API`) | any item with an `llm` criterion (including every `options.boxes` item). Without it those **criteria** come back `status: "error"` and the job completes with `complete: false` — the other criteria still score |
| `detector` (`DETECTOR_URL`) | the items with a `detector` criterion. Without DETECTOR_URL those are **refused at submit** (400); with it set but the service down, the detector criteria come back `status: "error"` |

Check before a run:
`curl $CLASSIFIER_BASE_URL/v1/classifier/document-kinds -H "Authorization: Bearer $KEY"`
→ `regions.detector.configured` and `ocr.available` are the live answers.

### Locally, with no model at all

`--local` mounts `ai/classifier/main.py` in-process with FastAPI's
`TestClient` on a throwaway `PAYLOAD_DIR` / `CLASSIFIER_ARTIFACT_DIR` /
`CLASSIFIER_REFERENCE_DIR` under the output directory, with
`CLASSIFIER_OCR_ENGINE=rapidocr`, and a throwaway **Postgres database**: the
classifier keeps its job queue, references and model-usage rows in Postgres
and nowhere else, so `--local` needs `TEST_POSTGRES_DSN` (any
server you can `CREATE DATABASE` on — e.g.
`postgresql://postgres@localhost:5432/postgres`). It creates
`classifier_local_<pid>_<hex>` on it for the run and drops it afterwards
(`pg_testdb.py`, the same helper the unit-test session uses); without the
variable it stops before starting with a message saying so. No container, no
GPU, no model. It
verifies everything that is not the vision model: collection parsing,
submission, the job queue, OCR, the OpenCV detectors, text matching, artifact
writing, the annotation pass, and the report itself.

Two consequences, both stated in the report rather than hidden:

* **`llm` and `detector` criteria are dropped** from a request before it is
  sent (a `detector` criterion would be a 400 with DETECTOR_URL empty), so the
  `cv` / `text` / OCR geometry — the part that *can* be checked without a GPU
  — still runs. The report notes exactly which criteria were dropped, and
  their expectations are marked **skipped**, not met.
* **A criterion that still reaches the model errors, alone.** If every
  criterion is `llm` (the enforcement-loop item), the request is sent
  unchanged; and a `cv` name with no OpenCV detector falls back to the llm
  because no detector is configured. Those criteria come back
  `status: "error"` naming the vision model, the job completes with
  `complete: false`, and their checks — and the overall verdict — are
  recorded as **skipped**. Only in `--local`, and only when the error names
  the vision model; the same outcome against the box counts as a failure.

---

## Usage

```bash
# Everything, against the box
uv run --package classifier python unit-tests/classifier/regions_report.py

# Everything, in-process, no model needed (needs a Postgres to create a throwaway DB on)
TEST_POSTGRES_DSN=postgresql://postgres@localhost:5432/postgres \
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
| `--only` | _(all)_ | Case-insensitive substring of the item name. Does not filter the `--pipeline` call |
| `--pipeline` | _(none)_ | Run the amount-due locator — see [§ Pipelines](#pipelines): one `/assess` call per bill asking the vision model where the amount due is, drawn on the page. `utility-bill` is the one that exists |
| `--document` | the committed bills | Run the pipeline on this file instead (repeatable). An ad-hoc file has no expectations: its checks are recorded as **skipped**, not failed, and where the model put the box is still drawn |
| `--out` | `unit-tests/classifier/reports/<UTC timestamp>/` | Gitignored |
| `--timeout` | `600` | Seconds to wait for one job to reach a terminal phase |
| `--expect` | `unit-tests/classifier/regions_expected.json` | |
| `--parallel` | `1` | Keep it at or below the box's `CLASSIFIER_MAX_CONCURRENT` (4). A deeper queue does not go faster — it just makes every elapsed number meaningless |
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
│   ├── text.p0.never.json              the exact text the text criteria searched (item 0)
│   ├── p0.svg                          the service's overlay (rendered on fetch)
│   ├── p0.preview.jpg                  the service's preview (rendered on fetch)
│   └── p0.annotated.jpg                ← drawn by this script
├── 09-invoice-two-page-pdf-two-items-.../
│   ├── text.p0.auto.json  text.p1.auto.json  text.d0.auto.json   per page, and the joined document
│   ├── p0.preview.jpg  p1.preview.jpg
│   └── p0.annotated.jpg  p1.annotated.jpg                        ← one per item
└── _service/                           --local only: the throwaway payloads + artifacts (the DB is dropped)
```

### Reading `index.html`

**The summary table** — one row per item: endpoint, phase, overall verdict and
score, elapsed, the region count **by source**, LLM attempts/accepted,
detector calls, how many expectations held, and any notes. The source counts
are the fastest signal in the whole report: `pdf-text:6` on a native PDF and
`ocr:6` on the same invoice scanned is the two code paths agreeing; a zero
where you expected a number means the geometry never got collected.

**Per item**, in order:

1. **Request** — JSON or multipart, the fixture(s) — every document the
   request carries — and the criteria as a table (type / score / options /
   weight / depends_on).
2. **Result** — every criterion with method, score, verdict (or its status
   when it errored or was skipped), confidence, reason (or the error), region
   count, which pages, the `localization` summary (attempts / accepted /
   calls), **links to the text it searched** (one `text.p<n>.<key>.json` per
   item — or `text.d<i>.<key>.json` for a scope-document search — with its
   source and length, downloaded next to the report), and ✓ or ✗ against its
   expectation. These are the AGGREGATED answers.
   * **Per item** — for a request with more than one item: one row per item
     (its document, page, and own weighted score), one column per criterion
     with that item's unit verdict, score and region count, and each
     criterion's `aggregate_used` underneath. A scope-document criterion's
     cell is its one unit for the whole document. A criterion whose expectation is `null` says *not
   asserted* rather than a tick — a green tick you did not earn is worse than
   no tick.
3. **Geometry, drawn twice** — per item (headed `item n — <file> p<page>`),
   the annotated page on the left, the service's own preview on the right. Each region carries a caption:

   ```
   Notice to Owner · 10 PASS · ocr
   has solar panels · 9 PASS · llm · attempt 2 verify=9
   has solar panels · 9 PASS · llm · attempt 1 ✗        (drawn dashed)
   ```

   Colour is one hue per criterion, hashed from the name by
   `common.vision.palette` — the same hue the service uses, so the two
   pictures are comparable at a glance. Stroke says the source: solid
   `cv`/`detector`/`pdf-text`, dashed `ocr`, dotted `llm`. The **accepted**
   LLM attempt is drawn from its `bbox_px` — the box the service stored and
   cropped for the verify call, in original (upright) page pixels after the
   refine pass mapped it back — so the picture shows exactly what was
   verified. A **rejected** attempt is drawn dashed and unfilled, from the
   `bbox_grid` the model literally answered rather than the clamped box, so
   an overshoot looks like an overshoot.
4. **Links** to the `p<n>.svg` layers, `regions.json`, the `text.*.json` layers, `job.json`.
5. **What the collection says to look for** — the Postman item's own
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
| `verdict` | The job's overall verdict. Omit or `null` to assert nothing |
| `http_status` | The item is expected to be **rejected at submit time**; nothing else is asserted |
| `criteria.<name>.status` | `ok` \| `error` \| `skipped`. A skipped criterion has a null verdict, so this is how a skip is asserted |
| `criteria.<name>.verdict` | The criterion's verdict. **`null` asserts nothing** |
| `criteria.<name>.min_regions` | At least this many regions on that criterion (over every item) |
| `criteria.<name>.item_verdicts` | The per-unit answers, in order — one per item, or one per document for a `scope: "document"` text criterion: a verdict, or `skipped` / `error` for a unit that did not answer; `null` in the list asserts nothing |
| `criteria.<name>.region_pages` | The items (sorted) the criterion's regions landed on |

Keys starting with `_` are ignored — `_about` in the file carries the same
rules inline, since JSON has no comments.

**Why every `llm` row says `"verdict": null`.** Its score comes from a model.
A suite that pins a model's judgement to a fixed answer is not measuring the
model, it is measuring whether the model changed — and it will go red on an
upgrade that made things better. `min_regions` is still asserted for those
rows, but only as `0`: "you may produce boxes, and if you do they will be
drawn". The deterministic paths — `cv`, `text`, `ocr`, `pdf-text` —
carry real numbers, taken from
[`documents/README.md`](documents/README.md) and
[`regions/README.md`](regions/README.md).

An item with **no entry at all** fails its coverage check, so a new Postman
item cannot quietly go unasserted.

---

## Pipelines

The collection holds fixed requests. `--pipeline utility-bill` is the one case
it cannot hold: a request built for whatever bill you pass on the command line,
with a result the report reads more closely than a generic case. It asks the
vision model **one question — where is the amount due?** — and shows you where
it answered.

```bash
# Both committed bills, against the box
uv run --package classifier python unit-tests/classifier/regions_report.py --pipeline utility-bill

# Your own bill. No expectations for it, so its checks are skipped; the box is still drawn
uv run --package classifier python unit-tests/classifier/regions_report.py \
    --pipeline utility-bill --document ~/Downloads/some_bill.jpg --keep-jobs
```

### `utility-bill` — where is the amount due?

**One call per bill:** `POST /assess` with a single criterion:

```json
{"name": "the amount due line: the words 'Amount Due' next to the dollar figure owed",
 "type": "llm", "score": false, "options": {"hint": "presence", "boxes": true}}
```

No OCR criteria, no gate, no crop-and-resubmit. `score: false` locates without
judging: the classifier still makes the one scoring call, because the
enforcement loop gates on its presence score, then clears the judgement from
the result. It then runs the loop: ask on the page with a labelled
coordinate grid drawn on it, refine on a zoomed crop of the original, verify
the crop alone, retry with feedback. That is **4 model calls** when attempt 1
is accepted — the scoring call, then ask, refine, verify (the loop's
`localization.calls` is 3; the report shows "1 scoring + 3 loop model
call(s)") — and at most 1 + 3 × `CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS` = 10.

The report gets a section per bill above the cases:

- **Where it landed:** the accepted box on the 0-1000 grid, which attempt, whether it was refined, the verify score, and what the verifier said it saw.
- **The page, with every attempt drawn on it** by this script. The accepted box is drawn from the `bbox_px` the service stored and verified, dotted and filled, the stroke every `llm` region gets; rejected attempts are drawn from the model's own `bbox_grid`, dashed and unfilled.
- **A zoomed view of the accepted box** on the original pixels, `amount_due_zoom.jpg`, because on a phone photo of a whole bill the full-page picture is too small to tell a line from the one above it. It is cut from the accepted box's own page, read upright (EXIF orientation applied, as the service does), with `ZOOM_PAD` (4 %) of the page on every side; `summary.json` records the window as `zoom_window_px`.
- **Every attempt as a table:** the model's first answer on the gridded page (`coarse_bbox_grid`, or `bbox_grid` when no refine pass ran), the final box that was verified (`bbox_grid` — the refine answer mapped back onto the page grid, or the first answer again when the refine answer was unusable), whether it was refined (or why the refine answer was not used), the verify score, and the reason — why it was rejected, or what the verifier saw. A multi-page `--document` (a PDF bill) runs the loop once per page; the table then gets an `item` column, and the zoom is cut from the page the accepted box is on.

Two fixtures run by default, chosen because they lay the amount due out differently:

| Fixture | Bill | Amount-due lines |
|---|---|---|
| [`documents/utility_bill.jpeg`](documents/utility_bill.jpeg) | Ohio Edison, 5712×4284 with EXIF orientation 6 (read upright at 4284×5712) | `Amount Due: $80.49` on one line in the header, and again on the payment stub |
| [`documents/utility_bill_2.jpeg`](documents/utility_bill_2.jpeg) | AEP Ohio, 4032×3024, two pages side by side | `Amount due on or before` … `$193.33` in the header and on the stub; `Total Amount Due At Last Billing $201.60` in the charges table is a different number |

Expectations, in `regions_expected.json`, one pair per fixture:

```json
"utility bill — where is the amount due? — utility_bill_2.jpeg": {
  "criteria": { "the amount due line: …": { "verdict": null, "min_regions": 1 } }
},
"pipeline: utility bill — utility_bill_2.jpeg": {
  "box_accepted": true,
  "amount_due_boxes_grid": [[360, 142, 495, 172], [373, 641, 511, 671]]
}
```

`amount_due_boxes_grid` lists every place the bill prints its amount due,
hand-measured on the 0-1000 grid of the upright page. The accepted box must
reach IoU ≥ 0.25 with at least one of them. This is a localisation check, not
a judgement of the model's opinion: it asks whether the box sits on the words,
which is what this section exists to show.

Under `--local` there is no vision model. The one criterion is `llm`, so the
request is sent unchanged, the criterion comes back `status: "error"` naming
the model, and the checks are recorded as **skipped**, like every other
all-`llm` case in `--local`.

**Adding a bill** is: drop the photo in `documents/`, add its path to
`UTILITY_BILL_FIXTURES` in `regions_report.py`, measure its amount-due lines
on the 0-1000 grid of the upright page, and write the two entries.

### The end-to-end proof, with no model

[`test_regions_report_pipeline.py`](test_regions_report_pipeline.py) runs
this script's own `main(["--local", "--pipeline", "utility-bill", "--out", …])`
on `utility_bill.jpeg` — the real app, queue, enforcement loop, artifact
download, drawing, zoom, checks and HTML — with only the vision model
replaced, at the bare transport `llm.client._send`. The scripted model reads
each prompt and answers from the hand-measured header line in
`regions_expected.json`: presence 9 to the scoring call, a loose box to the
ask on the gridded page, the line on the **crop's own grid** to the refine
(computed from the window the refine pass must cut, and checked against the
crop's aspect ratio), and 9 to the verify. It asserts the four calls, the
accepted box in upright pixels, the verify crop's size, the checks (3/3, none
skipped, exit 0), the three files, and that `amount_due_zoom.jpg` is the
upright page cut around the box with the box drawn on it. A second run has
the verifier reject attempt 1 and accept attempt 2, and checks both attempts
reach `summary.json`, the drawing and the attempts table.

```bash
UV_LINK_MODE=copy uv run --no-sync --with pytest --package classifier \
    python -m pytest unit-tests/classifier/test_regions_report_pipeline.py -q -p no:cacheprovider
```

It is part of the ordinary `unit-tests/classifier` suite and takes ~15 s.

---

## Adding a case

Two steps, and neither is in this script:

1. Add the item to the Postman collection, in one of the folders being run.
   Follow the conventions in the repo `CLAUDE.md`: `src` paths relative to the
   repo root, `:name` path params, and a description that says what to look at
   and what to expect.
2. Add an entry to `regions_expected.json` under the item's exact name.

The collection **is** the suite, so that is all. Two things to know:

* A **JSON body** (`POST /assess` with `document.type: "base64"`) references a
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

* [`plan_set_report.py`](plan_set_report.py) — the same machinery (it imports
  this script's transports, polling, artifact download and drawing) asking one
  whole-document question of files passed on the command line: *is this a
  solar plan set?*, with the criteria in
  [`solar_plan_set_criteria.json`](solar_plan_set_criteria.json). Six
  `scope: "document"` text criteria plus one `llm` cover-sheet check
  (`aggregate: any`); `--expect PASS|FAIL`, `--local`, `--keep-jobs` as here.
  Every page is an item, so a plan set longer than `CLASSIFIER_MAX_ITEMS` is a
  400 — the report names the knob.
* [`utility_bill_reference_report.py`](utility_bill_reference_report.py) — the
  same machinery asking *is this a utility bill?* with a saved example in
  view. It `POST /references` the AEP Ohio bill (`documents/utility_bill_2.jpeg`)
  with the answer key from
  [`utility_bill_reference.json`](utility_bill_reference.json) — PASS 10, the
  whole page as the region, a supplied description, so creating it costs no
  model call — then assesses each candidate (default `documents/utility_bill.jpeg`,
  expected PASS) **twice**: without references, and with `references: [id]`.
  The criteria are one `llm` "this document is a utility bill" criterion
  (weight 3, the one the reference guides — one two-image call: the
  reference's composite, then the candidate) and four OCR `text` criteria
  (usage units, amount due, account number, billing period). The report puts
  the two side by side per criterion with the model's reason each time, the
  `detail.reference` examples and calls, and the composite the model was
  shown. The reference is a copy of a customer document, so it is
  `DELETE`d at the end unless `--keep-reference`; `--reference-id` reuses a
  kept one. `--local` creates the reference for real (the llm criterion
  errors with no model); `test_utility_bill_reference_report.py` drives the
  guided path with a scripted model. Needs a classifier with the references
  API and `VISION_LLM_MAX_IMAGES_PER_PROMPT` ≥ 2.

  That default spec shows the plumbing, not an effect: the model already
  knows a utility bill, so it is PASS 10 with or without the example.
  `--spec unit-tests/classifier/utility_bill_k7_reference.json` is the
  example where **the reference decides the answer**: the criterion is
  *"This document is an intake class K7 document"*, an internal label only
  the reference defines. On the box (2026-10-05) the Ohio Edison bill went
  FAIL 1 → PASS 10 ("a residential electric bill matching the intake class
  K7 example") and the roofing invoice stayed FAIL 1 both ways. A spec lists
  its own `candidates`, each with `expect` and optionally `expect_changed`
  (a check that the guided verdict differs from the baseline).
* [`ai/classifier/API.md`](../../ai/classifier/API.md) — the request/response
  reference, § Regions and layers and § The text a criterion searched in
  particular.
* [`documents/README.md`](documents/README.md) — the document fixtures, their
  criteria and expected results, and § Marks on the letter for the logo /
  stamp / signature boxes.
* [`regions/README.md`](regions/README.md) — the region fixtures, expected
  geometry per file, and the list of things that should never happen.
* [`shared/common/src/common/vision/annotate.py`](../../shared/common/src/common/vision/annotate.py)
  — the drawing, which is shared rather than local to this script so the
  classifier can adopt it later.
