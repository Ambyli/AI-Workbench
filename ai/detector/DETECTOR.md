# Detector — open-vocabulary object detection

Text-prompted object detection as a service. POST an image and a list of
free-text labels; get boxes back with scores, in the image's **original**
pixel coordinates.

```bash
curl -s -X POST http://localhost:8021/detect \
  -F file=@unit-tests/classifier/Neighborhood.jpeg \
  -F 'labels=["tree", "house", "car", "swimming pool"]' | jq '.counts'
# → {"tree": 5, "house": 8, "car": 0, "swimming pool": 0}
```

"Open vocabulary" means there is no fixed class list and no fine-tuning step:
`swimming pool`, `roof vent`, and `solar panel` work exactly as well as `car`
does. That is the whole reason this container exists — the classifier's `cv`
criteria are limited to the handful of OpenCV detectors registered in
`ai/classifier/cv/`, and everything else used to cost a vision-LLM call and
come back with no geometry at all.

| | |
|---|---|
| Compose file | [`docker-compose.detector.yml`](docker-compose.detector.yml) |
| Image | built locally from [`Dockerfile.detector`](Dockerfile.detector) (build context = repo root) |
| Host port | `PORT_DETECTOR`, default **8021** |
| Network | `ai_shared` |
| Model | `DETECTOR_MODEL`, default `google/owlv2-base-patch16-ensemble` |
| GPU | `device_ids: ['2']` — **shared with `qwen3.8-solo`** |
| Postman | [`detector.postman_collection.json`](detector.postman_collection.json) |
| Metrics | `GET /metrics`, scraped by `prometheus` (see `ai/litellm/prometheus.yml`) |

---

## Endpoints

### `POST /detect`

Two spellings of the same request, chosen by `Content-Type`.

**Multipart** — what a human with curl or Postman has:

| Field | Required | Meaning |
|---|---|---|
| `file` | yes | The image (JPEG / PNG / anything Pillow opens) |
| `labels` | yes | A JSON array (`["tree","house"]`) **or** a comma list (`tree, house`) **or** one name |
| `threshold` | no | Confidence floor 0–1. Default `DETECTOR_DEFAULT_THRESHOLD` (0.25) |
| `max_per_label` | no | Keep at most this many boxes per label, best first |

**JSON** — what another service has:

```json
{
  "image": {"data": "<base64 or an http(s) URL>", "type": "base64"},
  "labels": ["tree", "swimming pool"],
  "threshold": 0.3,
  "max_per_label": 5
}
```

`type: "url"` is fetched **after** an SSRF check (`common.net.validate_url`):
this container sits on `ai_shared`, so an unchecked URL input would be a
request to proxy into the private network. A URL resolving to any RFC1918,
loopback, or link-local address is a 400.

**Response**

```json
{
  "model": "google/owlv2-base-patch16-ensemble",
  "family": "owl",
  "device": "cuda",
  "elapsed_ms": 184.2,
  "threshold": 0.25,
  "image": {"width": 7770, "height": 1999},
  "labels": ["tree", "house"],
  "counts": {"tree": 5, "house": 8},
  "by_label": {
    "house": [{"label": "house", "score": 0.6467, "box": [87.7, 6.5, 3574.8, 1903.4]}],
    "tree":  [{"label": "tree",  "score": 0.3190, "box": [3037.7, 352.2, 3877.9, 1863.6]}]
  },
  "detections": [ … the same boxes, flat, score order … ]
}
```

- `box` is `[x1, y1, x2, y2]`, top-left origin, **original image pixels**,
  clipped to the image. See [Coordinates](#coordinates).
- **Every requested label gets a key**, even with no hits. "Looked and found
  none" and "did not look" are different answers; a missing key makes them
  indistinguishable.
- `by_label` and `detections` are two views of the same boxes — the first is
  what a caller with one criterion per label wants, the second is what a
  caller drawing an overlay wants.
- `elapsed_ms` is inference only, not the round trip.

Errors: **400** bad labels/threshold, undecodable image, blocked URL, or a
Content-Type that is neither multipart nor JSON. **502** an image URL that
could not be fetched. **503** the model is not loaded — see `GET /health`.

### `GET /health`

```json
{"status": "ok", "model": "google/owlv2-base-patch16-ensemble", "family": "owl",
 "device": "cuda", "configured_device": "cuda", "loaded": true, "error": null,
 "default_threshold": 0.25, "max_image_side": 1024, "max_labels": 32}
```

Returns **503** with `"status": "degraded"` and the loader's error while the
weights are not resident. That is deliberate: a container that is up with a
failed model load is not healthy, and the Docker healthcheck should fail it
rather than route traffic to a service that can only answer 503. `device` is
what is actually in use, which is not always `configured_device` — see
[CPU fallback](#cpu-fallback).

### `POST /mcp` — the `detect_objects` tool

Registered with LiteLLM as `detector` (`ai/litellm/litellm_config.yaml` →
`mcp_servers`), so a model can call it as `detector.detect_objects`:

```
detect_objects(image, labels, threshold=None, max_per_label=None)
```

`image` is an http(s) URL or base64 bytes — **prefer a URL**, base64 of a
photo is a very large tool argument. The tool returns the same body as
`POST /detect` minus the flat `detections` list, which is redundant with
`by_label` and is the bulky half.

### Through LiteLLM

`/v1/detector/*` is a pass-through (`include_subpath: true`, GET + POST,
`forward_headers: true`), so everything above is also reachable as
`{{litellm}}/v1/detector/detect` with a LiteLLM virtual key. That is what the
Postman collection uses.

---

## Coordinates

Boxes come back in the pixel space of the image **you sent**, after EXIF
transpose. Two resizes happen in between and neither leaks out:

1. This service resizes the long side down to `DETECTOR_MAX_IMAGE_SIDE`
   (1024) before inference. OWLv2 pads to a square and works on its own 960-px
   grid anyway, so feeding it a 12-megapixel photo costs decode and transfer
   time and buys nothing. Images already small enough are never upscaled.
2. OWLv2's processor pads the image to a square on the bottom and right.
   Post-processing therefore targets the **padded square**, not the image —
   with `target_sizes` set to the unpadded size every box comes back stretched
   by `W/max(W,H)` in x and `H/max(W,H)` in y, which on a portrait photo means
   boxes that are systematically too wide and too tall. Since the padding is
   bottom-right, the top-left origin is shared and the boxes only need
   clipping.

`detectors/base.py` owns both halves (`prepare` / `to_original`), so a new
family only has to return boxes in the prepared frame. `scale` is measured
from the *rounded* resize result rather than the ratio requested — a 1-pixel
rounding error compounds into a visibly offset box on a 4000-px photo.

The classifier sends its ≤1000-px **working** page image (the same pixels its
OpenCV detectors and the vision prompt see) and rescales the returned boxes
into original page pixels itself, through the same
`common.vision.rescale_region` every other region source uses.

---

## Model

Default: **OWLv2 base / ensemble** (`google/owlv2-base-patch16-ensemble`) —
Apache-2.0, ~600 MB, decent on everyday objects. Verified against
`transformers` 5.17.0 + `torch` 2.14.0.

Measured on `unit-tests/classifier/Neighborhood.jpeg` (a 7770×1999 street
panorama) with `["tree","house","car","swimming pool"]` at threshold 0.2: ~9
`house` boxes (best `0.6467`, at `[87.7, 6.5, 3574.8, 1903.4]`) and ~5 `tree`
boxes (best `0.319`), and **nothing at all** for `car` or `swimming pool` —
which is the assertion worth making, because a detector that finds everything
is useless. Scores are **not calibrated probabilities**: 0.65 is a confident
house, and the same number on another label in another photo means something
else. Treat the threshold as a knob to turn, not a percentage.

It also answers honestly on inputs with nothing in them: the synthetic
gradient fixture `unit-tests/classifier/regions/greenery_and_sky.png` returns
zero detections for all four labels, which is correct — there are no objects
in it, only coloured regions. Use the OpenCV mask detectors for that kind of
question; use this for things with names.

### Swapping the model

`DETECTOR_MODEL` is a `.env` variable, and `detectors/__init__.py` picks the
implementation **family** from the id, so a swap is a recreate:

| Id | Family | Notes |
|---|---|---|
| `google/owlv2-base-patch16-ensemble` | `owl` | The default |
| `google/owlv2-large-patch14-ensemble` | `owl` | Better, ~3x the latency and ~4x the VRAM — check it still fits beside `qwen3.8-solo` before committing |
| `google/owlvit-base-patch32` | `owl` | The older, faster, less accurate generation |
| `IDEA-Research/grounding-dino-*` | — | **Recognised and refused** with a message naming the class to write. Not implemented; see below |

A changed id downloads into the `detector_data` volume on first request, so
the first call after the recreate is slow. To bake it into the image instead,
pass the same value as the compose build arg and rebuild:

```bash
make build detector   # or: docker compose -f ai/detector/docker-compose.detector.yml build
make up detector
```

An id matching no family fails the load with a `ValueError` naming the known
families; the container stays up and `GET /health` says so.

### Adding Grounding DINO (or anything else)

Three steps, no edits outside `ai/detector/detectors/`:

1. Write `detectors/<family>.py` with a subclass of
   `base.OpenVocabularyDetector` implementing `load()` and
   `_detect_prepared()` — boxes in **prepared** pixels; the base class handles
   the rescale, the clipping, and the sort.
2. Add a `(patterns, loader)` row to `_FAMILIES` in `detectors/__init__.py`.
3. Add the dependency to `pyproject.toml` if it needs one, and a row to the
   table above.

---

## Image and CUDA

The base image is plain `python:3.11-slim`, **not** a CUDA base. torch's Linux
wheels on PyPI already *are* the CUDA build — 2.14.0 declares
`nvidia-cudnn-cu13` / `nvidia-nccl-cu13` and ships the CUDA 13 userspace
libraries inside the wheel — so a CUDA base image would add a second, unused
copy of them. What the **host** must provide is the driver and the NVIDIA
container toolkit:

| CUDA major | Driver floor | How to get it |
|---|---|---|
| 13 (default) | **R580+** | the `torch>=2.14` pin in `pyproject.toml` |
| 12 | R525+ | pin `torch==2.9.1` and add `--extra-index-url https://download.pytorch.org/whl/cu128` to the `uv sync` in the Dockerfile |

Same constraint the llama.cpp stack documents for `LLAMA_UNSLOTH_VARIANT`:
this box runs `cuda13-portable`, so it is already on an R580+ driver and the
default pin is right. Expect a **~7 GB image** either way — most of it is the
bundled CUDA libraries. `DETECTOR_DEVICE=cpu` needs none of this and runs on
the identical image.

Two non-obvious pins in `pyproject.toml`:

- **`scipy`** is not a declared `transformers` dependency, but
  `Owlv2ImageProcessorPil.resize()` calls `scipy.ndimage` for its antialiased
  downsample. Without it the container starts fine and the **first `/detect`
  call** raises `ImportError`. Pinned on purpose.
- **`transformers>=5.17`** — 5.x renamed
  `Owlv2Processor.post_process_object_detection` to
  `post_process_grounded_object_detection` (the HF model card still shows the
  old name). `detectors/owlv2.py` resolves whichever exists, so a 4.x pin also
  works; 5.17 is what this was built and verified against.

### Weights are baked in, and the volume is seeded from the image

The Dockerfile downloads the default checkpoint into `/opt/hf` at build time —
and asserts that it actually produced weight files, so a broken warm-up fails
the build rather than shipping as a runtime surprise. The compose file then
mounts the `detector_data` named volume at that same path. Docker **seeds an
empty named volume from the image**, so:

- a cold container starts with the weights already present and never reaches
  HuggingFace;
- a later `DETECTOR_MODEL` change downloads into the volume and **persists**
  across recreates.

Same reasoning as the classifier's RapidOCR warm-up. Deleting `detector_data`
returns you to the baked-in default on the next start.

---

## GPU sharing, and the CPU fallback

`device_ids: ['2']` — GPU 2, the same card `qwen3.8-solo` runs on at
`--gpu-memory-utilization 0.90`. OWLv2 base in fp16 is ~350 MB of weights plus
activations, which fits the headroom that leaves. `count: all` would put it on
the `muse-glimmer` pair instead and fight the model the classifier depends on.

Two things keep the footprint bounded:

- **fp16 on GPU** (`model.half()`), which halves weights and activations. On
  CPU the weights stay fp32 — most CPU kernels upcast anyway, so fp16 there is
  a pessimisation.
- **One inference at a time**, enforced by an `asyncio.Lock`. There is no
  batching to gain from concurrency here; two interleaved requests would just
  double each other's latency *and* double peak VRAM, which is exactly what
  must not happen beside `qwen3.8-solo`. `/health` and `/metrics` stay
  responsive because the forward pass runs in a worker thread.

### CPU fallback

```bash
# .env
DETECTOR_DEVICE=cpu
```

then `make up detector`. Same image, no rebuild. Expect **seconds per image**
instead of a few hundred milliseconds — measured 10–13 s for the 7770×1999
panorama on a laptop CPU, and ~12 s for a 900×700 image, which is the tell:
the cost is OWLv2's fixed 960-px grid and the label count, not the input size.
A server CPU is faster but the order of magnitude holds. That is usable for
the classifier's workload — a job makes one detector call per page — and is
the documented answer if the GPU-2 pairing proves fragile.

`DETECTOR_DEVICE=cuda` on a host where torch sees no CUDA device **falls back
to CPU with a warning** rather than refusing to start: a detector answering
slowly is more useful than one that is down, and `GET /health` reports
`device: "cpu", configured_device: "cuda"` so the mismatch is visible.

---

## Security posture

- **No auth on the service.** Same as the classifier: the LiteLLM pass-through
  does the bearer check, and direct access on `PORT_DETECTOR` is
  unauthenticated. Do not publish that port on an untrusted network.
- **URL inputs are SSRF-checked** with `common.net.validate_url` before any
  fetch — every resolved address must clear the RFC1918 / loopback /
  link-local blocklist, not just the first one. This is a check, not a tunnel:
  a hostile DNS server can still answer differently between the resolve and
  the connect. A deployment that must be protected against that belongs behind
  an egress proxy with an allowlist (the pattern `ai/sandbox` uses).
- **`labels` is capped** at `DETECTOR_MAX_LABELS` (32). OWLv2 embeds every
  label as its own text query, so cost is linear in the count and an unbounded
  list is a denial-of-service on a shared GPU.
- **Images below 32 px on a side are refused.** Below that the model's answers
  are noise, and running them wastes a GPU slot.
- **No `docker.sock`, no privileged flags, no writable mounts** except the
  weight cache.

---

## Verifying a change

The model-free parts — request parsing, the coordinate contract, response
shaping, the family factory — are unit-tested without torch, transformers, or
a GPU:

```bash
UV_LINK_MODE=copy uv run --package classifier pytest unit-tests/detector -q
```

(any environment with pytest, pydantic and Pillow works; the classifier's
workspace venv has all three.)

The model path itself is deliberately **not** unit-tested — mocking a
transformer's output shape tests the mock. Verify it by running the real
thing against the repo fixtures:

```bash
curl -s -X POST http://localhost:8021/detect \
  -F file=@unit-tests/classifier/Neighborhood.jpeg \
  -F 'labels=["tree","house","car","swimming pool"]' \
  -F threshold=0.2 | jq '.counts, .by_label.house[0]'
```

Sanity checks worth making on any change to `detectors/`:

- every box is inside the image (`0 ≤ x1 < x2 ≤ width`, same for y);
- halving `DETECTOR_MAX_IMAGE_SIDE` moves the boxes by a percent or two, not
  by a factor — if it moves them proportionally, the rescale is wrong;
- a portrait and a landscape crop of the same scene give boxes in the same
  places — if the portrait one is stretched, the square-padding handling
  broke.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `503` from `/detect`, `/health` says `degraded` | The model did not load. `error` in the health body has the reason: a bad `DETECTOR_MODEL`, a Grounding DINO id (not implemented), no disk, or no network on a cold volume with a changed model. |
| Container healthy, first `/detect` raises `ImportError: scipy` | `scipy` went missing from the image. It is a real dependency of OWLv2's PIL image processor — see [Image and CUDA](#image-and-cuda). |
| `/health` says `device: "cpu"` but `.env` says `cuda` | torch sees no CUDA device. Check `deploy.resources.reservations.devices` in the compose file and the host's nvidia-container-toolkit. |
| Every label returns zero boxes | Either the image genuinely has none of them (try `["person","car","tree"]` on a street photo to confirm the model works), or the threshold is too high. OWLv2 scores are low in absolute terms; 0.2–0.3 is a normal working range. |
| Spurious boxes covering the whole image | Threshold too low. Also true of OWLv2 generally — a full-frame box is usually the model saying "I have nothing". |
| OOM on GPU 2 | `qwen3.8-solo` grew, or `DETECTOR_MODEL` was pointed at the `large` checkpoint. Drop back to `base`, or set `DETECTOR_DEVICE=cpu`. |
| Classifier jobs show `artifacts.notes` about the detector | Expected degradation, not a failure — the classifier never fails a job because an enrichment did. The note says whether it was unconfigured or unreachable. |
