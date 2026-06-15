#!/usr/bin/env python3
"""
Test the classifier endpoints.

Fetches available hints and CV detectors, then submits an image assessment
job and polls until complete.

Usage:
    python test_classifier_endpoint.py
    python test_classifier_endpoint.py --image path/to/image.jpg
    python test_classifier_endpoint.py --criteria-file unit-tests/classifier/criteria.json
    python test_classifier_endpoint.py --base-url http://192.168.5.233:4001 --max-wait 120

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
    {"name": "document legibility", "type": "llm", "hint": "quality"},
    {"name": "image sharpness",     "type": "llm", "hint": "quality"},
    {"name": "proper exposure",     "type": "llm", "hint": "quality"},
    {"name": "absence of artifacts","type": "llm", "hint": "quality"},
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
            if " #" in value:
                value = value[:value.index(" #")].strip()
            return value
    sys.exit("ERROR: DEFAULT_LITELLM_MASTER_KEY not found in .env")


def get(base_url: str, api_key: str, path: str) -> dict:
    """Perform a GET request to the classifier API and return the JSON body."""
    url = f"{base_url}/v1/classifier{path}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
    if not resp.ok:
        sys.exit(f"GET {path} failed ({resp.status_code}): {resp.text}")
    return resp.json()


def fetch_hints(base_url: str, api_key: str) -> dict:
    return get(base_url, api_key, "/hints")


def fetch_cv_detectors(base_url: str, api_key: str) -> dict:
    return get(base_url, api_key, "/cv-detectors")


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


def poll_job(base_url: str, api_key: str, job_id: str, max_wait: int, poll_interval: int) -> dict:
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


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def print_hints(hints_data: dict) -> None:
    hints = hints_data.get("hints", {})
    print(f"\n{'=' * 60}")
    print(f"  Available hints ({len(hints)})")
    print(f"{'=' * 60}")
    for name, defn in hints.items():
        print(f"\n  [{name}]")
        print(f"    {defn.get('heading', '')}")
        print(f"    Rubric : {defn.get('rubric', '')}")
        if defn.get("extra"):
            print(f"    Extra  : {defn['extra']}")


def print_cv_detectors(detectors_data: dict) -> None:
    detectors = detectors_data.get("detectors", [])
    total = detectors_data.get("total_names", 0)
    print(f"\n{'=' * 60}")
    print(f"  Registered CV detectors ({len(detectors)} functions, {total} names)")
    print(f"{'=' * 60}")
    for d in detectors:
        names = ", ".join(d.get("names", []))
        print(f"  {d['function']:<25}  {names}")


def print_result(job: dict, criteria: list = None) -> None:
    if job["status"] == "completed":
        result     = job.get("result", {})
        assessment = result.get("assessment", {})
        cv         = assessment.get("cv",       {})
        llm        = assessment.get("llm",      {})
        combined   = assessment.get("combined", {})
        cv_criteria = {k: v for k, v in cv.items() if isinstance(v, dict)}

        print(f"\n{'-' * 60}")
        print(f"  Combined verdict : {result.get('combined_verdict', 'n/a')} "
              f"(score {combined.get('overall_score', '-')})")
        if cv_criteria:
            print(f"  CV overall       : {cv.get('overall_verdict', 'n/a')} "
                  f"(score {cv.get('overall_score', '-')})")
            for name, val in cv_criteria.items():
                print(f"    {name:<30} {val.get('verdict', '?')}  "
                      f"score={val.get('score', '?')}")
        if llm.get("overall_verdict"):
            print(f"  LLM overall      : {llm.get('overall_verdict', 'n/a')} "
                  f"(score {llm.get('overall_score', '-')})")
        print(f"{'-' * 60}")

        per = combined.get("per_criterion_scores", {})
        if per:
            print("\n  Per-criterion scores (combined):")
            for name, val in per.items():
                if not isinstance(val, dict):
                    continue
                method = val.get("method", "?")
                conf   = f"  confidence={val.get('confidence', '?')}"
                print(f"    {name:<35} {val.get('verdict', '?'):<10} "
                      f"score={val.get('score', '?')}{conf}  [{method}]")
                reason = val.get("reason", "")
                if reason:
                    print(f"      {reason}")

        breakdown = combined.get("weighted_score_breakdown")
        if breakdown:
            print(f"\n  Weighted score breakdown  ({breakdown.get('formula')})")
            print(f"  {'Criterion':<35} {'Score':>5}  {'Weight':>7}  {'Contribution':>12}")
            print(f"  {'-'*35}  {'-'*5}  {'-'*7}  {'-'*12}")
            for name, vals in breakdown.get("per_criterion", {}).items():
                print(f"  {name:<35} {vals['score']:>5}  {vals['weight']:>7}  {vals['contribution']:>12.4f}")
            print(f"  {'-'*35}  {'-'*5}  {'-'*7}  {'-'*12}")
            print(f"  {'Total weight':<35} {'':>5}  {breakdown['total_weight']:>7}  "
                  f"{'Σ = ' + str(round(breakdown['weighted_sum'], 4)):>12}")
            print(f"  {'Unrounded average':<35} {breakdown['unrounded_average']:>5.4f}")
            print(f"  {'Final score (rounded)':<35} {breakdown['final_score']:>5}")

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
        description="Test the classifier API — fetches capabilities then runs an assessment.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--image", type=Path, default=SCRIPT_DIR / "Neighborhood.jpeg")
    parser.add_argument("--criteria", type=str, default=None)
    parser.add_argument("--criteria-file", type=Path, default=None)
    parser.add_argument("--base-url", default="http://192.168.5.233:4001")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--max-wait", type=int, default=300)
    parser.add_argument("--poll-interval", type=int, default=3)
    args = parser.parse_args()

    api_key = args.api_key or load_api_key()

    if args.criteria_file:
        criteria = json.loads(args.criteria_file.read_text(encoding="utf-8"))
    elif args.criteria:
        criteria = json.loads(args.criteria)
    else:
        criteria = DEFAULT_CRITERIA

    image = args.image.resolve()
    if not image.exists():
        sys.exit(f"Image not found: {image}")

    # --- Fetch and display capabilities ---
    print(f"\nBase URL : {args.base_url}")

    print("\nFetching hints...")
    hints_data = fetch_hints(args.base_url, api_key)
    print_hints(hints_data)

    print("\nFetching CV detectors...")
    detectors_data = fetch_cv_detectors(args.base_url, api_key)
    print_cv_detectors(detectors_data)

    # --- Submit and poll assessment job ---
    size_kb = image.stat().st_size / 1024
    print(f"\n{'=' * 60}")
    print(f"  Assessment")
    print(f"{'=' * 60}")
    print(f"\nImage    : {image.name} ({size_kb:.1f} KB)")
    print(f"Criteria : {[c['name'] for c in criteria]}\n")

    print("Submitting job...")
    job_id = submit_job(args.base_url, api_key, image, criteria)
    print(f"Job ID   : {job_id}\n")

    print("Polling for result...")
    job = poll_job(args.base_url, api_key, job_id, args.max_wait, args.poll_interval)

    print_result(job, criteria)


if __name__ == "__main__":
    main()
