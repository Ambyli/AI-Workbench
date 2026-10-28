# ai/classifier

FastAPI service that scores documents (photos, PDFs, SVG, text, .docx; every page of every document is one item) against criteria answered by OpenCV detectors, text matching, the `ai/detector` service or a vision / text LLM chosen from the model registry `models.yaml`. Async jobs on a Postgres queue (`classifier-db`). Guide: [`API.md`](API.md). Compose: `docker-compose.classifier.yml` (`classifier-db`, `classifier`). Tests: [`API.md § Running the unit tests`](API.md#running-the-unit-tests).

## Rules

- **Postgres is the only store.** Every row (`jobs`, `job_progress`, `reference_examples`, `llm_calls`) is in `classifier-db` through ONE asyncpg pool (`db.py`, `db.pool_max()` = `CLASSIFIER_MAX_CONCURRENT` + Σ every model's `max_concurrent` + `DB_POOL_HEADROOM` 8) — a new registry entry grows the pool. No SQLite. DB unset or unreachable stops startup. `CLASSIFIER_DB_HOST` / port are fixed in the compose file, never `.env`. `jobs.result` is `JSON`, not `JSONB` (JSONB sorts keys; criteria keep request order). `job_progress` cascades with its job; `llm_calls` outlives it on purpose. [`API.md § Concurrency and durability`](API.md#concurrency-and-durability).
- **`models.yaml` is the model config, not env vars.** Bind-mounted read-only AND baked into the image (the build's import check loads it): an edit needs `docker restart classifier` (compose cannot see it). Validated at startup by `llm/models.py` (pydantic, unknown keys refused); invalid YAML, an unknown `default`, an unavailable default, a key on plain `http://` to a dotted host, or a `system_line` reasoning on a Muse entry stops the container. Precedence: `options.model` → request `model` → `default` (`muse-glimmer`). [`API.md § Models`](API.md#models).
- **An llm result's headline `confidence` stays the model's STATED number until its entry's `confidence.calibration` is fitted and `headline: calibrated` is set** (`bin/confidence_calibration.py` measures and fits; re-fit after a model, prompt or reasoning change). How confidence is measured (`confidence.method: logprobs | sampling | stated`, default `stated`) is a per-entry knob, never a model-name branch in code; the measured signals are `detail.confidence` (shadow). The logprob payload is never stored — `llm_calls` keeps `usage` / `finish_reason` only. Sampling via `n_param` holds n slots of `max_concurrent`. [`API.md § Confidence`](API.md#confidence--measured-then-calibrated).
- **Keys never live in the YAML.** An entry's `api_key_env` names an env var that must ALSO be listed in the compose `environment:` as `- VAR=${VAR:-}`; unset → `available: false`, selecting it is a 400. `GET /models` never returns a key.
- **Muse Glimmer reasoning rides as `chat_template_kwargs: {reasoning_strength: low}`.** Its stock template renders "Reasoning strength: low." itself and a system-prompt reasoning line would OVERRIDE it — never add one (load refuses `system_line` on Muse).
- **`max_images_per_prompt` must equal the vLLM server's `--limit-mm-per-prompt`** (3 = contrastive references, 2 = one example per call, 1 = none). A vLLM started without the flag rejects every reference-guided call — recreate `muse-glimmer` before the classifier. [`API.md § Deploying this version`](API.md#deploying-this-version).
- **Text-only entries (`vision: false`)** refuse boxes, references and image-only work at submit; each model's `timeout_s` bounds one non-streamed request and a timeout is not retried.
- **Limits, outer to inner:** `CLASSIFIER_MAX_CONCURRENT` (jobs) → `CLASSIFIER_MAX_UNITS_PER_JOB` (units of one job, a per-job `asyncio.Semaphore` in `analysis/scheduler.py`) → per-model `max_concurrent` (held in `llm/client.py` around the HTTP request, every call kind) and `CLASSIFIER_OCR_WORKERS` (process-wide `common.jobs.limits.ConcurrencyLimit`). A unit = (criterion, item); a `text` criterion with `scope: "document"` is one unit per document.
- **Drain the queue before a payload-schema change** (`schema: 3` today): an old payload is refused by the new runner and the job fails. Restart requeues `processing` rows.
- **The box loop never changes a score or a verdict**; it runs after scoring. Rejection wording is the retry's prompt — changing it changes behaviour. Re-run `bin/grounding_experiment.py` before trusting a new model or a new `CLASSIFIER_LLM_BBOX_GRID_STEP`. [`API.md § The LLM enforcement loop`](API.md#the-llm-enforcement-loop).
- **References are not swept.** `CLASSIFIER_REFERENCE_DIR` must stay outside `CLASSIFIER_ARTIFACT_DIR` (the sweeper deletes every directory whose job row is gone); only `DELETE /references/{id}` removes one; `CLASSIFIER_REFERENCE_MAX_COUNT` bounds the disk.
- **A criterion's `key` is for reference matching only.** Order `options.reference.criterion` (a reference criterion NAME) → `key` → `name`, case-folded, one rule in `references.resolve.match_target`; a keyed criterion never falls back to its name; keys may repeat on `/assess` but are unique within a reference (`check_unique_keys`; PATCH `criteria_keys` is the one edit a ready record takes). Results, `depends_on`, prompts and file names stay on `name` — the key never reaches a prompt. A plan without `matched_by` means `name`. [`API.md § How a criterion finds its examples`](API.md#how-a-criterion-finds-its-examples--the-matching-order).
- **The artifact byte cap never drops the evidence** (`regions.json`, `manifest.json`, `text.*.json`, the SVGs).
- **The worker never touches the network for input**: URLs and opt-in SVG `<image>` links are fetched at SUBMIT through `common.net.fetch_url` (SSRF guard, no redirects).
- **`POST /assess` is the only analysis endpoint**; `/locate` and `/assess/compare` are gone (404). Geometry without judgement is `"score": false`.
- **The trino_reader init script (`db-init/10-trino-reader.sh`) must stay safe to SOURCE**: the postgres entrypoint sources a script without an exec bit — no `exit`, no `set`.
- **A new subpackage under `ai/classifier/` needs its own `COPY` line in the `Dockerfile`** (one per subpackage, no `.dockerignore` on purpose); the build's import smoke turns a forgotten one into a build failure.
- **An API change updates `classifier.postman_collection.json` and `API.md`** (root `CLAUDE.md` § Postman collections).

## Result shapes

The standard: root `CLAUDE.md` § Result shapes declared beside the producer. Here:
- An OpenCV detector's `detail` — `@describes(DetectorSpec(...))` on the detector function (`cv/result.py`); the detector `REGISTRY` is the lookup, `GET /cv-detectors` serves it, `unit-tests/classifier/test_cv_measurements.py` checks it.
- A criterion type's `detail` — `@result_spec(ResultSpec(...))` on its evaluator (`analysis/{llm,text,detector,cv}_eval.py`), registered by type name in `analysis/result_specs.py` (looked up by the aggregator, the scheduler and `GET /criterion-types`); a new type's module is also listed once in `result_specs._DECLARING_MODULES`. `unit-tests/classifier/test_result_specs.py` checks it.
- `detail.reference` is ONE shared declaration, `REFERENCE_FIELD` in `analysis/result_specs.py`, listed on `LLM_RESULT` only — a `cv` criterion answered by the llm fallback carries it through the llm shape.
- Adding a detector, a type, or a key to a `detail` updates its declaration in the same change. [`API.md § Result details`](API.md#result-details--the-same-shape-on-every-type).

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **Local vLLM slots** — the `muse-glimmer` entry's `max_concurrent: 6` is 6 of vLLM's `--max-num-seqs 8` (`ai/vllm`), leaving 2 for LiteLLM chat; filling all 8 makes the overflow hook spill chat to Claude. `qwen3.8-solo` entry stays at `max_concurrent: 1` (3 sequences, chat runs there). `CLASSIFIER_MAX_UNITS_PER_JOB` (6) is matched to the default model's `max_concurrent`. Watch `classifier_llm_call_seconds{kind, model}` before raising either. (root `CLAUDE.md` § Cross-folder rules)
- **Image limit per prompt** — the `muse-glimmer` entry's `max_images_per_prompt: 3` must equal its vLLM `--limit-mm-per-prompt '{"image": 3}'` (`ai/vllm` compose); recreate `muse-glimmer` first when raising it, or every reference-guided call is rejected. (root `CLAUDE.md` § Cross-folder rules)
- **`qwen3.5-4b` sizing** — its entry's `max_concurrent: 2`, `max_tokens.score: 512`, `text_char_budget: 12000` match the llama.cpp service's `--parallel 2`, `--n-predict 512`, `-c 32768` (`ai/llama`); route-policy's `ROUTE_POLICY_ANALYSIS_CONCURRENCY` (2) equals that `max_concurrent`, and its ruleset `history.max_chars` must stay ≤ 12000. Change together. (root `CLAUDE.md` § Cross-folder rules)
- **Classifier as route-policy's model runner** — route-policy's `llm_classifier` calls `POST /assess`, `GET /models`, `GET` / `DELETE /jobs/{id}` (it always deletes); it submits only to an entry that is `authenticated: false`, `available: true` and resolves to private addresses. Changing those endpoints' shapes breaks `ai/route-policy/route_policy/analysis/`. Route jobs share this service's FIFO with document jobs. (root `CLAUDE.md` § Cross-folder rules)
- **Trino reads other stacks' Postgres** — `classifier-db` is federated as `postgres_classifier` (`ai/trino/catalogs/postgres_classifier.properties`) through the read-only `trino_reader` role made on the FIRST start of an empty `classifier_db` volume; `CLASSIFIER_TRINO_READER_PASSWORD` is also in `trino-coordinator`'s env. Retrofit / rotate: [`API.md § Concurrency and durability`](API.md#concurrency-and-durability). Trino reaches `classifier-db:5432` by service DNS, not `PORT_CLASSIFIER_DB`. (root `CLAUDE.md` § Cross-folder rules)
- **Deploy order across stacks** — `docker restart classifier` after a `models.yaml` edit; recreate `muse-glimmer` (`make up vllm muse-glimmer`) before the classifier when the entry's image count or concurrency rises; drain the queue before a payload-schema change. (root `CLAUDE.md` § Cross-folder rules)
- **Swap-class services publish through the edge** — the classifier is recreate-class (startup requeues the running copy's jobs) but FRONTED by `ai/edge` on `PORT_CLASSIFIER` 8005 (no `ports:` of its own), so a restart answers `503` + `Retry-After`; no auth on the service — LAN access is `EDGE_ALLOWED_CIDRS`, the bearer check is LiteLLM's pass-through. (root `CLAUDE.md` § Deploys (`make up`))
- **LiteLLM pass-throughs carry the auth** — the service checks no token; `/v1/classifier` in `ai/litellm/litellm_config.yaml` `pass_through_endpoints` carries `auth: true` and its `methods` must include `PATCH` (for `PATCH /references/{id}`). A new route without `auth: true` is open to keyless callers (`ai/litellm/LITELLM.md § Pass-through auth`). (root `CLAUDE.md` § Cross-folder rules)

## Env vars

One line each; full detail in `.env.example` (Classifier block) and the `API.md` section named.

- `CLASSIFIER_MAX_CONCURRENT` (`4`) — jobs at once (worker tasks claiming from `jobs`); the job-level knob, not the model-load one; `1` = serial.
- `CLASSIFIER_MAX_UNITS_PER_JOB` (`6`) — units one job runs at once; matched to the default model's `max_concurrent`; lower it to stop a big job crowding others. (Renamed from `CLASSIFIER_MAX_CRITERIA_PER_JOB`.)
- `CLASSIFIER_OCR_WORKERS` (`4`) — OCR passes at once, all jobs; OCR is memoised per item by its settings.
- `CLASSIFIER_PROGRESS_FLUSH_INTERVAL_S` (`1`, floor `0.2`) — progress snapshot + `job_progress` write cadence; terminal state always written. [`API.md § Progress`](API.md#progress).
- `CLASSIFIER_DB_USER` / `_PASSWORD` / `_NAME` (`classifier` ×3) — dev defaults; override the password before exposing the DB. DSN quoted with `urllib.parse.quote`.
- `CLASSIFIER_TRINO_READER_PASSWORD` (unset) — set BEFORE the first start of an empty volume; empty skips the role (only the Trino catalog breaks); a real secret (`openssl rand -hex 32`).
- `PORT_CLASSIFIER` (`8005`, published by the edge) / `PORT_CLASSIFIER_DB` (`5439`, psql / DBeaver only) — `.env` PORT REGISTRY, Postgres group (root `CLAUDE.md` § Env vars and the PORT REGISTRY).
- `JOB_TTL_HOURS` (`24`) — terminal jobs and their artifacts expire; `pending` / `processing` are never swept.
- `CLASSIFIER_OCR_ENGINE` (`rapidocr`) — `none` disables OCR; `text` criteria on image-only pages then fail. Models are pre-warmed in the Dockerfile. [`API.md § OCR`](API.md#ocr).
- `CLASSIFIER_OCR_MIN_NATIVE_CHARS` (`20`) — below it `ocr: "auto"` treats a page as a scan.
- `CLASSIFIER_PDF_RENDER_DPI` (`150`) — PDF / SVG render density; pages are then shrunk to ≤1000 px, so higher mostly buys OCR accuracy.
- `CLASSIFIER_MIN_IMAGE_WIDTH` / `_HEIGHT` (`32` / `32`) — thumbnail-catcher only (was 100×100, which broke one-line crops).
- `CLASSIFIER_MAX_ITEMS` (`20`, inclusive) — items (pages) per request; 21 is a 400 naming each document's page count. [`API.md § Items and the cap`](API.md#items-and-the-cap).
- `CLASSIFIER_CRITERION_NAME_MAX_CHARS` (`200`) — 422 past it; the `llm` name is the prompt and is slugified into file names.
- `CLASSIFIER_TEXT_MIN_COUNT_CAP` (`1000`) — max `options.min_count`. The 500-char pattern cap is in `common.documents.textmatch`, not a knob.
- `CLASSIFIER_SVG_FETCH_IMAGES` (`false`) / `_MAX_IMAGES` (`10` distinct) / `_MAX_BYTES` (`5000000`) / `_TIMEOUT_S` (`10`, wall-clock) — opt-in SVG `<image>` inlining at submit. [`API.md § External references in SVG`](API.md#external-references-in-svg).
- `CLASSIFIER_ARTIFACT_DIR` (`/data/artifacts`) — per-job artifact dirs on the `classifier_data` volume. [`API.md § What a job writes, and what renders on first fetch`](API.md#what-a-job-writes-and-what-renders-on-first-fetch).
- `CLASSIFIER_ARTIFACT_SWEEP_INTERVAL_S` (`600`) — sweeper cadence (also at startup); deletes expired dirs AND job rows — what makes `JOB_TTL_HOURS` real.
- `CLASSIFIER_ARTIFACT_MAX_BYTES` (`50000000`) — byte cap PER ITEM. [`API.md § Disk and retention`](API.md#disk-and-retention).
- `CLASSIFIER_INLINE_REGIONS_MAX` (`50`) — regions per criterion inline in the result (`regions_truncated`); raising it slows `GET /jobs`.
- `CLASSIFIER_LLM_BBOX_MAX_ATTEMPTS` (`3`) — box-loop rounds and the CAP on `options.max_attempts` (over it is a 400); ≤ 3 calls per attempt.
- `CLASSIFIER_LLM_BBOX_VERIFY_PASS` (`7`) — score the isolated crop must earn.
- `CLASSIFIER_LLM_BBOX_MIN_AREA` / `_MAX_AREA` (`0.002` / `0.95`) — box area bounds; full-frame boxes are the commonest failure.
- `CLASSIFIER_LLM_BBOX_CROP_PAD` (`0.25`) — verify-crop padding (was 0.10; the refine pass draws tight boxes).
- `CLASSIFIER_LLM_BBOX_GRIDLINES` (`true`) / `_GRID_STEP` (`100`, a divisor of 1000, floor 10) — grid on the ask image.
- `CLASSIFIER_LLM_BBOX_REFINE` (`true`) / `_REFINE_ZOOM` (`2.5`) / `_REFINE_MIN_SPAN` (`0.2`) — the zoomed second ask.
- `CLASSIFIER_REFERENCE_DIR` (`/data/references`) — never under the artifact dir. [`API.md § References`](API.md#references).
- `CLASSIFIER_REFERENCE_MAX_COUNT` (`500`) — 409 past it; the only disk bound on references.
- `CLASSIFIER_REFERENCE_MAX_PER_REQUEST` / `_MAX_PER_CRITERION` (`10` / `3`) — explicit ids per `/assess`; examples (and extra scoring calls) per criterion.
- `CLASSIFIER_REFERENCE_AUTO_POOL_MAX` / `_AUTO_MIN_CONFIDENCE` (`20` / `60`) — `references: "auto"` pool and match floor; the selection runs on the JOB's model.
- `CLASSIFIER_REFERENCE_POSITION_MIN_IOU` / `_MAX_OFFSET` / `_CAP` (`0.3` / `0.15` / `5`) — opt-in position check; a miss caps, never raises; no model call.
- `CLASSIFIER_REFERENCE_DESCRIBE` / `_DESCRIPTION_MAX_CHARS` (`true` / `500`) — auto-describe a reference page; also the max caller description.
- `DETECTOR_URL` (empty = off) — `http://detector:8000` once `ai/detector` is up; empty refuses `detector` criteria at submit; set, a `cv` name with no OpenCV detector defaults to `fallback: "detector"`. Unreachable fails only the criteria that asked (`status: "error"`). [`API.md § The detector`](API.md#the-detector).
- `DETECTOR_MIN_SCORE` (`0.25`) — used twice on purpose: the detector `threshold` AND the PASS line for box-scored criteria (≥0.5 → 10, above the floor → 7, else 1).
- `DETECTOR_TIMEOUT_S` (`30`) — one `POST /detect`.
- `DETECTOR_MAX_LABELS_PER_CALL` (`16`) — keep ≤ the detector's `DETECTOR_MAX_LABELS` (32); more labels are split, not refused.
- Removed with no fallback (startup WARNs if set): `VISION_LLM_API`, `VISION_LLM_MODEL`, `CLASSIFIER_REASONING_STRENGTH`, `VISION_LLM_MAX_TOKENS`, `VISION_LLM_MAX_IMAGES_PER_PROMPT`, `CLASSIFIER_MAX_LLM_CALLS`, `CLASSIFIER_HTTP_TIMEOUT_S`, `CLASSIFIER_LLM_BBOX_MAX_TOKENS`, `CLASSIFIER_REFERENCE_SELECT_MAX_TOKENS`, `CLASSIFIER_TEXT_CHAR_BUDGET` — their job is `models.yaml` now.
- Compose-fixed, never `.env`: `CLASSIFIER_DB_HOST` (`classifier-db`), `CLASSIFIER_DATA_DIR` (`/data`), `PAYLOAD_DIR` (`/data/payloads`).
