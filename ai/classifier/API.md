# Quality Checker API

FastAPI service that assesses image quality via OpenCV pre-checks and Qwen2.5-VL-7B LLM scoring.

Base URL (direct): `http://<host>:8005`
Base URL (via LiteLLM passthrough): `http://<host>:4001/v1/quality-check`

All passthrough requests require `Authorization: Bearer <master-key>`.

---

## Endpoints

### `GET /health`

Returns service status.

```bash
curl http://localhost:8005/health
```

```json
{ "status": "ok" }
```

---

### `POST /assess`

Assess a single image via CV pre-checks and LLM scoring.

**Content-Type:** `multipart/form-data`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `image` | File | Yes | — | JPEG or PNG image |
| `criteria` | string (JSON) | No | see below | JSON array of criterion objects. Each has `name` (string) and `type` (`"quality"` or `"feature"`, default `"quality"`). |

#### Criterion types

| Type | Behaviour | Score mapping |
|---|---|---|
| `quality` | Scores image quality on a 1–10 scale | 1-3 = FAIL, 4-6 = MARGINAL, 7-10 = PASS |
| `feature` | Detects whether a feature is present | 10 = clearly present (PASS), 5 = uncertain (MARGINAL), 1 = clearly absent (FAIL) |

`sharpness` and `exposure` are additionally backed by OpenCV measurements (Laplacian variance and mean pixel intensity) and merged into the LLM result. All criteria produce a `confidence` field (`high`/`medium`/`low`).

**Example request:**

```bash
curl http://localhost:8005/assess \
  -F "image=@Neighborhood.jpeg" \
  -F 'criteria=[{"name":"image sharpness","type":"quality"},{"name":"proper exposure","type":"quality"},{"name":"has solar panels","type":"feature"}]'

# Via LiteLLM passthrough
curl http://localhost:4001/v1/quality-check/assess \
  -H "Authorization: Bearer sk-1234" \
  -F "image=@Neighborhood.jpeg" \
  -F 'criteria=[{"name":"image sharpness","type":"quality"},{"name":"proper exposure","type":"quality"},{"name":"has solar panels","type":"feature"}]'
```

**Example response:**

```json
{
  "status": "ok",
  "image_info": {
    "width": 1920,
    "height": 500,
    "format": "image/jpeg",
    "size_bytes": 318243
  },
  "cv_pre_checks": {
    "sharpness": {
      "criterion": "sharpness",
      "score": 8,
      "verdict": "PASS",
      "detail": "Laplacian variance: 312.4 (threshold: 100.0)"
    },
    "exposure": {
      "criterion": "exposure",
      "score": 7,
      "verdict": "PASS",
      "detail": "Normal exposure (mean: 142.3)"
    }
  },
  "cv_overall_verdict": "PASS",
  "llm_assessment": {
    "overall_verdict": "PASS",
    "overall_score": 8,
    "per_criterion_scores": {
      "image sharpness": {
        "score": 8,
        "verdict": "PASS",
        "confidence": "high",
        "reason": "Image is crisp with well-defined edges and fine detail visible"
      },
      "proper exposure": {
        "score": 7,
        "verdict": "PASS",
        "confidence": "high",
        "reason": "Balanced lighting with natural evening ambience"
      },
      "has solar panels": {
        "score": 10,
        "verdict": "PASS",
        "confidence": "high",
        "reason": "Solar panels are clearly visible on the rooftops of all houses"
      }
    }
  },
  "combined_verdict": "PASS"
}
```

---

### `POST /assess/compare`

Assess an input image against one or more reference examples. Each example has its own weight controlling how much similarity to that example influences its combined score. Examples without `pre_generated_analysis` are analysed concurrently.

**Content-Type:** `application/json`

#### Top-level fields

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `image` | `ImageInput` | Yes | — | The input image to assess |
| `criteria` | `CriterionInput[]` | No | see `/assess` defaults | List of criterion objects with `name` and `type` |
| `aggregation` | `mean` \| `min` \| `max` | No | `mean` | How to collapse per-example combined scores into one aggregate verdict |
| `examples` | `ExampleInput[]` | Yes | — | One or more reference images (min 1) |

**Aggregation options:**

| Value | Behaviour | Use when |
|---|---|---|
| `mean` | Average of all combined scores | All examples are equally important references |
| `min` | Lowest combined score wins | Input must be close to every example |
| `max` | Highest combined score wins | Input only needs to match any one example |

#### `ImageInput`

| Field | Type | Description |
|---|---|---|
| `data` | string | Base64-encoded image or a URL |
| `type` | `base64` \| `url` | Tells the service how to interpret `data` |

#### `ExampleInput`

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `data` | string | Yes* | — | Base64-encoded image or a URL |
| `type` | `base64` \| `url` | Yes* | — | How to interpret `data` |
| `weight` | float (0.0–1.0) | No | `0.5` | How much similarity to this example affects its combined score.<br>`0.0` = ignore example, use only absolute quality score.<br>`1.0` = ignore absolute quality, use only similarity score. |
| `pre_generated_analysis` | object | No | `null` | A prior analysis result (from any endpoint's `example_analysis` or `example_results[n].example_analysis`). When provided, the LLM call for this example is skipped entirely. |

\* Not required if `pre_generated_analysis` is provided and you don't need the image re-analysed.

#### Combined score formula

Each example produces its own combined score:

```
combined = (1 - weight) × input_overall_score + weight × similarity_score
```

Where `similarity_score` is a 0–10 scale derived from how closely the per-criterion scores of the input match the example. Scores are then aggregated across all examples using the chosen `aggregation` method.

**Verdicts:** `PASS` ≥ 7 · `MARGINAL` 4–6 · `FAIL` < 4

---

**Example request — two examples, one pre-generated:**

```json
{
  "image": {
    "data": "<base64-encoded input image>",
    "type": "base64"
  },
  "criteria": [
    {"name": "image sharpness", "type": "quality"},
    {"name": "proper exposure", "type": "quality"},
    {"name": "has solar panels", "type": "feature"}
  ],
  "aggregation": "mean",
  "examples": [
    {
      "data": "<base64-encoded reference image>",
      "type": "base64",
      "weight": 0.8
    },
    {
      "data": "https://example.com/reference2.jpg",
      "type": "url",
      "weight": 0.4,
      "pre_generated_analysis": {
        "image_info": { "width": 1920, "height": 1080, "format": "image/jpeg", "size_bytes": 245000 },
        "cv_pre_checks": {
          "sharpness": { "criterion": "sharpness", "score": 8, "verdict": "PASS", "detail": "Laplacian variance: 312.4 (threshold: 100.0)" },
          "exposure": { "criterion": "exposure", "score": 7, "verdict": "PASS", "detail": "Normal exposure (mean: 142.3)" }
        },
        "cv_overall_verdict": "PASS",
        "llm_assessment": {
          "overall_verdict": "PASS",
          "overall_score": 8,
          "per_criterion_scores": {
            "image sharpness": { "score": 8, "verdict": "PASS", "reason": "Crisp edges throughout" },
            "proper exposure": { "score": 7, "verdict": "PASS", "reason": "Balanced lighting" },
            "absence of artifacts": { "score": 9, "verdict": "PASS", "reason": "Clean render" }
          }
        },
        "combined_verdict": "PASS"
      }
    }
  ]
}
```

```bash
curl http://localhost:4001/v1/quality-check/assess/compare \
  -H "Authorization: Bearer sk-1234" \
  -H "Content-Type: application/json" \
  -d @request.json
```

**Example response:**

```json
{
  "status": "ok",
  "criteria": [
    {"name": "image sharpness", "type": "quality"},
    {"name": "proper exposure", "type": "quality"},
    {"name": "has solar panels", "type": "feature"}
  ],
  "aggregation": "mean",
  "input_analysis": {
    "image_info": { "width": 1920, "height": 500, "format": "image/jpeg", "size_bytes": 318243 },
    "cv_pre_checks": { "sharpness": { "score": 8, "verdict": "PASS", "detail": "..." }, "exposure": { "score": 7, "verdict": "PASS", "detail": "..." } },
    "cv_overall_verdict": "PASS",
    "llm_assessment": {
      "overall_verdict": "PASS",
      "overall_score": 8,
      "per_criterion_scores": {
        "image sharpness": { "score": 8, "verdict": "PASS", "reason": "..." },
        "proper exposure": { "score": 7, "verdict": "PASS", "reason": "..." },
        "absence of artifacts": { "score": 9, "verdict": "PASS", "reason": "..." }
      }
    },
    "combined_verdict": "PASS"
  },
  "example_results": [
    {
      "index": 0,
      "weight": 0.8,
      "pre_generated": false,
      "example_analysis": { "...": "same shape as input_analysis" },
      "similarity": {
        "overall_similarity": 0.889,
        "similarity_score": 8.9,
        "per_criterion": {
          "image sharpness": { "example_score": 9, "input_score": 8, "similarity": 0.889 },
          "proper exposure": { "example_score": 7, "input_score": 7, "similarity": 1.0 },
          "absence of artifacts": { "example_score": 8, "input_score": 9, "similarity": 0.889 }
        }
      },
      "combined_score": 8.7,
      "combined_verdict": "PASS"
    },
    {
      "index": 1,
      "weight": 0.4,
      "pre_generated": true,
      "example_analysis": { "...": "the pre_generated_analysis you passed in" },
      "similarity": {
        "overall_similarity": 0.741,
        "similarity_score": 7.4,
        "per_criterion": {
          "image sharpness": { "example_score": 8, "input_score": 8, "similarity": 1.0 },
          "proper exposure": { "example_score": 7, "input_score": 7, "similarity": 1.0 },
          "absence of artifacts": { "example_score": 9, "input_score": 9, "similarity": 1.0 }
        }
      },
      "combined_score": 7.8,
      "combined_verdict": "PASS"
    }
  ],
  "aggregate": {
    "method": "mean",
    "combined_score": 8.3,
    "combined_verdict": "PASS",
    "per_example_combined_scores": [8.7, 7.8]
  }
}
```

---

## Caching example analyses

Re-analysing the same reference image on every request wastes LLM tokens. The recommended pattern is:

1. Call `/assess` once on each reference image and save the response.
2. Pass the saved response as `pre_generated_analysis` in subsequent `/assess/compare` calls.

The `pre_generated_analysis` field accepts the full object returned under `example_results[n].example_analysis` in any compare response, or the root-level response from `/assess`.

---

## CV pre-check thresholds

| Check | Method | PASS threshold |
|---|---|---|
| Sharpness | Laplacian variance | ≥ 100.0 |
| Exposure | Mean pixel intensity | 30.0 – 220.0 |

These run on every image regardless of the criteria string and are merged into the LLM per-criterion scores if not already present.
