"""Correlation ID middleware and logging filter.

Every inbound HTTP request is assigned a short unique ID (the correlation ID).
This ID is:
  - Stored in a per-task ContextVar so it is available to any async code
    running within that request without being threaded through function args.
  - Injected into every log record via RequestIDFilter so all log lines for
    a request share the same [request_id] prefix.
  - Returned to the caller in the X-Request-ID response header so they can
    correlate their request with server-side logs.

If the caller supplies an X-Request-ID header, that value is reused.
Otherwise a new 8-character UUID prefix is generated.

Process flow position: CorrelationIDMiddleware wraps the entire FastAPI app
in main.py; RequestIDFilter is attached to the log handler at startup.
"""

import logging
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# asyncio-safe context variable — each concurrent request gets its own value.
# Default "-" is used for log lines emitted outside a request context
# (e.g. startup/shutdown messages).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """Assign and propagate a correlation ID for every inbound request.

    Runs before any route handler so the ID is available from the first
    log line inside the handler.
    """

    async def dispatch(self, request: Request, call_next):
        # Reuse caller-supplied ID or generate a fresh short one
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
        request_id_var.set(req_id)

        response = await call_next(request)

        # Echo the ID back so the caller can link their logs to ours
        response.headers["X-Request-ID"] = req_id
        return response


class RequestIDFilter(logging.Filter):
    """Inject the current request ID into every log record as request_id.

    The log format in main.py references %(request_id)s.  Without this
    filter the attribute would not exist and logging would raise a KeyError.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")
        return True
