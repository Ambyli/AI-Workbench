"""cdp_interceptor.spy — CLI utility that launches an isolated Chrome, loads
a URL, and prints every JSON response the page's fetch/XHR calls receive.

Usage
-----
    python -m cdp_interceptor.spy --url https://example.com \\
        --profile-dir "%TEMP%\\cdp_spy" [--port 9222] [--headless] \\
        [--pattern "api\\.example\\.com/v1"] [--pattern "/data\\.json"]

Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading

from cdp_interceptor.cdp_session import Capture
from cdp_interceptor.client import InterceptorClient


def main() -> int:
    p = argparse.ArgumentParser(description="Print JSON responses seen by a page's fetch/XHR.")
    p.add_argument("--url", required=True, help="Target URL to load in Chrome.")
    p.add_argument(
        "--profile-dir", required=True,
        help="Chrome user-data-dir for the isolated session. Created if missing.",
    )
    p.add_argument("--port", type=int, default=9222, help="Chrome remote-debugging port.")
    p.add_argument("--headless", action="store_true", help="Force headless launch.")
    p.add_argument(
        "--pattern", action="append", default=[],
        help="URL regex to isolate (repeatable). If omitted, prints every JSON response.",
    )
    p.add_argument("--debug", action="store_true", help="Verbose logging.")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    profile_dir = os.path.expandvars(args.profile_dir)
    stop = threading.Event()

    def _print_capture(cap: Capture) -> None:
        # Runs on the worker thread. Print without buffering issues.
        try:
            body_repr = json.dumps(cap.body, indent=2)
        except Exception:
            body_repr = repr(cap.body)
        print(f"\n[capture] {cap.url}\n{body_repr}", flush=True)

    def _on_status(status: str, error: str | None) -> None:
        if error:
            print(f"[status] {status}: {error}", file=sys.stderr, flush=True)
        else:
            print(f"[status] {status}", file=sys.stderr, flush=True)

    client = InterceptorClient(
        profile_dir=profile_dir,
        debug_port=args.port,
        debug_logging=args.debug,
        url_patterns=args.pattern or None,
        # No parse_fn: on_data won't fire, but on_capture prints everything.
        on_capture=_print_capture,
        on_status=_on_status,
        session_sentinel=False,  # spy always launches fresh
    )

    # If --headless is set, we need session_sentinel=True + an existing sentinel
    # OR we can just start visible; the spy's simplest behavior is to start
    # visible and let the caller log in. Honor --headless by forcing it.
    if args.headless:
        # Bypass the sentinel gate — the caller explicitly asked for headless.
        client._headless = True  # noqa: SLF001 — internal knob for CLI use

    print(f"Launching Chrome ({'headless' if args.headless else 'visible'}) → {args.url}", flush=True)
    print(f"Profile dir: {profile_dir}", flush=True)
    try:
        client.launch(args.url)
        # Block until Ctrl-C.
        while not stop.is_set():
            stop.wait(1)
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        client.quit()

    return 0


if __name__ == "__main__":
    sys.exit(main())
