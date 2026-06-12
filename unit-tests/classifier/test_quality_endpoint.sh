#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE_PATH="${1:-$SCRIPT_DIR/Neighborhood.jpeg}"
_DEFAULT_CRITERIA='[{"name":"document legibility","type":"quality"},{"name":"image sharpness","type":"quality"},{"name":"proper exposure","type":"quality"},{"name":"absence of artifacts","type":"quality"}]'
CRITERIA="${2:-$_DEFAULT_CRITERIA}"
BASE_URL="${3:-http://192.168.5.233:4001}"
MAX_WAIT="${4:-300}"
POLL_INTERVAL="${5:-3}"

# Load API key from .env
ENV_FILE="$SCRIPT_DIR/../../.env"
API_KEY=$(grep "^DEFAULT_LITELLM_MASTER_KEY=" "$ENV_FILE" | cut -d'=' -f2)
if [ -z "$API_KEY" ]; then
    echo "ERROR: DEFAULT_LITELLM_MASTER_KEY not found in .env" >&2
    exit 1
fi

# Submit job
SIZE_KB=$(du -k "$IMAGE_PATH" | cut -f1)
echo "Submitting ${SIZE_KB} KB image to classifier..."

SUBMIT_RESP=$(curl -s -X POST "$BASE_URL/v1/classifier/assess" \
    -H "Authorization: Bearer $API_KEY" \
    -F "image=@$IMAGE_PATH" \
    -F "criteria=$CRITERIA")

JOB_ID=$(echo "$SUBMIT_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])" 2>/dev/null)
if [ -z "$JOB_ID" ]; then
    echo "ERROR: Failed to get job_id from response: $SUBMIT_RESP" >&2
    exit 1
fi

echo "Job submitted: $JOB_ID"

# Poll until complete or failed
ELAPSED=0
FINAL_STATUS=""

while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    sleep "$POLL_INTERVAL"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))

    STATUS_RESP=$(curl -s "$BASE_URL/v1/classifier/jobs/$JOB_ID" \
        -H "Authorization: Bearer $API_KEY")

    STATUS=$(echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])" 2>/dev/null)

    echo "  [${ELAPSED}s] $STATUS"

    if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then
        FINAL_STATUS="$STATUS"
        break
    fi
done

echo ""

if [ -z "$FINAL_STATUS" ]; then
    echo "WARNING: job did not complete within ${MAX_WAIT}s (last status: $STATUS)" >&2
    exit 1
fi

if [ "$FINAL_STATUS" = "completed" ]; then
    echo "Result:"
    echo "$STATUS_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get('result', d), indent=2))"
else
    echo "Job failed:" >&2
    echo "$STATUS_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error','unknown error'))" >&2
    exit 1
fi
