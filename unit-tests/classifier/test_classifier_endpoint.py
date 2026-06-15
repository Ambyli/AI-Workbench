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
import base64
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


def encode_image(image_path: Path) -> str:
    """Base64-encode an image file for use in JSON request bodies."""
    return base64.b64encode(image_path.read_bytes()).decode()


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


def submit_compare_job(
    base_url: str,
    api_key: str,
    input_path: Path,
    example_path: Path,
    criteria: list,
    example_weight: float = 0.5,
    aggregation: str = "mean",
    pre_generated_analysis: dict = None,
) -> str:
    """Submit a /assess/compare job and return the job ID.

    If pre_generated_analysis is provided it is passed as the example's
    pre_generated_analysis — skipping re-analysis of the example image.
    The example image bytes are always included so the server has them if needed.
    """
    url = f"{base_url}/v1/classifier/assess/compare"
    print(f"  POST {url}")

    body = {
        "image": {"data": encode_image(input_path), "type": "base64"},
        "criteria": criteria,
        "aggregation": aggregation,
        "examples": [{
            "data":                  encode_image(example_path),
            "type":                  "base64",
            "weight":                example_weight,
            "pre_generated_analysis": pre_generated_analysis,
        }],
    }

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    if not resp.ok:
        sys.exit(f"Compare submit failed ({resp.status_code}): {resp.text}")
    return resp.json()["job_id"]


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

        print(f"\n{'-' * 60}")
        print(f"  Verdict          : {result.get('verdict', 'n/a')} "
              f"(score {assessment.get('overall_score', '-')})")
        print(f"{'-' * 60}")

        per = assessment.get("per_criterion_scores", {})
        if per:
            print("\n  Per-criterion scores:")
            for name, val in per.items():
                if not isinstance(val, dict):
                    continue
                verdict = val.get("verdict", "?")
                if verdict == "SKIPPED":
                    print(f"    {name:<35} {'SKIPPED':<10}  [{val.get('reason', '')}]")
                else:
                    method = val.get("method", "?")
                    conf   = f"  confidence={val.get('confidence', '?')}"
                    print(f"    {name:<35} {verdict:<10} "
                          f"score={val.get('score', '?')}{conf}  [{method}]")
                    reason = val.get("reason", "")
                    if reason:
                        print(f"      {reason}")

        breakdown = assessment.get("weighted_score_breakdown")
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


def print_compare_result(job: dict, label: str = "") -> None:
    if job["status"] == "completed":
        result = job.get("result", {})
        agg    = result.get("aggregate", {})

        header = f"  Comparison result{f' — {label}' if label else ''}"
        print(f"\n{'-' * 60}")
        print(header)
        print(f"  Aggregate verdict : {agg.get('combined_verdict', 'n/a')} "
              f"(score {agg.get('combined_score', '-')}, method={agg.get('method', '-')})")
        print(f"{'-' * 60}")

        for ex in result.get("example_results", []):
            pre = "pre-generated" if ex.get("pre_generated") else "live"
            print(f"\n  Example {ex['index']}  weight={ex['weight']}  [{pre}]")
            print(f"    Combined score   : {ex.get('combined_score', '-')} "
                  f"→ {ex.get('combined_verdict', '-')}")
            sim = ex.get("similarity", {})
            print(f"    Overall sim      : {sim.get('overall_similarity', '-')} "
                  f"(score {sim.get('similarity_score', '-')})")
            for crit, vals in sim.get("per_criterion", {}).items():
                print(f"      {crit:<35} sim={vals['similarity']:.3f}  "
                      f"(example={vals['example_score']} / input={vals['input_score']})")

        print("\n  Full JSON:")
        print(json.dumps(result, indent=2))
    else:
        print(f"\nCompare job failed: {job.get('error', 'unknown error')}", file=sys.stderr)
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

    # Save the assess result to use as pre_generated_analysis in the first comparison
    prior_analysis = job.get("result")

    # --- Comparison 1: example uses pre-generated analysis (no re-analysis) ---
    print(f"\n{'=' * 60}")
    print(f"  Comparison 1 — pre-generated example analysis")
    print(f"{'=' * 60}")
    print(f"\nInput   : {image.name}")
    print(f"Example : {image.name}  [using cached analysis from assess above]")
    print(f"Criteria: {[c['name'] for c in criteria]}\n")

    print("Submitting compare job...")
    cmp_id = submit_compare_job(
        args.base_url, api_key, image, image, criteria,
        example_weight=0.5,
        pre_generated_analysis=prior_analysis,
    )
    print(f"Job ID  : {cmp_id}\n")

    print("Polling for result...")
    cmp_job = poll_job(args.base_url, api_key, cmp_id, args.max_wait, args.poll_interval)
    print_compare_result(cmp_job, label="pre-generated example")

    # --- Comparison 2: both images analyzed live, no pre-generated analysis ---
    print(f"\n{'=' * 60}")
    print(f"  Comparison 2 — fully live (both images re-analyzed)")
    print(f"{'=' * 60}")
    print(f"\nInput   : {image.name}")
    print(f"Example : {image.name}  [analyzed live]")
    print(f"Criteria: {[c['name'] for c in criteria]}\n")

    print("Submitting compare job...")
    cmp_id2 = submit_compare_job(
        args.base_url, api_key, image, image, criteria,
        example_weight=0.5,
        pre_generated_analysis=None,
    )
    print(f"Job ID  : {cmp_id2}\n")

    print("Polling for result...")
    cmp_job2 = poll_job(args.base_url, api_key, cmp_id2, args.max_wait, args.poll_interval)
    print_compare_result(cmp_job2, label="fully live")


if __name__ == "__main__":
    main()
