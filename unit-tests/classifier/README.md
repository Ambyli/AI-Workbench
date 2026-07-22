# Classifier Image Tests

Pytest suite that exercises the classifier API (`/v1/classifier/assess`) against
known-good and known-bad images for every field defined in
`field_criteria.json`. Each field has one test function that submits every
pass image in its folder, then submits a shared degraded fail image, and
asserts the resulting verdicts.

## Layout

```
unit-tests/classifier/
├─ field_criteria.json                  # per-field criteria (name, type, weight)
├─ test_image_classifications.py        # 60 pytest functions, one per field
├─ test_classifier_endpoint.py          # standalone smoke test (unrelated)
└─ images/
   ├─ _shared_fail.<jpg|png|...>        # one obviously-wrong photo
   ├─ safetyForm/pass.jpg
   ├─ safetyForm/pass (1).jpg           # optional extra pass images
   ├─ exclusionZones/pass.jpg
   └─ …one folder per field…
```

### Pass images

Any file in `images/<fieldName>/` whose stem matches `pass`, `pass (N)`,
`pass N`, or `passN` (extensions `.jpg .jpeg .png .webp`) is picked up.
Each is submitted individually and its full weighted-score breakdown is
printed. The test passes only if **every** pass image gets `verdict = PASS`.

### Fail images

Resolution order:

1. `images/<fieldName>/fail.jpg` (per-field manual override), else
2. `images/_shared_fail.<ext>` — blurred + darkened on the fly via Pillow
   so both CV quality criteria and LLM content criteria fail. Cached to
   `_shared_fail.degraded.jpg` after the first run.

### Missing images

A field with no pass images calls `pytest.skip(...)` cleanly. Same when
`_shared_fail.*` is missing.

## Setup

Install dependencies (one time):

```powershell
pip install pytest requests pillow
```

`pillow` is optional but recommended — without it the shared fail image is
used raw, so CV quality criteria may still pass and the aggregate verdict
may not reach `FAIL`.

## Running

All commands assume you are in the project root
(`C:\Users\Amber Price\Desktop\claude usage`).

### All tests

```powershell
pytest unit-tests/classifier/test_image_classifications.py -s
```

`-s` lets `print()` output through so you see the per-image weighted-score
breakdown live.

### One field

```powershell
pytest unit-tests/classifier/test_image_classifications.py -k safetyForm -s
```

`-k` matches substrings. Combine with `or` / `and` for multiple:

```powershell
pytest unit-tests/classifier/test_image_classifications.py -k "railEnd or railSplice" -s
```

### Verbose list (PASS / FAIL / SKIPPED per field)

```powershell
pytest unit-tests/classifier/test_image_classifications.py -v -s
```

### Show why tests were skipped

```powershell
pytest unit-tests/classifier/test_image_classifications.py -rs
```

### Stop on first failure

Useful when tuning weights or debugging one field:

```powershell
pytest unit-tests/classifier/test_image_classifications.py -x -s
```

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `CLASSIFIER_BASE_URL` | `http://192.168.5.233:4001` | LiteLLM passthrough for the classifier |
| `CLASSIFIER_API_KEY` | read from `../../.env` (`DEFAULT_LITELLM_MASTER_KEY`) | Bearer token |

Override for a single run:

```powershell
$env:CLASSIFIER_BASE_URL = "http://192.168.5.233:4001"
$env:CLASSIFIER_API_KEY = "sk-…"
pytest unit-tests/classifier/test_image_classifications.py -s
```

## Timing

Each assessment takes ~5–30s (LLM latency). A full run against all 45
populated fields, some with 5–6 pass images each, will take several
minutes. Use `-k <field>` while iterating.

## Adding a new field

1. Add the field's entry to `field_criteria.json` (`fieldName`,
   `displayName`, `classifierCriteria`).
2. Add `def test_<fieldName>(base_url, api_key): _run_field(base_url, api_key, "<fieldName>")`
   to `test_image_classifications.py`.
3. Create `images/<fieldName>/` and drop in at least one `pass.jpg`.
