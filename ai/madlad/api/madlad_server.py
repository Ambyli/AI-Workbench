"""Stateless FastAPI server for MADLAD-400 translation.

Proxies requests to the madlad-app inference container.
Exposes an MCP `translate` tool for LiteLLM routing, plus direct
/translate and /languages endpoints.
"""

import os

import httpx
from fastapi import FastAPI, HTTPException
from fastmcp import FastMCP
from pydantic import BaseModel

APP_URL = os.environ.get("MADLAD_APP_URL", "http://madlad-app:8085")

mcp = FastMCP("MADLAD Translation")
mcp_app = mcp.http_app(path="/")

app = FastAPI(title="MADLAD Translation API", lifespan=mcp_app.lifespan)

http_timeout = httpx.Timeout(120.0, connect=10.0)


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
        raise HTTPException(status_code=502, detail=f"Cannot reach madlad-app: {exc}")


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
        raise HTTPException(status_code=502, detail=f"Cannot reach madlad-app: {exc}")


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
        return f"Error translating: {exc}"


app.mount("/mcp", mcp_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
