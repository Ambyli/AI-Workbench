/*
 * sandbox browser shim — captures browser-side error/warn events and
 * forwards them to sandbox-runner so `get_logs` can show them alongside
 * the container's own stdout+stderr.
 *
 * Injected by sandbox-proxy (Caddy replace-response) into every
 * text/html response — see ai/sandbox/proxies/Caddyfile.
 *
 * See ai/sandbox/SANDBOX.md § Browser console capture for the whole
 * end-to-end story including what's captured, what's NOT, and the
 * CSP known limitation.
 */
(function () {
  try {
    // ── Where do we POST events to? ────────────────────────────────────
    //
    // The sandbox URL prefix is either:
    //   /{sandbox_id}/...             (direct: http://localhost:8011/…)
    //   /sandboxes/{sandbox_id}/...   (behind oauth2-proxy)
    //
    // The runner endpoint is registered by sandbox-proxy under
    // /{sandbox_id}/_browser_log AND /sandboxes/{sandbox_id}/_browser_log
    // — both routes exist in the Caddyfile.
    //
    // We compute the prefix from location.pathname: if the first
    // segment is exactly "sandboxes", we take the first TWO segments;
    // otherwise we take just the first. Any pathname shorter than that
    // (e.g. Caddy's / catchall) means we cannot report events — bail.
    const parts = location.pathname.split("/").filter(Boolean);
    if (parts.length === 0) return;
    const prefix =
      parts[0] === "sandboxes" && parts.length >= 2
        ? "/sandboxes/" + parts[1]
        : "/" + parts[0];
    const endpoint = prefix + "/_browser_log";

    // ── Buffering ──────────────────────────────────────────────────────
    // A single sendBeacon per 100 ms burst cuts network chattiness for
    // apps that log verbosely on every render. Buffered entries flush
    // on unload too so a page nav doesn't lose the last batch.
    const buf = [];
    let scheduled = false;
    const debug = location.search.includes("_debug=1");

    function flush() {
      scheduled = false;
      if (buf.length === 0) return;
      const payload = JSON.stringify({ entries: buf.splice(0) });
      try {
        // sendBeacon is fire-and-forget, non-retrying, survives unload.
        // Blob wrapper gives us an application/json content-type; a raw
        // string would go out as text/plain and force a different code
        // path on the receiver.
        const blob = new Blob([payload], { type: "application/json" });
        navigator.sendBeacon(endpoint, blob);
      } catch (_e) {
        // Silent — never throw from the shim.
      }
    }
    function schedule() {
      if (scheduled) return;
      scheduled = true;
      setTimeout(flush, 100);
    }
    function push(level, message, meta) {
      // meta may carry {source, line, col, stack}. Undefined fields
      // aren't serialized by JSON.stringify so we don't have to filter.
      buf.push(Object.assign({ level: level, ts: Date.now(), message: message }, meta || {}));
      schedule();
    }

    // ── console wrapping ───────────────────────────────────────────────
    // Original methods are always called after forwarding so DevTools
    // still shows everything. levels array is fixed order; log/info/debug
    // are gated behind ?_debug=1.
    const alwaysLevels = ["error", "warn"];
    const debugLevels = ["log", "info", "debug"];
    const levels = alwaysLevels.concat(debug ? debugLevels : []);
    for (const level of levels) {
      const orig = console[level] && console[level].bind(console);
      if (!orig) continue;
      console[level] = function () {
        try {
          const args = Array.prototype.slice.call(arguments);
          const msg = args
            .map(function (a) {
              if (typeof a === "string") return a;
              try {
                return JSON.stringify(a);
              } catch (_e) {
                return String(a);
              }
            })
            .join(" ");
          push(level, msg);
        } catch (_e) {
          // never let the shim break the app's console call
        }
        orig.apply(console, arguments);
      };
    }

    // ── window.onerror ─────────────────────────────────────────────────
    // Catches synchronous exceptions from event handlers, inline
    // scripts, and anything else that bubbles up to the window.
    window.addEventListener("error", function (e) {
      try {
        push("error", e.message || "window error", {
          source: e.filename,
          line: e.lineno,
          col: e.colno,
          stack: e.error && e.error.stack ? String(e.error.stack) : undefined,
        });
      } catch (_e) {}
    });

    // ── unhandled promise rejections ──────────────────────────────────
    window.addEventListener("unhandledrejection", function (e) {
      try {
        const reason = e.reason;
        push("error", "unhandled rejection: " + String(reason), {
          stack: reason && reason.stack ? String(reason.stack) : undefined,
        });
      } catch (_e) {}
    });

    // Flush on unload so short-lived pages still deliver.
    window.addEventListener("pagehide", flush);
    window.addEventListener("beforeunload", flush);
  } catch (_e) {
    // Outer catch: any bug in the shim itself must not break the app.
  }
})();
