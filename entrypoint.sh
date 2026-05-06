#!/bin/bash
set -e

LLAMA_SERVER="/home/unsloth/.unsloth/llama.cpp/build/bin/llama-server"
LLAMA_DIR="/home/unsloth/.unsloth/llama.cpp"

# Check if llama-server exists and has CUDA support
if [ ! -f "$LLAMA_SERVER" ] || ! "$LLAMA_SERVER" --version 2>&1 | grep -q "CUDA"; then
    echo "==> Building llama.cpp with CUDA support..."
    
    rm -rf "$LLAMA_DIR"
    git clone https://github.com/ggerganov/llama.cpp "$LLAMA_DIR"
    
    cd "$LLAMA_DIR"
    cmake -B build \
        -DGGML_CUDA=ON \
        -DCMAKE_CUDA_ARCHITECTURES=89
    
    cmake --build build --config Release -j4
    
    echo "==> llama.cpp build complete"
fi

# Hand off to the original base image entrypoint
exec /usr/local/bin/entrypoint.sh "$@"