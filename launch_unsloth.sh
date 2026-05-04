#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$(pwd)/work"

docker run -d \
  -e JUPYTER_PASSWORD="mypassword" \
  -p 8888:8888 \
  -p 8000:8000 \
  -p 2222:22 \
  -v "$(pwd)/work:/workspace/work" \
  --gpus all \
  unsloth/unsloth
