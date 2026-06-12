#!/usr/bin/env python3
"""
Test the classifier /assess endpoint.

Submits an image as a job and polls until complete, then prints the result.

Usage:
    python test_quality_endpoint.py
    python test_quality_endpoint.py --image path/to/image.jpg
    python test_quality_endpoint.py --criteria '[{"name":"sharpness","type":"quality"},{"name":"has solar panels","type":"feature"}]'
    python test_quality_endpoint.py --base-url http://192.168.5.233:4001 --max-wait 120

Requires: pip install requests
"""

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests is required: pip install requests")

SCRIPT_DIR = Path(__file__).parent

DEFAULT_CRITERIA = [
    {"name": "document legibility", "type": "quality"},
    {"name": "image sharpness",     "type": "quality"},
    {"name": "proper exposure",     "type": "quality"},
    {"name": "absence of artifacts","type": "quality"},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_api_key() -> str:
    env_file = (SCRIPT_DIR / "../../.env").resolve()
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("DEFAULT_LITELLM_MASTER_KEY="):
            value = line.split("=", 1)[1].strip()
            # Strip inline comments (e.g. key=sk-1234  # comment)
            if " #" in value:
                value = value[:value.index(" #")].strip()
            return value
    sys.exit("ERROR: DEFAULT_LITELLM_MASTER_KEY not found in .env")


def submit_job(base_url: str, api_key: str, image_path: Path, criteria: list) -> str:
    url = f"{base_url}/v1/classifier/assess"
    print(f"  POST {url}")
    with image_path.open("rb") as fh:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"image": (image_path.name, fh, "image/jpeg")},
            data={"criteria": json.dumps(criteria)},
            timeout=30,
        )
    if not resp.ok:
        sys.exit(f"Submit failed ({resp.status_code}): {resp.text}")
    return resp.json()["job_id"]


def poll_job(
    base_url: str,
    api_key: str,
    job_id: str,
    max_wait: int,
    poll_interval: int,
) -> dict:
    url = f"{base_url}/v1/classifier/jobs/{job_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    elapsed = 0

    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval

        resp = requests.get(url, headers=headers, timeout=10)
        if not resp.ok:
            sys.exit(f"Poll failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        status = data["status"]
        print(f"  [{elapsed:>4}s] {status}", flush=True)

        if status in ("completed", "failed"):
            return data

    sys.exit(f"Timed out after {max_wait}s — last status: {status}")


def print_result(job: dict) -> None:
    if job["status"] == "completed":
        result = job.get("result", {})
        assessment = result.get("llm_assessment", {})
        cv = result.get("cv_pre_checks", {})

        print(f"\n{'-' * 60}")
        print(f"  Combined verdict : {result.get('combined_verdict', 'n/a')}")
        print(f"  CV sharpness     : {cv.get('sharpness', {}).get('verdict', 'n/a')} "
              f"(score {cv.get('sharpness', {}).get('score', '-')})")
        print(f"  CV exposure      : {cv.get('exposure', {}).get('verdict', 'n/a')} "
              f"(score {cv.get('exposure', {}).get('score', '-')})")
        print(f"  LLM overall      : {assessment.get('overall_verdict', 'n/a')} "
              f"(score {assessment.get('overall_score', '-')})")
        print(f"{'-' * 60}")

        per = assessment.get("per_criterion_scores", {})
        if per:
            print("\n  Per-criterion scores:")
            for name, val in per.items():
                conf = f"  confidence={val.get('confidence', '?')}" if isinstance(val, dict) else ""
                print(f"    {name:<35} {val.get('verdict', '?'):<10} "
                      f"score={val.get('score', '?')}{conf}")
                reason = val.get("reason", "")
                if reason:
                    print(f"      {reason}")

        print("\n  Full JSON:")
        print(json.dumps(result, indent=2))
    else:
        print(f"\nJob failed: {job.get('error', 'unknown error')}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Submit an image to the classifier and print the result.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--image", type=Path,
        default=SCRIPT_DIR / "Neighborhood.jpeg",
        help="Path to the image file to assess",
    )
    parser.add_argument(
        "--criteria", type=str, default=None,
        help='JSON array of criterion objects, e.g. \'[{"name":"sharpness","type":"quality"}]\'',
    )
    parser.add_argument(
        "--criteria-file", type=Path, default=None,
        help="Path to a JSON file containing the criteria array (alternative to --criteria)",
    )
    parser.add_argument(
        "--base-url", default="http://192.168.5.233:4001",
        help="Base URL of the LiteLLM proxy (or classifier directly)",
    )
    parser.add_argument(
        "--api-key", default=None,
        help="API key (defaults to DEFAULT_LITELLM_MASTER_KEY from .env)",
    )
    parser.add_argument(
        "--max-wait", type=int, default=300,
        help="Maximum seconds to wait for the job to complete",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=3,
        help="Seconds between status polls",
    )
    args = parser.parse_args()

    api_key  = args.api_key or load_api_key()
    if args.criteria_file:
        criteria = json.loads(args.criteria_file.read_text(encoding="utf-8"))
    elif args.criteria:
        criteria = json.loads(args.criteria)
    else:
        criteria = DEFAULT_CRITERIA
    image    = args.image.resolve()

    if not image.exists():
        sys.exit(f"Image not found: {image}")

    size_kb = image.stat().st_size / 1024
    print(f"\nImage   : {image.name} ({size_kb:.1f} KB)")
    print(f"Criteria: {[c['name'] for c in criteria]}")
    print(f"Base URL: {args.base_url}\n")

    print("Submitting job...")
    job_id = submit_job(args.base_url, api_key, image, criteria)
    print(f"Job ID  : {job_id}\n")

    print("Polling for result...")
    job = poll_job(args.base_url, api_key, job_id, args.max_wait, args.poll_interval)

    print_result(job)


if __name__ == "__main__":
    main()
