// interceptor.js
// Patches fetch + XHR so every JSON response is recorded in
// window._capturedResponses before the page script can read it.
//
// DEBUG_LOGGING is injected as a boolean constant by usage_fetcher.py
// immediately before this script is evaluated. When true, every captured
// request URL and its response body are printed to the browser console.

// Shared list where every captured JSON response is stored.
// The `|| []` guard preserves any responses already collected if this
// script is injected more than once (e.g. after a manual reload).
window._capturedResponses = window._capturedResponses || [];

const _log = (...args) => {
  if (DEBUG_LOGGING) console.log(...args);
};

_log("[interceptor] script injected — DEBUG_LOGGING is active");

// Only patch fetch/XHR once per page lifetime.  Without this guard, each
// injection would wrap the already-wrapped APIs again, producing nested
// interceptors that push duplicate entries into _capturedResponses.
if (!window._fetchInterceptorActive) {
  window._fetchInterceptorActive = true;
  _log("[interceptor] first injection — patching fetch and XHR");

  // Record one captured JSON response: stamp it with a monotonic per-document
  // sequence id, append it to the shared array, and notify the CDP side with
  // that SAME seq. The seq lets the CDP fetcher dedup a response across its two
  // delivery paths (the bindingCalled fast path and the array poll fallback)
  // without dropping or double-delivering — the fast path and the poll can race
  // freely because both carry the same stable id. ``_capSeq`` lives on window
  // so it survives re-injection (the guard above skips re-patching) and resets
  // naturally on a new document, in lockstep with _capturedResponses.
  const _record = (url, body) => {
    const seq = (window._capSeq = (window._capSeq | 0) + 1);
    window._capturedResponses.push({ seq: seq, url: url, body: body });
    _log("[interceptor] captured & notified:", seq, url, body);
    // Best-effort: the fetcher may have navigated and lost the binding.
    try {
      window.__cdpNotify(JSON.stringify({ seq: seq, url: url, body: body }));
    } catch (_) {
      _log("[interceptor] failed to notify CDP fetcher for", url);
    }
  };

  // ── Patch window.fetch ────────────────────────────────────────────────
  // Save a reference to the real fetch before we overwrite it.
  const _origFetch = window.fetch.bind(window);
  _log("[interceptor] fetch: original saved, installing wrapper");

  window.fetch = async function (input, init) {
    // Normalise the request target to a plain URL string.
    const url = typeof input === "string" ? input : input.url || "";
    _log("[interceptor] fetch: request outgoing →", url);

    // Let the real request go out exactly as the page intended.
    let response;
    try {
      response = await _origFetch(input, init);
    } catch (e) {
      _log("[interceptor] fetch: request failed →", url, e);
      throw e;
    }

    // Try to parse the body as JSON regardless of content-type. Some sites
    // (notably Bubble.io) return JSON with content-type text/plain or nothing
    // at all, and the strict header check silently drops them. Mirror the
    // XHR patch's approach: attempt parsing and let JSON.parse decide.
    // Response bodies can only be consumed once, so we clone before reading.
    // The original response is returned to the page untouched.
    try {
      const clone = response.clone();
      const text = await clone.text();
      const json = JSON.parse(text);
      _record(url, json);
    } catch (_) {
      _log(
        "[interceptor] fetch: body not JSON, skipping:",
        url,
        "(content-type:",
        response.headers.get("content-type") || "",
        ")",
      );
    }

    // Always return the original so the page behaves normally.
    return response;
  };

  _log("[interceptor] fetch: wrapper installed");

  // ── Patch XMLHttpRequest ──────────────────────────────────────────────
  // Some older or non-fetch paths still use XHR; we patch both to be safe.
  const _origOpen = XMLHttpRequest.prototype.open;
  const _origSend = XMLHttpRequest.prototype.send;
  _log("[interceptor] XHR: originals saved, installing wrappers");

  // open() is where the URL is set — stash it on the instance so send()
  // can reference it later when the response arrives.
  XMLHttpRequest.prototype.open = function (m, url, ...a) {
    _log("[interceptor] XHR open:", m, url);
    this._xurl = url;
    return _origOpen.call(this, m, url, ...a);
  };

  XMLHttpRequest.prototype.send = function (...a) {
    // Attach a load listener before the request fires so we catch the
    // response regardless of when it arrives.
    this.addEventListener("load", function () {
      try {
        const json = JSON.parse(this.responseText);
        _record(this._xurl || "", json);
      } catch (_) {
        _log(
          "[interceptor] XHR: skipped non-JSON response for",
          this._xurl || "",
        );
      }
    });
    _log("[interceptor] XHR send:", this._xurl || "");
    return _origSend.call(this, ...a);
  };

  _log("[interceptor] XHR: wrappers installed — interceptor fully active");
} else {
  _log(
    "[interceptor] already active (re-injection skipped), existing captures:",
    window._capturedResponses.length,
  );
}
