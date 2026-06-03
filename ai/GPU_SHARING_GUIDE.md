# GPU Sharing Guide

Options for running multiple GPU workloads concurrently on the same physical GPU in this stack (vLLM, Kokoro, etc.).

---

## CUDA MPS (Multi-Process Service)

Shares a single CUDA context across processes, reducing per-process overhead and improving GPU utilization when workloads overlap. Works on **all NVIDIA GPUs** (consumer and datacenter).

### Setup (host, run once)

```bash
# Set exclusive process mode (prevents other apps from stealing the GPU)
nvidia-smi -i 0 -c EXCLUSIVE_PROCESS

# Start the MPS control daemon
nvidia-cuda-mps-control -d
```

### Docker configuration

Mount the MPS Unix socket into each container that needs GPU access:

```yaml
services:
  vllm-llama:
    # ... existing config ...
    volumes:
      - /tmp/nvidia-mps:/tmp/nvidia-mps
    environment:
      - NVIDIA_MPS=1

  vllm-qwen:
    # ... existing config ...
    volumes:
      - /tmp/nvidia-mps:/tmp/nvidia-mps
    environment:
      - NVIDIA_MPS=1
```

### Trade-offs

| Pros | Cons |
|---|---|
| Works on any NVIDIA GPU | Shared context means OOM in one process can crash all of them |
| Low overhead | No memory isolation — processes still compete for the same VRAM pool |
| Simple to set up | Debugging OOM issues is harder (stack traces point to shared context) |

---

## NVIDIA MIG (Multi-Instance GPU)

Partitions a GPU into fully isolated slices at the hardware level. Each slice gets dedicated memory and compute units. **Requires A100/H100/H100-Pcie/H200** and datacenter drivers.

### Check MIG support

Before setting up, verify your hardware and driver support MIG:

```bash
# 1. Confirm GPU model (A100/H100/H200 only)
nvidia-smi -L

# 2. Check if driver supports MIG
nvidia-smi migCap

# 3. List available MIG profiles (errors out on unsupported hardware)
migctl device list

# 4. Check current MIG mode
nvidia-smi -i 0 -query=gpu.mig.current.state

# If all four commands succeed, MIG is supported on this system.
```

### Setup (host)

```bash
# Set GPU mode to MIG (requires reboot on some systems)
nvidia-smi -i 0 -g 0 -dm 1

# Configure a partition — e.g., two equal slices on GPU 0
nvidia-smi gpu0 -ig 0 -cgi 0 -pm 0   # create GPU-instance 0, 1 slice
nvidia-smi gpu0 -dg 0 -ci 0 -smi 0   # create SM-instance 0 under GPU-instance 0
```

The exact `nvidia-smi` flags depend on your driver version. The `nvidia-ml-py` library or `migctl` provides a cleaner interface:

```bash
# List available MIG profiles
migctl profile list

# Create a partition (two equal halves)
migctl device-partition -g 0 -s 2

# Verify
nvidia-smi -i 0 -L
```

### Docker configuration

Reference the MIG device by its GPU index instead of `count: all`:

```yaml
services:
  vllm-llama:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              devices: ["0"]        # MIG slice 0 on GPU 0
              capabilities: [gpu]

  vllm-qwen:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              devices: ["1"]        # MIG slice 1 on GPU 0
              capabilities: [gpu]
```

### Trade-offs

| Pros | Cons |
|---|---|
| Full memory isolation — OOM in one slice doesn't affect the other | Only works on A100/H100/H200 |
| Dedicated compute units per slice | Reduces max VRAM per slice (e.g., 80 GB A100 → two × 40 GB slices) |
| Cleaner failure boundaries | Requires datacenter GPU + driver support |
| Better for multi-tenant setups | Partitioning is relatively static — harder to resize on the fly |

---

## Which to choose?

| Scenario | Recommendation |
|---|---|
| Consumer GPU (RTX 3090/4090, etc.) | **MPS** — MIG isn't available |
| A100/H100 with mixed workloads (vLLM + Kokoro) | **MIG** if you need isolation; **MPS** if you want max shared VRAM |
| Same model type, similar memory profiles | **MPS** — simpler, less overhead |
| Different models with very different memory needs | **MIG** — prevents a memory-hungry workload from starving the other |
| Single GPU, two vLLM instances at 50% each | Either works. MPS is lower-friction to start with |

---

## Current stack notes

Your [docker-compose.vllm.yml](docker-compose.vllm.yml) uses `count: all` with `--gpu-memory-utilization 0.5` on each service — this relies on the CUDA driver to time-share the GPU, which works but isn't optimal. Both services can see the full VRAM pool and may OOM if their combined needs exceed physical memory.

Neither MPS nor MIG is currently configured. Adding either would make the two vLLM instances more predictable when running simultaneously.
