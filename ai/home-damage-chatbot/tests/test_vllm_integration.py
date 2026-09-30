"""Comprehensive test suite for vLLM backend integration.

Tests:
  1. Backend resolution & configuration switching (vllm, ollama, mock).
  2. Health & readiness checking (/v1/models).
  3. OpenAI-compatible /v1/chat/completions schema extraction for all field types.
  4. Responsiveness classification (_vllm_is_responsive).
  5. Bearer token authentication (VLLM_API_KEY).
  6. Robust fallback to deterministic mock extraction on vLLM outage/error.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import httpx

from backend import llm
from backend.config import settings


def _mock_resp(status_code: int = 200, json_data: dict = None, text: str = None) -> httpx.Response:
    req = httpx.Request("POST", "http://mock-vllm:8000/v1/chat/completions")
    if json_data is not None:
        return httpx.Response(status_code, json=json_data, request=req)
    return httpx.Response(status_code, text=text or "", request=req)


def test_backend_resolution():
    print("Testing backend resolution...")
    with patch.dict(os.environ, {"USE_MOCK_LLM": "1"}):
        llm.reset_mock_cache()
        assert llm.get_active_backend() == "mock"
        assert llm.using_mock() is True

    with patch.dict(os.environ, {"USE_MOCK_LLM": "0", "LLM_BACKEND": "vllm"}):
        llm.reset_mock_cache()
        assert llm.get_active_backend() == "vllm"

    with patch.dict(os.environ, {"USE_MOCK_LLM": "0", "LLM_BACKEND": "ollama"}):
        llm.reset_mock_cache()
        assert llm.get_active_backend() == "ollama"

    print("  ✓ Backend resolution and environment overrides work correctly")


def test_vllm_readiness_check():
    print("Testing vLLM readiness checking...")
    with patch.dict(os.environ, {"USE_MOCK_LLM": "0", "LLM_BACKEND": "vllm", "VLLM_BASE_URL": "http://mock-vllm:8000/v1"}):
        llm.reset_mock_cache()

        # Case 1: Reachable vLLM server (/v1/models returns 200)
        with patch("httpx.get") as mock_get:
            mock_get.return_value = _mock_resp(200, json_data={"data": [{"id": "qwen3.8:27b"}]})
            assert llm.is_backend_ready() is True
            assert llm.using_mock() is False

        # Case 2: Unreachable vLLM server -> falls back to mock
        llm.reset_mock_cache()
        with patch("httpx.get", side_effect=httpx.ConnectError("Connection refused")):
            assert llm.is_backend_ready() is False
            assert llm.using_mock() is True

    print("  ✓ vLLM readiness detection and automatic mock fallback work correctly")


def test_vllm_schema_extractions():
    print("Testing vLLM JSON extraction for all field types...")
    with patch.dict(os.environ, {
        "USE_MOCK_LLM": "0",
        "LLM_BACKEND": "vllm",
        "VLLM_BASE_URL": "http://mock-vllm:8000/v1",
        "VLLM_MODEL": "qwen3.8:27b",
        "VLLM_API_KEY": "test-vllm-secret-key",
    }):
        llm.reset_mock_cache()

        # Mock successful readiness check
        with patch("backend.llm.is_backend_ready", return_value=True):
            # 1. Yes/No extraction
            mock_response_yesno = _mock_resp(
                200,
                json_data={"choices": [{"message": {"content": json.dumps({"value": True})}}]},
            )
            with patch("httpx.post", return_value=mock_response_yesno) as mock_post:
                res = llm.extract("yesno", "yes, definitely leaking", field_label="Active Leak")
                assert res is True
                call_args = mock_post.call_args
                assert call_args[1]["headers"]["Authorization"] == "Bearer test-vllm-secret-key"
                payload = call_args[1]["json"]
                assert payload["model"] == "qwen3.8:27b"
                assert payload["temperature"] == 0.0

            # 2. Integer scale 1..10 extraction
            mock_response_int = _mock_resp(
                200,
                json_data={"choices": [{"message": {"content": json.dumps({"value": 9})}}]},
            )
            with patch("httpx.post", return_value=mock_response_int):
                res = llm.extract("int_1_10", "it's about a 9 out of 10", field_label="Urgency")
                assert res == 9

            # 3. Enum extraction with options
            mock_response_enum = _mock_resp(
                200,
                json_data={"choices": [{"message": {"content": json.dumps({"value": "tile"})}}]},
            )
            with patch("httpx.post", return_value=mock_response_enum):
                res = llm.extract(
                    "enum",
                    "we have spanish style tile roof",
                    field_label="Roof Type",
                    options=["shingle", "tile", "metal", "flat"],
                )
                assert res == "tile"

            # 4. Text extraction with markdown fence handling
            mock_response_fence = _mock_resp(
                200,
                json_data={"choices": [{"message": {"content": "```json\n{\"value\": \"Water dripping in master bedroom\"}\n```"}}]},
            )
            with patch("httpx.post", return_value=mock_response_fence):
                res = llm.extract("text", "Water dripping in master bedroom", field_label="Description")
                assert res == "Water dripping in master bedroom"

            # 5. Pointer passthrough
            res = llm.extract("pointer", "photo_pointer_123.png")
            assert res == "photo_pointer_123.png"

    print("  ✓ vLLM structured extractions succeed across all field types")


def test_vllm_responsiveness_classifier():
    print("Testing vLLM responsiveness classifier...")
    with patch.dict(os.environ, {
        "USE_MOCK_LLM": "0",
        "LLM_BACKEND": "vllm",
        "VLLM_BASE_URL": "http://mock-vllm:8000/v1",
        "VLLM_MODEL": "qwen3.8:27b",
    }):
        llm.reset_mock_cache()

        with patch("backend.llm.is_backend_ready", return_value=True):
            # Case 1: Clear responsive answer
            mock_resp_good = _mock_resp(
                200,
                json_data={"choices": [{"message": {"content": json.dumps({"responsive": True, "confidence": 0.95})}}]},
            )
            with patch("httpx.post", return_value=mock_resp_good):
                responsive, conf = llm.is_responsive("What is your name?", "My name is Carlos Martinez")
                assert responsive is True
                assert conf == 0.95

            # Case 2: Evasive / non-answer
            mock_resp_bad = _mock_resp(
                200,
                json_data={"choices": [{"message": {"content": json.dumps({"responsive": False, "confidence": 0.05})}}]},
            )
            with patch("httpx.post", return_value=mock_resp_bad):
                responsive, conf = llm.is_responsive("What is your name?", "why do you need to know that?")
                assert responsive is False
                assert conf == 0.05

    print("  ✓ vLLM responsiveness classification evaluates answers accurately")


def test_vllm_error_fallback_resilience():
    print("Testing vLLM error resilience and graceful fallback...")
    with patch.dict(os.environ, {
        "USE_MOCK_LLM": "0",
        "LLM_BACKEND": "vllm",
        "VLLM_BASE_URL": "http://mock-vllm:8000/v1",
        "VLLM_MODEL": "qwen3.8:27b",
    }):
        llm.reset_mock_cache()

        with patch("backend.llm.is_backend_ready", return_value=True):
            # 1. HTTP 500 Internal Server Error from vLLM -> falls back to mock without raising
            with patch("httpx.post", return_value=_mock_resp(500, text="GPU out of memory")):
                res = llm.extract("yesno", "yes the breaker tripped", field_label="Breakers Tripped")
                assert res is True, "Must fall back to mock and extract valid answer"

            # 2. Network Timeout -> falls back to mock
            with patch("httpx.post", side_effect=httpx.TimeoutException("Read timed out")):
                res = llm.extract("enum", "roof", options=["roof", "electrical", "solar", "misc"])
                assert res == "roof"

            # 3. Invalid JSON payload -> falls back to mock
            with patch("httpx.post", return_value=_mock_resp(200, json_data={"choices": [{"message": {"content": "INVALID_NON_JSON"}}]})):
                res = llm.extract("int_1_10", "urgency 8 out of 10")
                assert res == 8

    print("  ✓ vLLM error handling never crashes turns and gracefully recovers via mock")


if __name__ == "__main__":
    try:
        test_backend_resolution()
        test_vllm_readiness_check()
        test_vllm_schema_extractions()
        test_vllm_responsiveness_classifier()
        test_vllm_error_fallback_resilience()
        print("\nALL vLLM INTEGRATION TESTS PASSED ✓")
    except AssertionError as e:
        print(f"\nvLLM INTEGRATION TEST FAILED: {e}")
        sys.exit(1)
