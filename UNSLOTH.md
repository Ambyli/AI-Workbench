# Unsloth Docker

Runs the [unsloth/unsloth](https://hub.docker.com/r/unsloth/unsloth) container with GPU access and a Jupyter notebook server.

Files saved to `work/` persist between sessions. Everything else (installed packages, files outside `work/`) is reset when you run `make down` followed by `make run`.

## Ports

| Port | Service |
|------|---------|
| 8888 | Jupyter notebook |
| 8000 | General use (e.g. serving models) |
| 2222 | SSH |

## First-time setup

```bash
make run
```

This creates the container and saves its ID to `.container_id`. Open Jupyter at `http://localhost:8888` and use password `mypassword`.

## Commands

| Command | Description |
|---------|-------------|
| `make run` | Create and start a fresh container. Resets all in-container state. |
| `make start` | Resume a previously stopped container, preserving installed packages and in-container files. |
| `make stop` | Pause the running container without destroying it. |
| `make down` | Stop and permanently remove the container. |
| `make logs` | Tail the container logs. |

## Persistent storage

The `work/` directory is mounted inside the container at `/workspace/work`. Save notebooks and datasets here to keep them across sessions.

Anything installed with `pip` or `apt` inside the container is **not** persistent across `make down` + `make run` cycles. Use `make stop` / `make start` to preserve in-container state between sessions without rebuilding.
