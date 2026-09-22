# Classifier region fixtures

Hand-testing material for the classifier's **regions and layers**: which
criteria localise on which file, what geometry to expect back, and the curl
calls for every artifact endpoint. The two synthetic images here are generated
by [`make_fixtures.py`](make_fixtures.py) — edit the recipe and re-run rather
than hand-editing a binary:

```bash
uv run --package classifier python unit-tests/classifier/regions/make_fixtures.py
```

The Postman collection mirrors this folder: `ai/classifier/classifier.postman_collection.json`
has a **Regions** subfolder with one ready-to-run request per fixture and an
**Artifacts** subfolder for the four endpoints. Import it, set the `litellm`
and `virtual master key` variables, and click Send.

Total size: ~800 KB (every PNG is JPEG round-tripped so PNG can compress the
texture noise).

---

## Files

Two are generated here; three are **referenced, not copied** — duplicating a
fixture is how two copies drift apart.

| File | Kind | What it exercises |
|---|---|---|
| [`greenery_and_sky.png`](greenery_and_sky.png) | image, 900×700 | `detect_sky`, `detect_vegetation`, `detect_water` — three mask-derived polygon sets in places you can check by eye |
| [`text_blocks.png`](text_blocks.png) | image, 900×700 | `detect_text`'s merged high-density block boxes (two columns, so the merge has to produce more than one box); with `ocr=always`, OCR line polygons behind a `text` criterion |
| [`scene_before.png`](scene_before.png) / [`scene_after.png`](scene_after.png) | image, 900×700 | Change detection (`regions.diff` on `/assess/compare`). One synthetic yard shot twice: the camera moved ~6 px and rotated ~1°, a red car appeared, a shed went |
| [`../Neighborhood.jpeg`](../Neighborhood.jpeg) | image | A **real** photo — the only honest input for `has faces`, and a realistic sky/vegetation mix |
| [`../documents/photo_of_letter.png`](../documents/photo_of_letter.png) | image | `source="ocr"` regions: a photographed letter whose text hits map back to OCR line polygons |
| [`../documents/invoice_native.pdf`](../documents/invoice_native.pdf) | pdf | `source="pdf-text"` regions: native PDF rectangles via `search_for` (literal modes) and word-span reconstruction (regex/fuzzy), with `attrs.pdf_rect` in points |

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
`cv`, `text` and `diff` rows are deterministic and should reproduce exactly;
the `detector` rows depend on a model and are given as ranges; the `llm` rows
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

Send it with `-F "regions=svg,png,preview"`.

| Criterion | Score | Regions | Notes |
|---|---|---|---|
| `has sky` | **PASS** 10 | 1 polygon, bbox ≈ `(0, 0, 899, 243)` | The detector only looks at the top 35% of the page, so the polygon is that band |
| `has vegetation` | **PASS** 10 | 1 polygon, bbox ≈ `(0, 359, 899, 699)` | 8 vertices after `approxPolyDP` — the lumpy top edge survives simplification |
| `has water` | **PASS** 10 | **2** polygons: the sky band **and** the pool at ≈ `(90, 518, 430, 651)` | Expected, and worth understanding: `detect_water` accepts any blue region whose Laplacian variance is under 200, and a smooth sky qualifies. The detector's honest answer, not a fixture flaw — the pool is the second, smaller one |
| `sharpness` | PASS | **none** — `"regions": []`, `"artifacts": null` | A whole-page measurement never fabricates a full-page box |

Artifacts written: `manifest.json`, `regions.json`, `p0.svg`, `p0.layer.png`,
`p0.preview.jpg`, and `p0.base.jpg` (the un-annotated page, kept only because
`preview` was requested, so a *filtered* preview can be re-composited).

### `text_blocks.png`

```json
[
  {"name": "has text",        "type": "cv"},
  {"name": "Notice to Owner", "type": "text", "match": "fuzzy", "fuzzy_threshold": 0.8},
  {"name": "total amount",    "type": "text", "match": "regex", "pattern": "\\$[\\d,]+\\.\\d{2}"}
]
```

Send it with `-F "ocr=always" -F "regions=svg"` — the text criteria need OCR,
since a PNG has no text layer.

| Criterion | Score | Regions | Notes |
|---|---|---|---|
| `has text` | MARGINAL 5 | **4** boxes | One per paragraph cluster, ≈ `(96,192)-(352,288)`, `(512,192)-(768,256)`, `(512,448)-(800,480)`, `(96,416)-(256,480)`. Two columns produce more than one box — which is the point of the fixture: a single box over the whole page would mean the merge is broken |
| `Notice to Owner` | PASS 10 | 1–2 polygons, `source: "ocr"` | The OCR line polygon for the heading. A fuzzy window can clip the line above, so two regions is normal, not a bug |
| `total amount` | PASS 10 | 1 polygon, `source: "ocr"` | The `$4,850.00` line |

### `../documents/invoice_native.pdf`

```json
[
  {"name": "Notice to Owner", "type": "text", "match": "contains"},
  {"name": "total amount",    "type": "text", "match": "regex", "pattern": "\\$[\\d,]+\\.\\d{2}"},
  {"name": "has text",        "type": "cv"}
]
```

Send it with `-F "ocr=never" -F "regions=true"` — the PDF has a native text
layer, so OCR is not needed and `pdf-text` is the source.

| Criterion | Regions | Notes |
|---|---|---|
| `Notice to Owner` | 1 box, `source: "pdf-text"`, `attrs.pdf_rect ≈ [60.0, 220.2, 193.8, 239.4]` | Located by PyMuPDF's `search_for`. The region's own points are in **rendered page pixels** (1240×1755 at 150 dpi); `pdf_rect` is the same rectangle in points |
| `total amount` | 3 boxes | `$1,200.00`, `$3,650.00`, `$4,850.00`, each reconstructed from the word spans that cover the regex match — approximate by construction (see `loaders._rect_for_snippet`) |
| `has text` | boxes on both pages | Regions are kept for **every** page even though the score reports the worst one |

`page_geometry[0].pdf_points` is `[595.2, 842.4]` and `working_scale` ≈ `0.569`.

### `../documents/photo_of_letter.png`

```json
[{"name": "Notice to Owner", "type": "text", "match": "fuzzy", "fuzzy_threshold": 0.8}]
```

With `-F "ocr=always" -F "regions=svg,preview"`: the heading comes back as an
`ocr` polygon at ≈ `(67, 197, 360, 244)` with `score` ≈ `0.999` (the
recogniser's own confidence) and `attrs.text` = `"NOTICE TO OWNER"`. The
polygon is a real quadrilateral, not a rectangle — the page is photographed at
a 3° skew and the OCR box follows it.

### `../Neighborhood.jpeg`

```json
[{"name": "has faces", "type": "cv"}, {"name": "has sky", "type": "cv"}]
```

`has sky` returns ~3 polygons. `has faces` usually returns **none** on this
photo (the people are small and not frontal), in which case the criterion
FAILs with `"regions": []` and `"artifacts": null` — which is the assertion
worth making: no regions must mean no artifact block, not an empty one.

### `../Neighborhood.jpeg` with the detector (phase 2)

The same photo is the detector fixture, because it is the only one here with
real objects in it. Needs `DETECTOR_URL` set on the classifier container
(check `GET /document-kinds` → `regions.detector.configured` first); without
it the job still succeeds and `artifacts.notes` says the service is not
configured.

```json
[
  {"name": "has roof vent",    "type": "cv"},
  {"name": "has bicycle",      "type": "detector"},
  {"name": "has sky",          "type": "cv"},
  {"name": "has solar panels", "type": "llm", "hint": "presence"}
]
```

Send it with `-F 'regions={"enabled":true,"detector":true,"layers":["svg","preview"]}'`
— the full object, not the comma shorthand, because `detector` is not a layer
format.

| Criterion | Path | What to expect |
|---|---|---|
| `has roof vent` | `detector` | A `cv` criterion **no** OpenCV detector matches, so `regions.detector` routes it to the service. Comes back `"method": "detector"` and scored from the boxes — 10 if the best score ≥ 0.5, 7 if above the 0.25 floor, **1/FAIL with no LLM call** if nothing is found |
| `has bicycle` | `detector` | Spelled `"type": "detector"` to name the path outright. As a `cv` criterion it now reaches the service too — `get_detector`'s fuzzy cutoff is 0.8 (`CV_NAME_FUZZY_CUTOFF`), so it no longer collides with the `has faces` Haar cascade the way it did at 0.6 |
| `has sky` | `cv` | Untouched. A real OpenCV detector matches, so the service is never asked |
| `has solar panels` | `llm` | The **model still scores it**; the detector only localises. `regions` (if any) carry `source: "detector"` |

On this photo OWLv2 reliably finds `house` (~9 boxes, best ≈ `0.6467` at
`[87.7, 6.5, 3574.8, 1903.4]`) and `tree` (~5 boxes, best ≈ `0.319`) at
threshold 0.2, and finds **nothing** for `car` or `swimming pool` — so if you
want a positive detector assertion, use `house` or `tree` as the criterion
name rather than the roof vent.

`result.document_info.detector` and the top of `regions.json` both carry
`{model, device, calls, labels, detections, elapsed_ms, errors}`. One call is
made per page image, carrying every label for that page.

> `greenery_and_sky.png` returns **zero** detections for every label — it is a
> synthetic gradient with no objects in it, only coloured regions. That is the
> correct answer and the reason the detector fixture is the real photo. Use
> the OpenCV mask detectors for that kind of question; use the detector for
> things with names.

### `scene_before.png` + `scene_after.png` — change detection (phase 4)

The only fixture that needs `/assess/compare`, because a diff needs a
reference. `scene_after.png` is the SUBJECT (it is the later photo);
`scene_before.png` is the example.

```json
{
  "image":    {"data": "<base64 of scene_after.png>",  "type": "base64"},
  "criteria": [{"name": "has sky", "type": "cv"}],
  "ocr": "never",
  "examples": [{"data": "<base64 of scene_before.png>", "type": "base64", "weight": 0.5}],
  "regions":  {"enabled": true, "layers": ["svg", "preview"], "diff": true, "examples": true}
}
```

| What | Expected |
|---|---|
| `example_results[0].diff.aligned` | `true` |
| `example_results[0].diff.inliers` | ~147 — comfortably over the 30 floor, because the fence pickets and window frames give ORB plenty of corners |
| `added` regions | **1**, bbox ≈ `(130, 509, 340, 623)` — the red car, including its wheels (which overhang the body that was drawn) |
| `removed` regions | **2**, bbox ≈ `(704, 322, 837, 362)` and `(754, 423, 790, 471)` — the shed's roofline and its door. It fragments because the middle of a flat brown wall against flat green ground does not clear the Otsu threshold; two regions in the right place is the honest output, not a bug |
| `changed` regions | **0** on this pair |
| `aggregate` | **Identical** to the same request with `"diff": false`. Assert this: diff regions are not criteria and must never reach the weighted score |
| Files written | `diff-e0-p0.svg`, `diff-e0-p0.preview.jpg`, plus `e0.p0.svg` / `e0.regions.json` from `"examples": true`, alongside the subject's own `p0.*` |

`regions.json` gains a `_diff:e0` entry whose regions carry
`source: "diff"` and `attrs.change`. The layer is filterable like any other:
`?criterion=<the _diff:e0 slug>`.

Two negative checks worth making, both of which a broken alignment step would
fail:

* **The house must not be a region.** It is in both frames and did not move
  relative to the scene; if it shows up, the homography was not applied.
* **Swap in an unrelated image** (`text_blocks.png` as the example) and the
  answer must be `aligned: false`, `regions: []`, and a note saying the two
  are probably not views of the same scene — *not* a page full of boxes.

### `../Neighborhood.jpeg` with the LLM enforcement loop (phase 3)

```json
[{"name": "solar panels", "type": "llm", "hint": "presence"},
 {"name": "a parked car", "type": "llm", "hint": "presence"}]
```

Send it with
`-F 'regions={"enabled":true,"llm_boxes":true,"detector":true,"layers":["svg","preview"]}'`.

This is the one fixture row whose numbers are **not** reproducible — it
depends on the model. What IS invariant, and worth asserting:

| Invariant | Why it matters |
|---|---|
| The `score` and `verdict` are identical to the same request with `llm_boxes` off | The loop runs after scoring and only adds keys |
| `localization.attempts` has ≥ 1 entry, each with `valid`, `accepted`, and a `reject` when rejected | Every attempt comes back, which is the point |
| `localization.calls` = attempts + (verify calls for the attempts that validated) | The cost is visible |
| An accepted attempt's box is inside the page and under 95% of its area | Validation actually ran |
| `artifacts.attempts[n].svg` renders THAT attempt's box, even a rejected one | `?attempt=n` is how a bad box gets looked at |
| With no `attempt`, `?criterion=<slug>` renders the ACCEPTED box only | The filtered and combined views must agree |
| A criterion the model scores < 7 produces **no** attempts at all | The presence gate |

For real numbers on your model, run the experiment rather than guessing:

```bash
docker compose exec classifier python bin/grounding_experiment.py
docker compose exec classifier python bin/grounding_experiment.py \
  --images /data/your-photos --criteria /data/criteria.json --out /data/grounding.json
```

It prints attempt-1 validity, verify pass rate, mean attempts to accept, and
(with `DETECTOR_URL` set) mean IoU against the detector, per criterion. Under
~50% attempt-1 validity, prefer `regions.detector` as the primary source.

---

## Curl examples

Set `job=<the job_id returned by the POST>` first. Direct base URL shown; via
LiteLLM prefix everything with `http://localhost:4001/v1/classifier` and add
`-H "Authorization: Bearer sk-…"`.

```bash
# Submit with layers
curl -s http://localhost:8005/assess \
  -F "file=@unit-tests/classifier/regions/greenery_and_sky.png" \
  -F "ocr=never" \
  -F "regions=svg,png,preview" \
  -F 'criteria=[{"name":"has sky","type":"cv"},{"name":"has vegetation","type":"cv"},
                {"name":"has water","type":"cv"},{"name":"sharpness","type":"cv"}]'
# → {"job_id":"abc123","phase":"pending"}

job=abc123

# Poll until completed, then read the regions inline
curl -s http://localhost:8005/jobs/$job \
  | jq '.result.assessment.per_criterion_scores["has sky"].regions'

# The manifest: what is in the directory, and the slug for each criterion
curl -s http://localhost:8005/jobs/$job/artifacts | jq '{files: [.files[].name], criteria}'

# One layer, combined
curl -s http://localhost:8005/jobs/$job/artifacts/p0.svg -o p0.svg

# The same layer filtered to one criterion (re-rendered on demand)
slug=$(curl -s http://localhost:8005/jobs/$job/artifacts | jq -r '.criteria["has water"].slug')
curl -s "http://localhost:8005/jobs/$job/artifacts/p0.svg?criterion=$slug" -o water.svg
curl -s "http://localhost:8005/jobs/$job/artifacts/p0.preview.jpg?criterion=$slug" -o water.jpg

# Filter by source instead (cv | ocr | pdf-text)
curl -s "http://localhost:8005/jobs/$job/artifacts/p0.svg?source=cv" -o cv-only.svg

# Just this criterion's geometry, no pictures
curl -s "http://localhost:8005/jobs/$job/artifacts/regions.json?criterion=$slug" | jq .

# Everything as a zip
curl -s http://localhost:8005/jobs/$job/artifacts.zip -o layers.zip && unzip -l layers.zip

# Free the disk but keep the job and its inline regions
curl -s -X DELETE http://localhost:8005/jobs/$job/artifacts -i | head -1   # 204

# …after which the endpoints answer 410 (gone), not 404 (never existed)
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8005/jobs/$job/artifacts  # 410
```

### `/locate` — no score, just the geometry

```bash
curl -s http://localhost:8005/locate \
  -F "file=@unit-tests/classifier/regions/text_blocks.png" \
  -F "ocr=always" \
  -F "regions=svg" \
  -F 'features=["has text", {"name":"Notice to Owner","type":"text","match":"fuzzy","fuzzy_threshold":0.8}]'

curl -s http://localhost:8005/jobs/$job | jq '.result.features | keys'
# → ["Notice to Owner", "has text"]   — no overall_score, no verdict
```

A bare string feature resolves to a `cv` detector when one matches the name,
then to the open-vocabulary `detector` when `DETECTOR_URL` is set, then to
`llm`. An `llm` feature comes back with empty regions and a reason naming the
option to set — unless the request carries
`-F 'regions={"enabled":true,"llm_boxes":true}'`, which runs the enforcement
loop for it (a presence-only scoring call, then ask → validate →
verify-by-crop → retry) and returns regions plus `localization` with still no
score and no verdict.

---

## Things that should NOT happen

Worth asserting, because each one is a real failure mode:

* **A region outside the page.** Every point must be within
  `0..page_geometry[n].width` × `0..height`. A coordinate near 1000 on a
  3024-px page means the working-image rescale was skipped.
* **A different score with `regions` on.** Submit the same file twice, once
  with `regions` and once without: `overall_score`, every per-criterion
  `score`, and every `verdict` must be identical. Rendering is strictly
  additive.
* **`"artifacts": {}` for a criterion with no regions.** It must be `null` —
  an empty object implies URLs that would 404.
* **A URL in `artifacts.files[]` that 404s.** The list is built from the
  directory *after* the byte cap runs, so a file the cap dropped is absent
  rather than listed.
* **An SVG that will not parse.** Criterion names go into ids, attributes and
  `<title>` tooltips; `xml.etree.ElementTree.fromstring` on any returned SVG
  must succeed even for a name containing `&` or `<`.
* **A score that moved because `llm_boxes` was on.** The enforcement loop runs
  after the scoring call and reads it; it must never write to it. Submit the
  same document with and without `llm_boxes` — the scores, verdicts and
  `overall_score` must match exactly, including in the case where every
  attempt was rejected.
* **A rejected attempt with no way to see it.** `localization.attempts` must
  list every attempt, and each one that produced four numbers must appear in
  `regions` with `attrs.accepted: false` and be renderable at
  `?criterion=<slug>&attempt=n`. "We tried three times" with nothing to look
  at is not returning every attempt.
* **The combined layer showing rejected boxes.** `p0.svg` with no query
  parameters, and `?criterion=<slug>` with no `attempt`, must both show the
  ACCEPTED box only. A filtered view and the unfiltered file must never
  disagree about a criterion they both contain.
* **A diff that changed the aggregate.** Submit the same compare body with
  `"diff": true` and `"diff": false`: `aggregate`, every `similarity`, and
  every `combined_score` must be byte-identical. Diff regions are not
  criteria.
* **A diff drawn on images that did not align.** `aligned: false` must come
  with `regions: []` and a note. Boxes over two photos of different scenes is
  the single worst output this subsystem could produce.
