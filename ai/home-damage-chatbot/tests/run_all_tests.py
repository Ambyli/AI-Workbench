"""Master Test Runner for Zeo Energy Service Chatbot.

Runs the complete suite of functional, integration, model compatibility, and security tests:
  1. End-to-End Smoke Test (smoke_test.py)
  2. vLLM Backend Integration & Fallback Suite (test_vllm_integration.py)
  3. Qwen 3.8 27B Model Compatibility Suite (test_qwen_model_compatibility.py)
  4. Comprehensive Security & Hardening Suite (test_comprehensive_security.py)
  5. Characterization & Behavior Lockdown Suite (characterization_test.py)
  6. Disposition Pipeline & Hardening Suite (pipeline_test.py)
  7. Google Workspace & Chat Integration Suite (integration_gmail_chat_test.py)
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# Ensure UTF-8 output on all consoles
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT_DIR = Path(__file__).resolve().parent.parent

TEST_SUITES = [
    ("End-to-End Smoke Flows", "tests/smoke_test.py"),
    ("vLLM Integration & OpenAI Schema", "tests/test_vllm_integration.py"),
    ("Qwen 3.8 27B Model Compatibility", "tests/test_qwen_model_compatibility.py"),
    ("Comprehensive Security & Hardening", "tests/test_comprehensive_security.py"),
    ("User Concurrency & Queueing", "tests/test_queue_concurrency.py"),
    ("State Machine Characterization", "tests/characterization_test.py"),
    ("Disposition Pipeline & CRM Match", "tests/pipeline_test.py"),
    ("Gmail & Google Chat Integration", "tests/integration_gmail_chat_test.py"),
]


def run_suite(name: str, script_path: str) -> tuple[bool, float, str]:
    full_path = ROOT_DIR / script_path
    start = time.time()
    res = subprocess.run(
        [sys.executable, str(full_path)],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    elapsed = time.time() - start
    output = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
    return (res.returncode == 0, elapsed, output)


def main():
    print("=" * 75)
    print(" ZEO ENERGY SERVICE CHATBOT — COMPREHENSIVE TEST SUITE RUNNER")
    print("=" * 75)
    print(f"Python Executable : {sys.executable}")
    print(f"Working Directory : {ROOT_DIR}")
    print(f"Total Test Suites : {len(TEST_SUITES)}\n")

    results = []
    all_passed = True

    for idx, (name, path) in enumerate(TEST_SUITES, 1):
        print(f"[{idx}/{len(TEST_SUITES)}] Running {name} ({path})...", flush=True)
        passed, elapsed, output = run_suite(name, path)
        if passed:
            print(f"      ✓ PASSED ({elapsed:.2f}s)\n")
        else:
            print(f"      ✗ FAILED ({elapsed:.2f}s)\n")
            print("--- Output from failure ---")
            print(output.strip())
            print("---------------------------\n")
            all_passed = False
        results.append((name, path, passed, elapsed))

    print("=" * 75)
    print(" TEST SUITE SUMMARY")
    print("=" * 75)
    for name, path, passed, elapsed in results:
        status_badge = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status_badge:<8} | {elapsed:>5.2f}s | {name:<35} ({path})")

    print("-" * 75)
    total_time = sum(r[3] for r in results)
    pass_count = sum(1 for r in results if r[2])
    fail_count = len(results) - pass_count

    print(f"Total: {len(results)} suites | Passed: {pass_count} | Failed: {fail_count} | Time: {total_time:.2f}s")
    if all_passed:
        print("\n🎉 ALL TEST SUITES PASSED CLEANLY! Chatbot service is verified.")
        sys.exit(0)
    else:
        print("\n❌ SOME TEST SUITES FAILED. Please inspect the output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
