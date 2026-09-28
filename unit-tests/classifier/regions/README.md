# Classifier region fixtures

Hand-testing material for the classifier's **regions and layers**: which
criteria localise on which file, what geometry to expect back, and the curl
calls for every artifact endpoint. The two synthetic images here are generated
by [`make_fixtures.py`](make_fixtures.py) — edit the recipe and re-run rather
than hand-editing a binary:

```bash
uv run --package classifier python unit-tests/classifier/regions/make_fixtures.py
```

Regions are no longer opt-in: **every** `/assess` job stores `regions.json`,
its text layers (`text.p<n>.<key>.json`, one per item and setting), each item's base image and a manifest.
The SVG / PNG / preview layers are rendered on **first fetch** and cached
into the job directory. There is no `regions` request option any more, and no
`/locate` — a criterion with `"score": false` is "locate without judging".

The Postman collection mirrors this folder: `ai/classifier/classifier.postman_collection.json`
has a **Regions** subfolder with one ready-to-run request per fixture and an
**Artifacts** subfolder for the four endpoints. Import it, set the `litellm`
and `virtual master key` variables, and click Send. (The **Documents + regions**
subfolder does the same for the document fixtures in
[`../documents/`](../documents/).)

To run the whole lot as a suite and get the regions re-drawn on the original
fixtures — plus an HTML report and a pass/fail exit code — see
[`REGIONS_REPORT.md`](../REGIONS_REPORT.md):

```bash
uv run --package classifier python unit-tests/classifier/regions_report.py
```

Total size: ~530 KB (every PNG is JPEG round-tripped so PNG can compress the
texture noise).

---

## Files

Two are generated here; three are **referenced, not copied** — duplicating a
fixture is how two copies drift apart.

| File | Kind | What it exercises |
|---|---|---|
| [`greenery_and_sky.png`](greenery_and_sky.png) | image, 900×700 | `detect_sky`, `detect_vegetation`, `detect_water` — three mask-derived polygon sets in places you can check by eye |
| [`text_blocks.png`](text_blocks.png) | image, 900×700 | `detect_text`'s merged high-density block boxes (two columns, so the merge has to produce more than one box); with a `text` criterion's `options.ocr: "always"`, OCR line polygons |
| [`../Neighborhood.jpeg`](../Neighborhood.jpeg) | image | A **real** photo — the only honest input for `has faces`, and a realistic sky/vegetation mix |
| [`../documents/photo_of_letter.png`](../documents/photo_of_letter.png) | image | `source="ocr"` regions: a photographed letter whose text hits map back to OCR line polygons |
| [`../documents/invoice_native.pdf`](../documents/invoice_native.pdf) | pdf | `source="pdf-text"` regions: native PDF rectangles via `search_for` (literal modes) and word-span reconstruction (regex/fuzzy), with `attrs.pdf_rect` in points |

The `scene_before.png` / `scene_after.png` change-detection pair was removed
with `/assess/compare`.

### Why there is no synthetic face fixture

A drawn face does not reliably trip OpenCV's Haar cascade — it was trained on
photographs, and a synthetic one either fails outright or passes for reasons
unrelated to the recipe, which makes it a fixture that lies about what the
detector does. `../Neighborhood.jpeg` is a real photo; use it for `has faces`
and accept whatever the cascade actually reports (often nothing, at that
distance — which is the correct answer and still a useful assertion, since the
criterion must then come back with `regions: []` and `artifacts: null`).

---

## Expected regions

Measured against these fixtures with `CLASSIFIER_OCR_ENGINE=rapidocr`. The
`cv` and `text` rows are deterministic and should reproduce exactly; the
`detector` rows depend on a model and are given as ranges; the `llm` rows
depend on the model's grounding and are the one thing here you should expect
to vary run to run — that variance is exactly what the enforcement loop and
`bin/grounding_experiment.py` exist to measure.

### `greenery_and_sky.png`

```json
[
  {"name": "has sky",        "type": "cv"},
  {"name": "has vegetation", "type": "cv"},
  {"name": "has water",      "type": "cv"},
  {"name": "sharpness",      "type": "cv"}
]
```

| Criterion | Score | Regions | Notes |
|---|---|---|---|
| `has sky` | **PASS** 10 | 1 polygon, bbox ≈ `(0, 0, 899, 243)` | The detector only looks at the top 35% of the page, so the polygon is that band |
| `has vegetation` | **PASS** 10 | 1 polygon, bbox ≈ `(0, 359, 899, 699)` | 8 vertices after `approxPolyDP` — the lumpy top edge survives simplification |
| `has water` | **PASS** 10 | **2** polygons: the sky band **and** the pool at ≈ `(90, 518, 430, 651)` | Expected, and worth understanding: `detect_water` accepts any blue region whose Laplacian variance is under 200, and a smooth sky qualifies. The detector's honest answer, not a fixture flaw — the pool is the second, smaller one |
| `sharpness` | **FAIL** 2 | **none** — `"regions": []`, `"artifacts": null` | A whole-page measurement never fabricates a full-page box. The FAIL is honest: this is a synthetic image of smooth colour ramps, so its Laplacian variance is far under the 100 floor |

Artifacts written at job time: `manifest.json`, `regions.json`, and
`p0.base.jpg` (the un-annotated page every preview is composited from).
`p0.svg`, `p0.layer.png` and `p0.preview.jpg` appear in the directory the
first time each is fetched. No `text.*.json` — no criterion here read text.

### `text_blocks.png`

```json
[
  {"name": "has text",        "type": "cv"},
  {"name": "Notice to Owner", "type": "text",
   "options": {"match": "fuzzy", "fuzzy_threshold": 0.8, "ocr": "always"}},
  {"name": "total amount",    "type": "text",
   "options": {"match": "regex", "pattern": "\\$[\\d,]+\\.\\d{2}", "ocr": "always"}}
]
```

The two text criteria share ONE OCR pass (same settings) and one
`text.p0.always.json`.

| Criterion | Score | Regions | Notes |
|---|---|---|---|
| `has text` | MARGINAL 5 | **4** boxes | One per paragraph cluster, ≈ `(96,192)-(352,288)`, `(512,192)-(768,256)`, `(512,448)-(800,480)`, `(96,416)-(256,480)`. Two columns produce more than one box — which is the point of the fixture: a single box over the whole page would mean the merge is broken |
| `Notice to Owner` | PASS 10 | 1–2 polygons, `source: "ocr"` | The OCR line polygon for the heading. A fuzzy window can clip the line above, so two regions is normal, not a bug |
| `total amount` | PASS 10 | 1 polygon, `source: "ocr"` | The `$4,850.00` line |

### `../documents/invoice_native.pdf`

```json
[
  {"name": "Notice to Owner", "type": "text", "options": {"ocr": "never"}},
  {"name": "total amount",    "type": "text",
   "options": {"match": "regex", "pattern": "\\$[\\d,]+\\.\\d{2}", "ocr": "never"}},
  {"name": "has text",        "type": "cv"}
]
```

The PDF has a native text layer, so `ocr: "never"` costs nothing and
`pdf-text` is the source. The layer is stored once as `text.p0.never.json`
(`source: "native"`).

| Criterion | Regions | Notes |
|---|---|---|
| `Notice to Owner` | 1 box, `source: "pdf-text"`, `attrs.pdf_rect ≈ [60.0, 220.2, 193.8, 239.4]` | Located by PyMuPDF's `search_for`. The region's own points are in **rendered page pixels** (1240×1755 at 150 dpi); `pdf_rect` is the same rectangle in points |
| `total amount` | 3 boxes | `$1,200.00`, `$3,650.00`, `$4,850.00`, each reconstructed from the word spans that cover the regex match — approximate by construction (see `loaders._rect_for_snippet`) |
| `has text` | several boxes | The text blocks of the one page |

`page_geometry[0].pdf_points` is `[595.2, 842.4]` (A4) and `working_scale` ≈ `0.569`.

### `../documents/photo_of_letter.png`

```json
[{"name": "Notice to Owner", "type": "text",
  "options": {"match": "fuzzy", "fuzzy_threshold": 0.8, "ocr": "always"}}]
```

The heading comes back as an `ocr` polygon at ≈ `(67, 197, 360, 244)` with
`score` ≈ `0.999` (the recogniser's own confidence) and `attrs.text` =
`"NOTICE TO OWNER"`. The polygon is a real quadrilateral, not a rectangle —
the page is photographed at a 3° skew and the OCR box follows it. The same
polygon is in `text.p0.always.json` → `lines[]`.

### `../Neighborhood.jpeg`

```json
[{"name": "has faces", "type": "cv"}, {"name": "has sky", "type": "cv"}]
```

`has sky` returns ~4 polygons.

`has faces` is **cascade- and version-dependent** — do not assert a number.
On the currently pinned `opencv-python-headless` (4.x, `<5`) it finds **2**
boxes here and PASSes; on other builds it has found none, which is equally
correct for a photo whose people are small and not frontal. The assertion
worth making is the structural one, and it holds either way: when a criterion
finds nothing it must come back with `"regions": []` and `"artifacts": null`,
never `"artifacts": {}`.

### `../Neighborhood.jpeg` with the detector

The same photo is the detector fixture, because it is the only one here with
real objects in it. Needs `DETECTOR_URL` set on the classifier container
(check `GET /document-kinds` → `regions.detector.configured` first); without
it a `detector` criterion is refused at submit with a 400.

```json
[
  {"name": "has roof vent",    "type": "cv"},
  {"name": "has bicycle",      "type": "detector", "options": {"threshold": 0.25}},
  {"name": "has sky",          "type": "cv"}
]
```

| Criterion | Path | What to expect |
|---|---|---|
| `has roof vent` | `detector` | A `cv` criterion **no** OpenCV detector matches; with `DETECTOR_URL` set its `options.fallback` resolves to `"detector"`, so it comes back `"method": "detector"` and scored from the boxes — 10 if the best score ≥ 0.5, 7 if above the 0.25 floor, **1/FAIL with no LLM call** if nothing is found |
| `has bicycle` | `detector` | Spelled `"type": "detector"` to name the path outright, with its own threshold |
| `has sky` | `cv` | Untouched. A real OpenCV detector matches, so the service is never asked |

On this photo OWLv2 reliably finds `house` (~9 boxes, best ≈ `0.6467` at
`[87.7, 6.5, 3574.8, 1903.4]`) and `tree` (~5 boxes, best ≈ `0.319`) at
threshold 0.2, and finds **nothing** for `car` or `swimming pool` — so if you
want a positive detector assertion, use `house` or `tree` as the criterion
name rather than the roof vent.

`result.detector` and the top of `regions.json` both carry
`{model, device, calls, labels, detections, elapsed_ms, errors}`. One call is
made per detector criterion. A detector that cannot be reached fails **that
criterion** (`status: "error"`), and the job reports `complete: false`.

> `greenery_and_sky.png` returns **zero** detections for every label — it is a
> synthetic gradient with no objects in it, only coloured regions. Use the
> OpenCV mask detectors for that kind of question; use the detector for
> things with names.

### `../Neighborhood.jpeg` with the LLM enforcement loop

```json
[{"name": "solar panels", "type": "llm", "options": {"hint": "presence", "boxes": true}},
 {"name": "a parked car", "type": "llm", "options": {"hint": "presence", "boxes": true, "max_attempts": 2}}]
```

This is the one fixture row whose numbers are **not** reproducible — it
depends on the model. What IS invariant, and worth asserting:

| Invariant | Why it matters |
|---|---|
| The `score` and `verdict` are identical to the same request with `boxes: false` | The loop runs after scoring and only adds keys |
| `localization.attempts` has ≥ 1 entry, each with `valid`, `accepted`, and a `reject` when rejected | Every attempt comes back, which is the point |
| `localization.calls` = asks + refines + verifies actually made | The cost is visible |
| `a parked car` never has more than 2 attempts | `options.max_attempts` bounds it (and may never exceed `CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS`) |
| An accepted attempt's box is inside the page and under 95% of its area | Validation actually ran |
| `artifacts.attempts[n].svg` renders THAT attempt's box, even a rejected one | `?attempt=n` is how a bad box gets looked at |
| With no `attempt`, `?criterion=<slug>` renders the ACCEPTED box only | The filtered and combined views must agree |
| A criterion the model scores < 7 produces **no** attempts at all | The presence gate |

Add `"score": false` to either criterion to get the geometry with no
judgement — the presence call still gates the loop, and its score, verdict
and confidence come back `null`.

For real numbers on your model, run the experiment rather than guessing:

```bash
docker compose exec classifier python bin/grounding_experiment.py
docker compose exec classifier python bin/grounding_experiment.py \
  --images /data/your-photos --criteria /data/criteria.json --out /data/grounding.json
```

---

## Curl examples

Set `job=<the job_id returned by the POST>` first. Direct base URL shown; via
LiteLLM prefix everything with `http://localhost:4001/v1/classifier` and add
`-H "Authorization: Bearer sk-…"`.

```bash
# Submit (regions are always stored)
curl -s http://localhost:8005/assess \
  -F "file=@unit-tests/classifier/regions/greenery_and_sky.png" \
  -F 'criteria=[{"name":"has sky","type":"cv"},{"name":"has vegetation","type":"cv"},
                {"name":"has water","type":"cv"},{"name":"sharpness","type":"cv"}]'
# → {"job_id":"abc123","phase":"pending"}

job=abc123

# Poll until completed, then read the regions inline
curl -s http://localhost:8005/jobs/$job \
  | jq '.result.assessment.per_criterion_scores["has sky"].regions'

# The manifest: what is in the directory, and the slug for each criterion
curl -s http://localhost:8005/jobs/$job/artifacts | jq '{files: [.files[].name], criteria}'

# One layer, combined — rendered now, cached for next time
curl -s http://localhost:8005/jobs/$job/artifacts/p0.svg -o p0.svg

# The same layer filtered to one criterion
slug=$(curl -s http://localhost:8005/jobs/$job/artifacts | jq -r '.criteria["has water"].slug')
curl -s "http://localhost:8005/jobs/$job/artifacts/p0.svg?criterion=$slug" -o water.svg
curl -s "http://localhost:8005/jobs/$job/artifacts/p0.preview.jpg?criterion=$slug" -o water.jpg

# Filter by source instead (cv | ocr | pdf-text | llm | detector)
curl -s "http://localhost:8005/jobs/$job/artifacts/p0.svg?source=cv" -o cv-only.svg

# Just this criterion's geometry, no pictures
curl -s "http://localhost:8005/jobs/$job/artifacts/regions.json?criterion=$slug" | jq .

# The exact text a text criterion searched (for a job that had one)
curl -s http://localhost:8005/jobs/$job/artifacts/text.p0.auto.json | jq '{source, chars}'
curl -s http://localhost:8005/jobs/$job/artifacts/text.p0.auto.txt

# Everything as a zip
curl -s http://localhost:8005/jobs/$job/artifacts.zip -o layers.zip && unzip -l layers.zip

# Free the disk but keep the job and its inline regions
curl -s -X DELETE http://localhost:8005/jobs/$job/artifacts -i | head -1   # 204

# …after which the endpoints answer 410 (gone), not 404 (never existed)
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8005/jobs/$job/artifacts  # 410
```

### Locate without judging — `"score": false`

```bash
curl -s http://localhost:8005/assess \
  -F "file=@unit-tests/classifier/regions/text_blocks.png" \
  -F 'criteria=[{"name":"has text","type":"cv","score":false},
                {"name":"Notice to Owner","type":"text","score":false,
                 "options":{"match":"fuzzy","fuzzy_threshold":0.8,"ocr":"always"}}]'

curl -s http://localhost:8005/jobs/$job | jq '.result.assessment | {overall_score, per: [.per_criterion_scores[] | {score, regions: (.regions | length)}]}'
# → overall_score null; each criterion score null, regions present
```

A `score: false` criterion that cannot produce geometry is refused at submit
(an `llm` criterion without `options.boxes`, the whole-page `sharpness` /
`exposure` measurements, a `cv` name that falls back to the llm), and so is
any `score: false` criterion on a .txt / .docx.

---

## Things that should NOT happen

Worth asserting, because each one is a real failure mode:

* **A region outside the page.** Every point must be within
  `0..page_geometry.width` × `0..height`. A coordinate near 1000 on a
  3024-px page means the working-image rescale was skipped.
* **`"artifacts": {}` for a criterion with no regions and no text.** It must
  be `null` — an empty object implies URLs that would 404.
* **A URL in `artifacts.files[]` that 404s.** The list is built from the
  directory *after* the byte cap runs, so a file the cap dropped is absent
  rather than listed. (`artifacts.items[].layers` URLs are different: they are
  rendered on fetch, so they work whether or not the file exists yet.)
* **An SVG that will not parse.** Criterion names go into ids, attributes and
  `<title>` tooltips; `xml.etree.ElementTree.fromstring` on any returned SVG
  must succeed even for a name containing `&` or `<`.
* **A score that moved because `boxes` was on.** The enforcement loop runs
  after the scoring call and reads it; it must never write to it.
* **A rejected attempt with no way to see it.** `localization.attempts` must
  list every attempt, and each one that produced four numbers must appear in
  `regions` with `attrs.accepted: false` and be renderable at
  `?criterion=<slug>&attempt=n`.
* **The combined layer showing rejected boxes.** `p0.svg` with no query
  parameters, and `?criterion=<slug>` with no `attempt`, must both show the
  ACCEPTED box only.
* **A `text.p<n>.<key>.json` whose `text` differs from what was searched.** The
  file is the evidence for a text criterion's verdict; it is written from the
  exact layer the matcher saw.
