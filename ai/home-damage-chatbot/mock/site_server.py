"""Local stand-in for the production nginx in front of the Service page mock.

Serves the production build of the website (built by scripts/mock-start.ps1 into
mock/.site-build) and forwards ONLY the four public chatbot routes to the backend,
the same allow-list the deployed proxy will have:

    POST /api/chat   POST /api/queue/status   GET /api/health   POST /api/upload

Everything else under /api/ is 404 here, before it reaches the backend.
POST /mock-form is a sink for the classic form, so the mock can never reach the
production email Cloud Function. No Docker needed.

Run:  python mock/site_server.py --port 8080 --api http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

ROOT = (Path(__file__).parent / ".site-build").resolve()
API = os.environ.get("MOCK_API_URL", "http://127.0.0.1:8000")
MAX_BODY = 10 * 1024 * 1024  # matches the upload ceiling plus multipart overhead

ALLOWED = {
    ("POST", "/api/chat"),
    ("POST", "/api/queue/status"),
    ("GET", "/api/health"),
    ("POST", "/api/upload"),
}
SECURITY_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(self)",
}
_PASS_REQUEST = {"content-type", "accept"}
# Like nginx proxy_pass, keep the API's own security headers (CSP, frame denial, ...).
_PASS_RESPONSE = {"content-type", "retry-after", "cache-control", "content-security-policy",
                  "x-frame-options", "referrer-policy", "cross-origin-opener-policy",
                  "x-content-type-options"}


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response


async def api(request: Request) -> Response:
    if (request.method, request.url.path) not in ALLOWED:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    body = await request.body()
    if len(body) > MAX_BODY:
        return JSONResponse({"detail": "payload_too_large"}, status_code=413)
    headers = {k: v for k, v in request.headers.items() if k.lower() in _PASS_REQUEST}
    # Overwrite (never append) so a client cannot forge its address for rate limiting.
    headers["X-Forwarded-For"] = request.client.host if request.client else "127.0.0.1"
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            upstream = await client.request(request.method, API + request.url.path,
                                            content=body, headers=headers)
    except httpx.HTTPError:
        return JSONResponse({"detail": "assistant_unavailable"}, status_code=503)
    out = {k: v for k, v in upstream.headers.items() if k.lower() in _PASS_RESPONSE}
    return Response(upstream.content, status_code=upstream.status_code, headers=out)


async def mock_form(request: Request) -> Response:
    """Classic-form sink: accepts and discards. The mock never sends real email."""
    return JSONResponse({"ok": True, "mock": True})


async def spa(request: Request) -> Response:
    rel = request.path_params.get("path", "")
    target = (ROOT / rel).resolve()
    if rel and target.is_file() and ROOT in target.parents:
        cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
        return FileResponse(target, headers={"Cache-Control": cache})
    index = ROOT / "index.html"
    if not index.is_file():
        return Response("Site not built. Run scripts/mock-start.ps1 -Rebuild.", status_code=503)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


app = Starlette(routes=[
    Route("/api/{path:path}", api, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
    Route("/mock-form", mock_form, methods=["POST"]),
    Route("/{path:path}", spa, methods=["GET", "HEAD"]),
])
app.add_middleware(SecurityHeaders)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--api", default=API)
    args = ap.parse_args()
    API = args.api
    # proxy_headers=False: like nginx's $remote_addr, the client address is the real TCP
    # peer. uvicorn would otherwise trust X-Forwarded-For from localhost callers, letting a
    # local client spoof its address past the rate limit (found in the security re-test).
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", proxy_headers=False)
