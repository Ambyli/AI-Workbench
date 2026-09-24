#!/usr/bin/env bash
# Stop Zeo Damage Chatbot Docker container
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$ROOT_DIR"

echo "=== Stopping Zeo Energy Service Chatbot Container ==="
docker compose down

echo "✓ Chatbot container stopped."
