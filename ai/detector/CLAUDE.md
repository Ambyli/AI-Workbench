# ai/detector

Open-vocabulary object detector (OWLv2): an image plus free-text labels in, boxes in ORIGINAL image pixels out. The classifier's source for arbitrary "has X" criteria, and an MCP tool (`detect_objects`). Guide: [`DETECTOR.md`](DETECTOR.md). Compose: `docker-compose.detector.yml` (one recreate-class service, `detector`). Tests: [`DETECTOR.md § Verifying a change`](DETECTOR.md#verifying-a-change).

## Rules

- **Weights are baked into the image** (`/opt/hf`, the build asserts weight files exist) and the `detector_data` volume is seeded from it, so a cold container never reaches HuggingFace. A changed `DETECTOR_MODEL` downloads into the volume on first request; pass the same value as the compose build arg and rebuild to bake it in. Deleting `detector_data` returns to the baked default. [`DETECTOR.md § Weights are baked in, and the volume is seeded from the image`](DETECTOR.md#weights-are-baked-in-and-the-volume-is-seeded-from-the-image).
- **`detectors/__init__.py` picks the FAMILY from the model id**: any OWL id (`owlv2` / `owlvit`) is a drop-in; a Grounding DINO id is recognised and REFUSED (not implemented). A new family is a `detectors/<family>.py` subclass of `base.OpenVocabularyDetector` + a `_FAMILIES` row — no edits outside `detectors/`. [`DETECTOR.md § Adding Grounding DINO (or anything else)`](DETECTOR.md#adding-grounding-dino-or-anything-else).
- **Coordinates:** `detectors/base.py` owns `prepare` / `to_original`; post-processing targets the PADDED square (OWLv2 pads bottom-right), never the unpadded size — otherwise boxes stretch on portrait photos. `scale` comes from the rounded resize result. [`DETECTOR.md § Coordinates`](DETECTOR.md#coordinates).
- **Two non-obvious pins in `pyproject.toml`:** `scipy` (OWLv2's PIL processor needs it; without it the FIRST `/detect` raises `ImportError`) and `transformers>=5.17` (5.x renamed `post_process_object_detection` → `post_process_grounded_object_detection`; `detectors/owlv2.py` resolves whichever exists).
- **One inference at a time** (`asyncio.Lock`, forward pass in a worker thread) and fp16 on GPU — peak VRAM must stay bounded beside `qwen3.8-solo`. Do not add concurrency. On CPU the weights stay fp32.
- **`DETECTOR_DEVICE=cuda` with no visible GPU falls back to cpu with a warning** (`/health` shows `device` vs `configured_device`); it never refuses to start.
- **No auth on the service.** The LiteLLM `/v1/detector` pass-through does the bearer check; never publish `PORT_DETECTOR` on an untrusted network. URL inputs go through `common.net.validate_url` (every resolved address); `labels` capped at `DETECTOR_MAX_LABELS`; images under 32 px refused; no `docker.sock`. [`DETECTOR.md § Security posture`](DETECTOR.md#security-posture).
- **The model path is not unit-tested on purpose** (mocking the transformer tests the mock) — verify a `detectors/` change against `unit-tests/classifier/Neighborhood.jpeg` per `DETECTOR.md § Verifying a change`.
- **Run `unit-tests/detector` in its own pytest process**, never with `unit-tests/classifier` (both ship a top-level `config.py`).
- **An API change updates `detector.postman_collection.json` and `DETECTOR.md`** (root `CLAUDE.md` § Postman collections).

## Cross-folder rules

Also in the root `CLAUDE.md` § Cross-folder rules — read the named entry there for the full picture.

- **GPU sharing and driver floor** — pinned to `device_ids: ['2']`, SHARED with `qwen3.8-solo` (`ai/vllm`, `--gpu-memory-utilization 0.90`); OWLv2 base fp16 fits the headroom. `count: all` would land on the `muse-glimmer` pair — never. The `large` checkpoint (~4× VRAM) must be checked against that headroom first; `DETECTOR_DEVICE=cpu` is the fallback (seconds per image, same image). torch's PyPI Linux wheel is the CUDA 13 build → host driver R580+ (same floor as `LLAMA_UNSLOTH_VARIANT=cuda13-portable`); CUDA 12 / R525+ means `torch==2.9.1` + the cu128 index. (root `CLAUDE.md` § Cross-folder rules)
- **Swap-class services publish through the edge** — the detector is recreate-class (GPU) but FRONTED by `ai/edge` on `PORT_DETECTOR` 8021 with no `ports:` of its own: the edge answers `503` + `Retry-After` while it restarts or loads its model, and admits only `EDGE_ALLOWED_CIDRS`. (root `CLAUDE.md` § Deploys (`make up`))
- **LiteLLM pass-throughs carry the auth** — `/v1/detector` in `ai/litellm/litellm_config.yaml` `pass_through_endpoints` carries `auth: true` because this service checks no token; a route without it is open to keyless callers (`ai/litellm/LITELLM.md § Pass-through auth`). (root `CLAUDE.md` § Cross-folder rules)
- **Smaller couplings** — the classifier's `DETECTOR_MAX_LABELS_PER_CALL` (16) must stay ≤ `DETECTOR_MAX_LABELS` (32); its `DETECTOR_URL` turns the feature on (`ai/classifier/CLAUDE.md`). (root `CLAUDE.md` § Cross-folder rules)
- **Ports** — `PORT_DETECTOR` 8021 lives in the `.env` PORT REGISTRY (root `CLAUDE.md` § Env vars and the PORT REGISTRY).

## Env vars

One line each; full detail in `.env.example` (Detector block) and `DETECTOR.md`.

- `DETECTOR_MODEL` (`google/owlv2-base-patch16-ensemble`) — HF id, baked in at build; a change is a recreate and downloads into the volume. [`DETECTOR.md § Swapping the model`](DETECTOR.md#swapping-the-model).
- `DETECTOR_DEVICE` (`cuda`) — `cuda` | `cpu`; GPU 2 shared with `qwen3.8-solo`. [`DETECTOR.md § GPU sharing, and the CPU fallback`](DETECTOR.md#gpu-sharing-and-the-cpu-fallback).
- `DETECTOR_DEFAULT_THRESHOLD` (`0.25`) — floor when a request sends no `threshold`; OWLv2 scores are not calibrated (below ~0.1 every label matches something).
- `DETECTOR_MAX_IMAGE_SIDE` (`1024`) — long side before inference; boxes are rescaled back to original pixels, so callers never see it.
- `DETECTOR_MAX_LABELS` (`32`) — labels per `/detect`; cost is linear, an unbounded list is a DoS on a shared GPU.
- `PORT_DETECTOR` (`8021`) — host port, published by `ai/edge`; no auth behind it.
- Classifier-side detector vars (`DETECTOR_URL`, `DETECTOR_MIN_SCORE`, `DETECTOR_TIMEOUT_S`, `DETECTOR_MAX_LABELS_PER_CALL`) are read by the classifier, not here — see `ai/classifier/CLAUDE.md`.
