#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE_PATH="${1:-$SCRIPT_DIR/Neighborhood.jpeg}"
PROMPT="${2:-What is in this image?}"
BASE_URL="${3:-http://192.168.5.233:4001}"
MODEL="${4:-qwen2.5-vl}"
MAX_TOKENS="${5:-512}"
MAX_WIDTH=800
MAX_HEIGHT=600

# Load API key from .env
ENV_FILE="$SCRIPT_DIR/../../.env"
API_KEY=$(grep "^DEFAULT_LITELLM_MASTER_KEY=" "$ENV_FILE" | cut -d'=' -f2)
if [ -z "$API_KEY" ]; then
    echo "ERROR: DEFAULT_LITELLM_MASTER_KEY not found in .env" >&2
    exit 1
fi

# Resize image and encode as base64
B64=$(python3 - "$IMAGE_PATH" "$MAX_WIDTH" "$MAX_HEIGHT" <<'EOF'
import sys, base64, io
from PIL import Image

img = Image.open(sys.argv[1])
max_w, max_h = int(sys.argv[2]), int(sys.argv[3])
img.thumbnail((max_w, max_h), Image.LANCZOS)
buf = io.BytesIO()
img.save(buf, format='JPEG', quality=85)
print(base64.b64encode(buf.getvalue()).decode(), end='')
EOF
)

SIZE_KB=$(echo -n "$B64" | wc -c | awk '{printf "%.1f", $1 * 3 / 4 / 1024}')
echo "Sending ~${SIZE_KB} KB image to $MODEL..."

# Build and send request
TMP_FILE=$(mktemp /tmp/vl_test_XXXXXX.json)
trap "rm -f $TMP_FILE" EXIT

cat > "$TMP_FILE" <<JSON
{
  "model": "$MODEL",
  "messages": [{
    "role": "user",
    "content": [
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,$B64"}},
      {"type": "text", "text": "$PROMPT"}
    ]
  }],
  "max_tokens": $MAX_TOKENS
}
JSON

echo ""
curl -s -X POST "$BASE_URL/v1/chat/completions" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    --data-binary "@$TMP_FILE" \
    | python3 -c "import sys,json; r=json.load(sys.stdin); print(r['choices'][0]['message']['content'])"
