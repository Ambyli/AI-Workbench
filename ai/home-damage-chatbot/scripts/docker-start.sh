#!/usr/bin/env bash
# Start Zeo Damage Chatbot Docker container
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$ROOT_DIR"

echo "=== Starting Zeo Energy Service Chatbot Container ==="
if [ -f .env ]; then
    echo "Using environment configuration from .env"
else
    echo "No .env found; using .env.example defaults"
    cp .env.example .env
fi

docker compose up -d --build chatbot

echo ""
echo "Chatbot service is starting up..."
echo "Waiting for healthcheck..."
for i in {1..30}; do
    if curl -s http://localhost:8000/api/health > /dev/null 2>&1; then
        echo "✓ Chatbot is healthy and listening on http://localhost:8000"
        exit 0
    fi
    sleep 1
done

echo "Warning: Chatbot is taking longer than usual to become healthy."
echo "Check container logs with: docker compose logs -f chatbot"
