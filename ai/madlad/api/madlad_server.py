"""Stateless FastAPI server for MADLAD-400 translation.

Proxies requests to the madlad-app inference container.
Exposes an MCP `translate` tool for LiteLLM routing, plus direct
/translate and /languages endpoints.
"""

import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastmcp import FastMCP
from pydantic import BaseModel

APP_URL = os.environ.get("MADLAD_APP_URL", "http://madlad-app:8085")

mcp = FastMCP("MADLAD Translation")
mcp_app = mcp.http_app(path="/")

app = FastAPI(title="MADLAD Translation API", lifespan=mcp_app.lifespan)

http_timeout = httpx.Timeout(120.0, connect=10.0)


def _app_error(exc: httpx.HTTPError) -> tuple[int, Any]:
    """``(status, detail)`` for a failed madlad-app call.

    An upstream 4xx is the caller's mistake (an unknown ``target_lang`` is
    madlad-app's 400): keep its status and its ``detail``. Anything else --
    unreachable, timed out, an upstream 5xx -- is a 502.
    """
    if isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500:
        response = exc.response
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "detail" in body:
            return response.status_code, body["detail"]
        return response.status_code, response.text or response.reason_phrase
    return 502, f"Cannot reach madlad-app: {exc}"


def _raise_app_error(exc: httpx.HTTPError):
    status, detail = _app_error(exc)
    raise HTTPException(status_code=status, detail=detail)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/languages")
def list_languages():
    try:
        r = httpx.get(f"{APP_URL}/languages", timeout=http_timeout)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        _raise_app_error(exc)


class TranslateRequest(BaseModel):
    text: str
    target_lang: str


@app.post("/translate")
def translate_endpoint(req: TranslateRequest):
    try:
        r = httpx.post(
            f"{APP_URL}/translate",
            json=req.model_dump(),
            timeout=http_timeout,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        _raise_app_error(exc)


@mcp.tool()
def translate(text: str, target_lang: str) -> str:
    """Translate text into target_lang using MADLAD-400.

    Args:
        text: The source text. Source language is auto-detected.
        target_lang: ISO 639-1 language code (e.g. "es", "fr", "ja", "zh").
            Call the /languages endpoint for the full list of supported codes.

    Returns:
        The translated text.
    """
    try:
        r = httpx.post(
            f"{APP_URL}/translate",
            json={"text": text, "target_lang": target_lang},
            timeout=http_timeout,
        )
        r.raise_for_status()
        return r.json()["translated"]
    except httpx.HTTPError as exc:
        # A 4xx from madlad-app (an unknown target_lang) says what to fix:
        # relay its detail; unreachable / 5xx keeps httpx's own message.
        status, detail = _app_error(exc)
        return f"Error translating: {detail if status != 502 else exc}"


app.mount("/mcp", mcp_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
