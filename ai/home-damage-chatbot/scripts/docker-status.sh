#!/usr/bin/env bash
# Check status of Zeo Damage Chatbot Docker container
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$ROOT_DIR"

echo "=== Container Process Status ==="
docker compose ps

echo ""
echo "=== Application Health Endpoint ==="
curl -s http://localhost:8000/api/health || echo "Endpoint unreachable (container may be stopped)"
echo ""
