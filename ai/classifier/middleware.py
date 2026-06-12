"""Correlation ID middleware.

Generates a short unique ID per request, threads it through logs via a
logging.Filter, and returns it in the X-Request-ID response header.
"""

import logging
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Module-level context var — safe with asyncio, each task gets its own value
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """Attach a request ID to every inbound request."""

    async def dispatch(self, request: Request, call_next):
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
        request_id_var.set(req_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response


class RequestIDFilter(logging.Filter):
    """Inject the current request ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")
        return True
