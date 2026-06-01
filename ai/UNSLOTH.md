# Unsloth Docker — CUDA build

The Unsloth Docker image ships a prebuilt `llama.cpp` binary that may lack CUDA support, causing inference to fall back to CPU. The `Dockerfile` and `entrypoint.sh` in this repo automate the fix — the CUDA-enabled binary is compiled at container startup and persisted in a named volume so subsequent restarts skip the build.

## How it works

`docker-compose.unsloth.yml` builds a custom image from `Dockerfile.unsloth` instead of pulling `unsloth/unsloth:latest` directly. On first start, `entrypoint.sh`:

1. Removes the prebuilt CPU-only `llama.cpp`
2. Clones `llama.cpp` from source
3. Compiles it with `-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=89` (RTX 4080 / Ada Lovelace)
4. Hands off to the original Unsloth entrypoint

The compiled binary lives in the `unsloth_data` named volume (`/home/unsloth/.unsloth`), so the build only runs once — subsequent starts skip straight to launch.

## Linux prerequisite — NVIDIA Container Toolkit

The container requires the NVIDIA Container Toolkit to expose the GPU to Docker. Before running any `make` or `docker compose` commands on Linux, follow the official install guide:

https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/1.19.0/install-guide.html

After installing, restart Docker (`sudo systemctl restart docker`) before proceeding.

## Starting the container

```bash
docker compose -f ai/docker-compose.unsloth.yml up --build   # first run — builds the image and compiles llama.cpp
docker compose -f ai/docker-compose.unsloth.yml up           # subsequent runs — reuses the volume, skips compile
```

Or use the Makefile helpers:

| Command | Effect |
|---|---|
| `make up` | Start the container in the background (`-d`), removing orphaned containers |
| `make down` | Stop the container |
| `make clean` | Stop and delete the container **and the named volume** (forces a full rebuild on next `make up`) |
| `make logs` | Tail container logs — useful for watching the llama.cpp compile progress on first start |

## GPU compute capability

`89` targets Ada Lovelace (RTX 4080 / 4090). Adjust `-DCMAKE_CUDA_ARCHITECTURES` in `Dockerfile.unsloth` for other GPUs. Full list: https://developer.nvidia.com/cuda-gpus

| Architecture | Value | Example GPUs |
|---|---|---|
| Ada Lovelace | `89` | RTX 4080, 4090 |
| Ampere | `86` | RTX 3080, 3090 |
| Turing | `75` | RTX 2080 |

## Verifying GPU is active

```bash
docker compose -f ai/docker-compose.unsloth.yml exec unsloth \
  /home/unsloth/.unsloth/llama.cpp/build/bin/llama-server --version 2>&1
```

If GPU layers are still not loading, confirm the container has access to the NVIDIA runtime (`runtime: nvidia` in `docker/docker-compose.unsloth.yml`) and that the host has the NVIDIA Container Toolkit installed.
