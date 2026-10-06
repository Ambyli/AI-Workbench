"""One tiny real completion against the configured model, so the mock scripts can
say plainly whether the demo is on the real Qwen model. Never prints the key."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from backend import llm  # noqa: E402
from backend.config import settings  # noqa: E402


def main() -> int:
    if llm.get_active_backend() == "mock":
        print("Model: offline stand-in (USE_MOCK_LLM=1). Not the real Qwen model.")
        return 0
    headers = {"Content-Type": "application/json"}
    if settings.VLLM_API_KEY:
        headers["Authorization"] = f"Bearer {settings.VLLM_API_KEY}"
    started = time.monotonic()
    try:
        r = httpx.post(f"{llm.get_host_url()}/chat/completions", headers=headers, timeout=30, json={
            "model": llm.get_model_name(), "max_tokens": 4,
            "messages": [{"role": "user", "content": "Reply OK."}],
            "chat_template_kwargs": {"enable_thinking": False},
        })
    except httpx.HTTPError as exc:
        print(f"Model: UNREACHABLE ({type(exc).__name__}) at {llm.get_host_url()}")
        return 2
    took = time.monotonic() - started
    if r.status_code == 200:
        print(f"Model: {r.json().get('model', llm.get_model_name())} OK ({took:.1f}s) via {llm.get_host_url()}")
        return 0
    try:
        detail = r.json().get("error", {}).get("message", r.text[:200])
    except ValueError:
        detail = r.text[:200]
    print(f"Model: REFUSED HTTP {r.status_code} for {llm.get_model_name()!r}: {detail}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
