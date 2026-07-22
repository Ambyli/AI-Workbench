"""
GMAIL CLIENT — thin wrapper over the Gmail MCP HTTP endpoint.

Replaces the old google-auth-oauthlib-based `listener.py`. Same output shape
(Contract A) so parser + orchestrator are unaffected.

Reads:
    GMAIL_MCP_URL           MCP endpoint (JSON-RPC 2.0 over HTTP)
    GMAIL_MCP_AUTH_VALUE    bearer token
    ROOFIX_SENDER           default "no-reply@roofix.io" (two o's)
    LISTENER_QUERY          Gmail search query (default "is:unread from:<sender>")

Assumed Gmail MCP tool names (configurable via env):
    GMAIL_MCP_TOOL_SEARCH   default "search_threads"
    GMAIL_MCP_TOOL_GET      default "get_message"
    GMAIL_MCP_TOOL_UNLABEL  default "unlabel_message"

The Contract A shape emitted per unread Roofix email:
    {sender, subject, body_text, body_html, timestamp, to,
     message_id, attachments: [{filename, mime_type}]}
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Optional

import httpx


ROOFIX_SENDER = os.getenv("ROOFIX_SENDER", "no-reply@roofix.io")
LISTENER_QUERY = os.getenv("LISTENER_QUERY", f"is:unread from:{ROOFIX_SENDER}")

_TOOL_SEARCH = os.getenv("GMAIL_MCP_TOOL_SEARCH", "search_threads")
_TOOL_GET = os.getenv("GMAIL_MCP_TOOL_GET", "get_message")
_TOOL_UNLABEL = os.getenv("GMAIL_MCP_TOOL_UNLABEL", "unlabel_message")


class GmailMcpClient:
    def __init__(self, url: Optional[str] = None, auth_value: Optional[str] = None,
                 timeout: float = 30.0):
        self.url = (url or os.environ["GMAIL_MCP_URL"]).rstrip("/")
        self.auth = auth_value or os.environ.get("GMAIL_MCP_AUTH_VALUE", "")
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _call_tool(self, name: str, arguments: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.auth:
            headers["Authorization"] = f"Bearer {self.auth}"
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}
        resp = self._client.post(self.url, headers=headers, json=body)
        resp.raise_for_status()
        env = resp.json()
        if "error" in env:
            raise RuntimeError(f"MCP error: {env['error']}")
        result = env.get("result", {})
        if result.get("isError"):
            raise RuntimeError(f"MCP tool '{name}' returned isError=true: {result}")
        for block in result.get("content", []) or []:
            if block.get("type") == "text":
                txt = block.get("text", "")
                try:
                    return json.loads(txt)
                except json.JSONDecodeError:
                    return {"text": txt}
        return {}

    # --- Contract A adapters -----------------------------------------------------

    def fetch(self, max_results: int = 25, query: Optional[str] = None) -> list:
        """Return raw emails matching the query. Does NOT mark them read — the
        caller marks read only after successful processing so a crash can't
        silently drop an event."""
        q = query or LISTENER_QUERY
        threads = self._call_tool(_TOOL_SEARCH,
                                  {"query": q, "pageSize": max_results})
        message_ids = _extract_message_ids(threads)

        out = []
        for mid in message_ids[:max_results]:
            msg = self._call_tool(_TOOL_GET,
                                  {"messageId": mid, "messageFormat": "FULL_CONTENT"})
            out.append(_to_contract_a(msg, mid))
        return out

    def mark_read(self, message_id: str) -> None:
        self._call_tool(_TOOL_UNLABEL,
                        {"messageId": message_id, "labelIds": ["UNREAD"]})


def _extract_message_ids(threads_result: dict) -> list[str]:
    """search_threads returns a list of threads with nested messages. Collect
    every message id in every returned thread."""
    ids: list[str] = []
    threads = threads_result.get("threads", threads_result.get("value", []))
    if isinstance(threads, dict):
        threads = threads.get("threads", [])
    for t in threads or []:
        for m in (t.get("messages") or []):
            mid = m.get("id") or m.get("messageId")
            if mid:
                ids.append(mid)
        # Some MCPs return a flat message id list on the thread instead.
        for mid in (t.get("messageIds") or []):
            ids.append(mid)
    return ids


def _to_contract_a(msg: dict, message_id: str) -> dict:
    """Normalize a Gmail MCP message payload into the parser's expected shape."""
    subject = msg.get("subject") or msg.get("Subject") or ""
    sender = msg.get("from") or msg.get("sender") or msg.get("From") or ""
    to_field = msg.get("to") or msg.get("To") or ""
    to_list = to_field if isinstance(to_field, list) else [
        a.strip() for a in str(to_field).split(",") if a.strip()]
    body_text = msg.get("plaintextBody") or msg.get("body_text") or msg.get("snippet") or ""
    body_html = msg.get("htmlBody") or msg.get("body_html")
    timestamp = msg.get("date") or msg.get("timestamp") or ""

    attachments = []
    for a in (msg.get("attachments") or []):
        attachments.append({"filename": a.get("filename", ""),
                            "mime_type": a.get("mimeType", "")})

    return {
        "sender": sender,
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
        "timestamp": timestamp,
        "to": to_list,
        "message_id": msg.get("id") or message_id,
        "attachments": attachments,
    }


def make_listener_callable(max_results: int = 25):
    """Return a zero-arg callable for orchestrator.run(listener=...)."""
    gc = GmailMcpClient()
    return lambda: gc.fetch(max_results=max_results)
