# Classifier API

FastAPI service that assesses image quality and features via OpenCV detectors
and Qwen2.5-VL-7B LLM scoring.

Base URL (direct): `http://<host>:8005`
Base URL (via LiteLLM passthrough): `http://<host>:4001/v1/classifier`

All passthrough requests require `Authorization: Bearer <master-key>`.

---

## Async job pattern

`POST /assess` and `POST /assess/compare` return **202 Accepted** immediately
with a job ID. Poll `GET /jobs/{job_id}` until `status` is `"completed"` or
`"failed"`, then read the `result` field.

```
POST /assess  →  {"job_id": "abc123", "status": "pending"}
                         ↓  poll
GET /jobs/abc123  →  {"status": "completed", "result": {...}}
```

---

## `CriterionInput` — shared input object

Used in both `/assess` (as a JSON array string) and `/assess/compare` (as a
JSON array in the request body).

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | — | The criterion to evaluate. Free-form text — the LLM interprets any name. |
| `type` | `"cv"` \| `"llm"` | `"llm"` | `"llm"`: scored by the vision LLM. `"cv"`: run through a registered OpenCV detector by name; falls back to LLM if no detector matches. |
| `weight` | float (> 0) | `1.0` | Relative weight in the combined weighted score. Higher = matters more. |
| `hint` | `"quality"` \| `"presence"` \| `"auto"` | `"auto"` | Tells the LLM which scoring rubric to use. `"quality"`: score image quality 1-10. `"presence"`: detect feature presence/absence. `"auto"`: LLM infers from the criterion name. Ignored for `cv` criteria that match a detector. |
| `depends_on` | string \| null | `null` | Name of another criterion that must **PASS** (score ≥ 7) before this criterion is evaluated. If the dependency does not pass — including if it was itself skipped — this criterion is marked `SKIPPED` and excluded from the weighted score. Chains are supported: A → B → C all skip if A fails. |

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

### `POST /assess`

Submit a single-image assessment job.

**Content-Type:** `multipart/form-data`  
**Returns:** `202 Accepted` — poll `GET /jobs/{job_id}` for the result.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `image` | File | Yes | — | JPEG or PNG image |
| `criteria` | string (JSON array) | No | see defaults | JSON array of `CriterionInput` objects |

**Example request:**

```bash
curl http://localhost:4001/v1/classifier/assess \
  -H "Authorization: Bearer sk-1234" \
  -F "image=@meter.jpg" \
  -F 'criteria=[
    {"name":"has electrical meter",                 "type":"llm","hint":"presence","weight":4.0},
    {"name":"meter value is readable",              "type":"llm","hint":"quality", "weight":3.0,"depends_on":"has electrical meter"},
    {"name":"three feet of clearance around meter", "type":"llm","hint":"presence","weight":2.5,"depends_on":"has electrical meter"},
    {"name":"sharpness",                            "type":"cv",                  "weight":1.0}
  ]'
# → {"job_id": "abc123", "status": "pending"}
```

**Polling:**

```bash
curl http://localhost:4001/v1/classifier/jobs/abc123 -H "Authorization: Bearer sk-1234"
```

**Result shape** (inside `job.result`):

```json
{
  "image_info": {"width": 480, "height": 640, "format": "image/jpeg", "size_bytes": 112400},
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

---

### `POST /assess/compare`

Submit a comparison job — assess an input image against one or more reference examples.

**Content-Type:** `application/json`  
**Returns:** `202 Accepted` — poll `GET /jobs/{job_id}` for the result.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `image` | `ImageInput` | Yes | — | The subject image |
| `criteria` | `CriterionInput[]` | No | defaults | List of criterion objects |
| `aggregation` | `mean` \| `min` \| `max` | No | `mean` | How to collapse per-example scores into one aggregate |
| `examples` | `ExampleInput[]` | Yes (min 1) | — | Reference images to compare against |

#### `ImageInput`

| Field | Type | Description |
|---|---|---|
| `data` | string | Base64-encoded image or a URL (SSRF-checked before fetching) |
| `type` | `"base64"` \| `"url"` | How to interpret `data` |

#### `ExampleInput`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `data` | string | Yes | — | Base64-encoded image or URL |
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
    {"name": "meter value is readable", "type": "llm", "hint": "quality", "weight": 3.0, "depends_on": "has electrical meter"}
  ],
  "aggregation": "mean",
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
{"id": "abc123", "status": "completed", "type": "assess", "result": {...}, "created_at": "...", "updated_at": "..."}
```

`status` values: `pending` → `processing` → `completed` | `failed`

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
| — | SKIPPED (dependency did not pass) |

SKIPPED criteria are excluded from the weighted score calculation entirely.
