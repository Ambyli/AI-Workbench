"""Page scripts and browser actions over a *second* CDP connection.

The capture side of this package only watches: ``run_session`` (cdp_session.py)
navigates, injects ``interceptor.js``, and collects the JSON bodies the page
fetches *by itself*. Some pages only fire the request you want after a person
types into a form and clicks a button — and on sites that sign their API calls
(Salesforce LWR adds a ``csrf-token`` header from its own runtime), a
hand-written in-page ``fetch()`` is refused. The only reliable way to get that
response is to make the page's own code send it: fill the inputs, click the
button, and let the already-installed interceptor capture what comes back.

This module does the driving. Like ``screenshot.py`` it opens its **own**
short-lived WebSocket to the same tab, so ``run_session``'s private worker
socket is untouched and every response the actions provoke flows through the
normal capture path with no change to the capture code.

Public API
----------
- ``parse_actions(list[dict], *, label, allowed_types) -> list[Action]`` —
  validate step dicts up front (raises ``ActionError``) so a bad request fails
  before a browser is started.
- ``run_actions(debug_port, actions, ..., gate, fill_origins) -> ActionsReport``
  — readiness gate, optional ``page_script``, then each step in order. Never
  raises: every failure is recorded on the report.
- ``Action``, ``ActionResult``, ``ActionsReport``, ``ActionError``,
  ``ACTION_TYPES``, ``PRESS_KEYS``.

Step types
----------
``wait_for``    selector, text?, state (visible|attached), timeout_s
``fill``        selector, text?, value, clear=True, timeout_s
``click``       selector, text?, method (mouse|js), timeout_s
``press``       key, selector?, text?, timeout_s
``select``      selector, text?, value, timeout_s
``wait``        seconds
``evaluate``    script, timeout_s
``screenshot``  format (jpeg|png|webp), quality, full_page, scale, max_height,
                timeout_s (default 30)
``scroll``      selector?, text?, to (top|bottom)?, by (px)?,
                block (start|center|end|nearest, selector-only), timeout_s —
                at least one of selector / to / by; to and by exclusive

Non-obvious behaviour
---------------------
- **``scroll`` finds the container that actually scrolls.** With only a
  ``selector`` it is ``scrollIntoView({block})`` (``start`` by default — the
  element at the top of the frame). ``to`` / ``by`` act on the element's
  scroll container (itself, or the nearest ancestor that scrolls, crossing
  shadow boundaries) or, with no selector, the page's main scroller:
  ``document.scrollingElement`` when it scrolls, else the largest visible
  ``overflow-y: auto|scroll|overlay`` element anywhere in the document or an
  open shadow root — Salesforce Lightning scrolls an inner ``<div>`` and the
  window never moves. Nothing scrolls → a no-op with ``target: null``, not a
  failure. ``click`` and ``fill`` scroll their element to the CENTRE of the
  viewport, so put a ``scroll`` right before a ``screenshot`` to frame it.
- **A failed screenshot does not stop the run.** Every other step type
  aborts the run on failure (the rest are reported ``skipped``); a
  ``screenshot`` step records ``ok=False`` with its error and the next step
  runs — it observes the page, so failing to observe it is no reason to stop
  driving it. Cancellation still stops the run, screenshot or not.
- **Screenshots ride the same side socket.** The step settles first (the
  readiness gate, capped at ~5 s and best-effort — a page that is still not
  ready is shot anyway, which is the point when a step lands on an
  unexpected page), reads ``location.href``, then ``Page.getLayoutMetrics`` →
  ``Page.captureScreenshot`` with the clip ``screenshot.build_capture_params``
  builds. The capture call's budget is whatever is left of the step's
  ``timeout_s``, not the 10 s per-RPC budget, because a tall full-page clip
  can take several seconds. ``value`` carries the image as ``data_base64``.
- **Element lookup pierces open shadow roots.** Salesforce Lightning Web
  Components (and most design systems built on custom elements) render inside
  native shadow DOM, where ``document.querySelector`` finds nothing. The
  injected helper walks ``document`` plus every open ``shadowRoot``
  recursively. Closed shadow roots and iframes are out of reach.
- **Trusted input.** ``fill`` focuses the real ``<input>``/``<textarea>``
  (descending into a custom-element host's shadow tree when the selector hit
  the host) and types with ``Input.insertText``; ``click`` dispatches
  ``Input.dispatchMouseEvent`` at the element's centre. Both produce events
  with ``isTrusted === true``, which is what LWC and React listen for.
- **Readiness gate.** No step runs until the tab is off ``about:blank``, not on
  a login URL, ``document.readyState === "complete"``, and
  ``window._fetchInterceptorActive === true`` (the guard ``interceptor.js``
  sets) — so a click can never fire before the capture hook exists. Bounded by
  ``ready_timeout_s``; while a visible browser sits on a login page this is
  also what waits for the human.
- **Navigation mid-run.** A click that submits a form can replace the
  document. The next step's lookup sees "Cannot find context" / "Execution
  context was destroyed", re-waits the readiness gate once, and retries; a
  dropped side socket is reopened on the next call.
- **Login gate.** ``gate="login"`` is the one mode that acts ON a login page —
  it is what ``InterceptorClient`` uses for ``login_actions`` when the capture
  hits a login wall. It needs only a real URL and ``readyState ===
  "complete"``: it does not refuse login URLs and does not wait for the
  capture hook. Pair it with ``fill_origins`` so a ``fill`` only types into a
  page whose ``location.origin`` is on the list (the password-manager rule).
"""

from __future__ import annotations

import base64
import json as _json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Literal, Optional, Sequence

# Reused from screenshot.py rather than refactored out of it: test_screenshot.py
# monkeypatches these names on the screenshot module. Importing them into this
# module's namespace gives this module's tests their own seam to patch.
from .screenshot import (
    CHROME_MAX_CLIP_PX,
    SCREENSHOT_FORMATS,
    ScreenshotError,
    _list_tabs,
    _open_ws,
    _Rpc,
    build_capture_params,
    pick_page_tab,
)

logger = logging.getLogger("cdp_interceptor")

ACTION_TYPES: tuple[str, ...] = (
    "wait_for", "fill", "click", "press", "select", "wait", "evaluate", "screenshot",
    "scroll",
)

DEFAULT_STEP_TIMEOUT_S = 15.0
# A full-page capture of a long document can legitimately take several seconds.
DEFAULT_SCREENSHOT_TIMEOUT_S = 30.0
MAX_STEP_SECONDS = 600.0

# Screenshot bounds — the same ones the interceptor service's request model uses.
MIN_SCREENSHOT_HEIGHT = 100
MAX_SCREENSHOT_SCALE = 2.0

# Scroll bounds — the same ones the interceptor service's request model uses.
MAX_SCROLL_BY_PX = 100000
SCROLL_TO = ("top", "bottom")
SCROLL_BLOCKS = ("start", "center", "end", "nearest")

# How long a screenshot step waits for the page to settle (readiness gate)
# before shooting it as it is.
_SCREENSHOT_SETTLE_S = 5.0

# Budget for one CDP round trip inside a step (a lookup probe, an insertText,
# one mouse event). Separate from the step's own ``timeout_s`` so a lookup that
# used up most of the step budget still leaves the act itself room to answer.
_RPC_TIMEOUT = 10.0

# Fields each step type accepts. ``None`` values are treated as absent, so a
# pydantic ``model_dump()`` of a typed request model passes straight through.
_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    #  type        required                           optional
    "wait_for": (frozenset({"selector"}), frozenset({"text", "state", "timeout_s"})),
    "fill": (frozenset({"selector", "value"}), frozenset({"text", "clear", "timeout_s"})),
    "click": (frozenset({"selector"}), frozenset({"text", "method", "timeout_s"})),
    "press": (frozenset({"key"}), frozenset({"selector", "text", "timeout_s"})),
    "select": (frozenset({"selector", "value"}), frozenset({"text", "timeout_s"})),
    "wait": (frozenset({"seconds"}), frozenset()),
    "evaluate": (frozenset({"script"}), frozenset({"timeout_s"})),
    "screenshot": (frozenset(), frozenset({"format", "quality", "full_page", "scale",
                                           "max_height", "timeout_s"})),
    "scroll": (frozenset(), frozenset({"selector", "text", "to", "by", "block", "timeout_s"})),
}

# key name → (DOM ``key``, DOM ``code``, Windows virtual key code, text).
# ``text`` is what makes Chrome emit keypress/beforeinput — Enter needs "\r"
# for an implicit form submit to happen.
PRESS_KEYS: dict[str, tuple[str, str, int, Optional[str]]] = {
    "Enter": ("Enter", "Enter", 13, "\r"),
    "Tab": ("Tab", "Tab", 9, None),
    "Escape": ("Escape", "Escape", 27, None),
    "Backspace": ("Backspace", "Backspace", 8, None),
    "Delete": ("Delete", "Delete", 46, None),
    "Space": (" ", "Space", 32, " "),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38, None),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40, None),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, None),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, None),
    "Home": ("Home", "Home", 36, None),
    "End": ("End", "End", 35, None),
    "PageUp": ("PageUp", "PageUp", 33, None),
    "PageDown": ("PageDown", "PageDown", 34, None),
}

_CONTEXT_LOST_MARKERS = (
    "Cannot find context",
    "Execution context was destroyed",
    "Cannot find default execution context",
    "Inspected target navigated or closed",
)


class ActionError(ValueError):
    """Raised by ``parse_actions`` for an unknown step type or a bad field."""


@dataclass
class Action:
    """One validated step. Only the fields its ``type`` uses are meaningful."""
    type: str
    selector: Optional[str] = None
    text: Optional[str] = None
    # Kept out of repr: a login fill's value is a resolved credential, and an
    # Action that ends up in a log line or a traceback must not carry it.
    value: Optional[str] = field(default=None, repr=False)
    state: str = "visible"            # wait_for: "visible" | "attached"
    clear: bool = True                # fill
    method: str = "mouse"             # click: "mouse" | "js"
    key: Optional[str] = None         # press
    seconds: Optional[float] = None   # wait
    script: Optional[str] = None      # evaluate
    format: str = "jpeg"              # screenshot: "jpeg" | "png" | "webp"
    quality: int = 80                 # screenshot (jpeg/webp only)
    full_page: bool = False           # screenshot
    scale: float = 1.0                # screenshot
    max_height: int = 8000            # screenshot (full_page clamp, CSS px)
    to: Optional[str] = None          # scroll: "top" | "bottom"
    by: Optional[int] = None          # scroll (CSS px, negative is up)
    block: str = "start"              # scroll, selector-only form
    timeout_s: float = DEFAULT_STEP_TIMEOUT_S


@dataclass
class ActionResult:
    """Outcome of one step. ``index`` is the position in the request's
    ``actions`` list, or ``-1`` for the ``page_script``. ``value`` is whatever
    the step reports back — the script's return value for ``evaluate`` /
    ``page_script``, element details for the DOM steps, and for
    ``screenshot`` ``{format, mime_type, width, height, full_page, bytes,
    page_url, data_base64}``."""
    index: int
    type: str
    ok: bool
    elapsed_ms: int
    error: Optional[str] = None
    value: Any = None


@dataclass
class ActionsReport:
    """Everything ``run_actions`` did. ``aborted_reason`` is ``None`` when every
    step ran (a failed ``screenshot`` step does not stop the run, so it can be
    ``ok=False`` here too); otherwise it says why the run stopped ("not ready:
    login page …", "cancelled", "actions[2] (click) failed: …"). Steps that
    never ran are listed with ``ok=False, error="skipped"``."""
    page_script: Optional[ActionResult] = None
    actions: list[ActionResult] = field(default_factory=list)
    aborted_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.aborted_reason is None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def not_run(
        cls,
        actions: Sequence[Action],
        page_script: Optional[str],
        reason: str,
    ) -> "ActionsReport":
        """A report for a run that never started (or was abandoned) — every
        requested step is listed as skipped."""
        return cls(
            page_script=_skipped(-1, "page_script") if page_script is not None else None,
            actions=[_skipped(i, a.type) for i, a in enumerate(actions)],
            aborted_reason=reason,
        )


def _skipped(index: int, type_: str) -> ActionResult:
    return ActionResult(index=index, type=type_, ok=False, elapsed_ms=0, error="skipped")


# ── Validation ───────────────────────────────────────────────────────────────

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_actions(
    raw: Optional[Iterable[dict]],
    *,
    label: str = "actions",
    allowed_types: Optional[Iterable[str]] = None,
) -> list[Action]:
    """Validate a list of step dicts and return ``Action`` objects.

    Raises ``ActionError`` naming the offending step (``<label>[i]``) on an
    unknown ``type``, a type outside ``allowed_types`` (when given), a missing
    required field, a field the type doesn't take, a wrong value type, an
    out-of-range screenshot option, an unknown ``press`` key, or a ``scroll``
    that mixes its forms. Keys whose value is ``None`` are ignored.
    """
    wanted = None if allowed_types is None else set(allowed_types)
    allowed = tuple(t for t in ACTION_TYPES if wanted is None or t in wanted)
    out: list[Action] = []
    for i, item in enumerate(raw or []):
        where = f"{label}[{i}]"
        if not isinstance(item, dict):
            raise ActionError(f"{where}: each action must be an object, got {type(item).__name__}")
        type_ = item.get("type")
        if type_ not in _FIELDS:
            raise ActionError(
                f"{where}: unknown action type {type_!r} — use one of {', '.join(allowed)}"
            )
        if type_ not in allowed:
            raise ActionError(
                f"{where}: action type {type_!r} is not allowed in {label} — "
                f"use one of {', '.join(allowed)}"
            )
        where = f"{where} ({type_})"
        required, optional = _FIELDS[type_]
        given = {k: v for k, v in item.items() if k != "type" and v is not None}
        unknown = set(given) - required - optional
        if unknown:
            raise ActionError(
                f"{where}: unexpected field(s) {', '.join(sorted(unknown))} — "
                f"{type_} takes {', '.join(sorted(required | optional))}"
            )
        missing = required - set(given)
        if missing:
            raise ActionError(f"{where}: missing required field(s) {', '.join(sorted(missing))}")

        a = Action(type=type_)
        if type_ == "screenshot":
            a.timeout_s = DEFAULT_SCREENSHOT_TIMEOUT_S
        for name in ("selector", "script"):
            if name in given:
                v = given[name]
                if not isinstance(v, str) or not v.strip():
                    raise ActionError(f"{where}: {name} must be a non-empty string")
                setattr(a, name, v)
        for name in ("text", "value"):
            if name in given:
                if not isinstance(given[name], str):
                    raise ActionError(f"{where}: {name} must be a string")
                setattr(a, name, given[name])
        if "state" in given:
            if given["state"] not in ("visible", "attached"):
                raise ActionError(f"{where}: state must be 'visible' or 'attached'")
            a.state = given["state"]
        if "method" in given:
            if given["method"] not in ("mouse", "js"):
                raise ActionError(f"{where}: method must be 'mouse' or 'js'")
            a.method = given["method"]
        if "clear" in given:
            if not isinstance(given["clear"], bool):
                raise ActionError(f"{where}: clear must be true or false")
            a.clear = given["clear"]
        if "timeout_s" in given:
            v = given["timeout_s"]
            if not _is_number(v) or not (0 < v <= MAX_STEP_SECONDS):
                raise ActionError(f"{where}: timeout_s must be a number in (0, {MAX_STEP_SECONDS:g}]")
            a.timeout_s = float(v)
        if "seconds" in given:
            v = given["seconds"]
            if not _is_number(v) or not (0 <= v <= MAX_STEP_SECONDS):
                raise ActionError(f"{where}: seconds must be a number in [0, {MAX_STEP_SECONDS:g}]")
            a.seconds = float(v)
        if "key" in given:
            k = given["key"]
            if not isinstance(k, str) or not (k in PRESS_KEYS or len(k) == 1):
                raise ActionError(
                    f"{where}: key must be one of {', '.join(PRESS_KEYS)} or a single character"
                )
            a.key = k
        if "format" in given:
            if given["format"] not in SCREENSHOT_FORMATS:
                raise ActionError(f"{where}: format must be one of {', '.join(SCREENSHOT_FORMATS)}")
            a.format = given["format"]
        if "quality" in given:
            v = given["quality"]
            if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 100):
                raise ActionError(f"{where}: quality must be an integer in [1, 100]")
            a.quality = v
        if "full_page" in given:
            if not isinstance(given["full_page"], bool):
                raise ActionError(f"{where}: full_page must be true or false")
            a.full_page = given["full_page"]
        if "scale" in given:
            v = given["scale"]
            if not _is_number(v) or not (0 < v <= MAX_SCREENSHOT_SCALE):
                raise ActionError(f"{where}: scale must be a number in (0, {MAX_SCREENSHOT_SCALE:g}]")
            a.scale = float(v)
        if "max_height" in given:
            v = given["max_height"]
            if (not isinstance(v, int) or isinstance(v, bool)
                    or not (MIN_SCREENSHOT_HEIGHT <= v <= CHROME_MAX_CLIP_PX)):
                raise ActionError(
                    f"{where}: max_height must be an integer in "
                    f"[{MIN_SCREENSHOT_HEIGHT}, {CHROME_MAX_CLIP_PX}]"
                )
            a.max_height = v
        if type_ == "scroll":
            # Only scroll takes to / by / block (the unexpected-field check
            # above refuses them elsewhere), so the form rules live here too.
            if "to" in given:
                if given["to"] not in SCROLL_TO:
                    raise ActionError(f"{where}: to must be 'top' or 'bottom'")
                a.to = given["to"]
            if "by" in given:
                v = given["by"]
                if (not isinstance(v, int) or isinstance(v, bool)
                        or not (-MAX_SCROLL_BY_PX <= v <= MAX_SCROLL_BY_PX)):
                    raise ActionError(
                        f"{where}: by must be an integer in "
                        f"[-{MAX_SCROLL_BY_PX}, {MAX_SCROLL_BY_PX}]"
                    )
                a.by = v
            if "block" in given:
                if given["block"] not in SCROLL_BLOCKS:
                    raise ActionError(f"{where}: block must be one of {', '.join(SCROLL_BLOCKS)}")
                a.block = given["block"]
            if a.text is not None and a.selector is None:
                raise ActionError(f"{where}: text needs a selector — it narrows the selector's matches")
            if a.selector is None and a.to is None and a.by is None:
                raise ActionError(f"{where}: give selector, to or by")
            if a.to is not None and a.by is not None:
                raise ActionError(f"{where}: to and by are mutually exclusive — give one")
            if "block" in given and (a.to is not None or a.by is not None):
                raise ActionError(f"{where}: block only applies to the selector-only form (no to / by)")
        out.append(a)
    return out


# ── Injected JavaScript ──────────────────────────────────────────────────────
# Every expression below is prefixed with _HELPER_JS, which (re)installs
# ``window.__ciActions`` in whatever document is current — a navigation wipes
# it, so it must not be assumed to survive between calls. The element a lookup
# found is stashed on ``window.__ciActionTarget`` for the act that follows.

_HELPER_JS = r"""(function () {
  if (window.__ciActions && window.__ciActions.v === 1) return;
  var FILLABLE = 'input:not([type=hidden]),textarea,[contenteditable=""],[contenteditable="true"]';
  function roots() {
    var out = [document], stack = [document];
    while (stack.length) {
      var all = stack.pop().querySelectorAll('*');
      for (var i = 0; i < all.length; i++) {
        var sr = all[i].shadowRoot;
        if (sr) { out.push(sr); stack.push(sr); }
      }
    }
    return out;
  }
  function deepAll(sel) {
    var rs = roots(), res = [], seen = new Set();
    for (var i = 0; i < rs.length; i++) {
      var m = rs[i].querySelectorAll(sel);
      for (var j = 0; j < m.length; j++) {
        if (!seen.has(m[j])) { seen.add(m[j]); res.push(m[j]); }
      }
    }
    return res;
  }
  function deepFirst(root, sel) {
    var queue = [root];
    while (queue.length) {
      var r = queue.shift();
      var hit = r.querySelector(sel);
      if (hit) return hit;
      var all = r.querySelectorAll('*');
      for (var i = 0; i < all.length; i++) if (all[i].shadowRoot) queue.push(all[i].shadowRoot);
    }
    return null;
  }
  function control(el, sel) {
    if (el.matches(sel)) return el;
    return (el.shadowRoot && deepFirst(el.shadowRoot, sel)) || deepFirst(el, sel);
  }
  function boxVisible(el) {
    var r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return false;
    var cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
  }
  function kids(el) {
    return Array.prototype.slice.call(el.children).concat(
      el.shadowRoot ? Array.prototype.slice.call(el.shadowRoot.children) : []);
  }
  function rectOf(el) {
    if (boxVisible(el)) return el.getBoundingClientRect();
    if (getComputedStyle(el).display === 'contents') {
      var k = kids(el);
      for (var i = 0; i < k.length; i++) { var r = rectOf(k[i]); if (r) return r; }
    }
    return null;
  }
  var SKIP = { STYLE: 1, SCRIPT: 1, TEMPLATE: 1 };
  function deepText(el, depth) {
    var s = '';
    try { s = el.innerText || ''; } catch (e) {}
    if (depth > 8) return s;
    var hosts = [el].concat(Array.prototype.slice.call(el.querySelectorAll('*')));
    for (var i = 0; i < hosts.length; i++) {
      var sr = hosts[i].shadowRoot;
      if (!sr) continue;
      for (var k = 0; k < sr.children.length; k++) {
        if (!SKIP[sr.children[k].tagName]) s += ' ' + deepText(sr.children[k], depth + 1);
      }
    }
    return s;
  }
  function texts(el) {
    var out = [deepText(el, 0)];
    if (typeof el.value === 'string') out.push(el.value);
    ['aria-label', 'placeholder', 'title'].forEach(function (a) {
      var v = el.getAttribute(a);
      if (v) out.push(v);
    });
    return out.map(function (s) { return String(s).replace(/\s+/g, ' ').trim(); });
  }
  function describe(el) {
    var d = '<' + el.tagName.toLowerCase();
    if (el.id) d += ' id="' + el.id + '"';
    var n = el.getAttribute('name');
    if (n) d += ' name="' + n + '"';
    return d + '>';
  }
  function deepActive() {
    var a = document.activeElement;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
    return a;
  }
  function find(sel, text, state) {
    var els;
    try { els = deepAll(sel); } catch (e) {
      return { error: 'invalid selector ' + JSON.stringify(sel) + ': ' + e.message };
    }
    var total = els.length;
    if (text !== null && text !== '') {
      var m = /^\/([\s\S]*)\/([a-z]*)$/.exec(text), rx = null;
      if (m) {
        try { rx = new RegExp(m[1], m[2].replace(/[gy]/g, '')); } catch (e) {
          return { error: 'invalid text regex ' + text + ': ' + e.message };
        }
      }
      var needle = text.replace(/\s+/g, ' ').trim();
      els = els.filter(function (el) {
        return texts(el).some(function (t) { return rx ? rx.test(t) : t.indexOf(needle) >= 0; });
      });
    }
    var hit = null, vis = 0;
    for (var i = 0; i < els.length; i++) {
      if (rectOf(els[i])) { vis++; if (!hit) hit = els[i]; }
    }
    if (!hit && state === 'attached' && els.length) hit = els[0];
    if (!hit) {
      var why = total === 0 ? 'nothing matches selector ' + JSON.stringify(sel)
        : (text ? total + ' matched the selector, ' + els.length + ' of those matched the text'
                : total + ' matched the selector');
      if (els.length) why += ', none visible';
      return { ok: false, reason: why };
    }
    window.__ciActionTarget = hit;
    return { ok: true, matched: els.length, visible: vis, element: describe(hit) };
  }
  window.__ciActions = {
    v: 1, FILLABLE: FILLABLE, find: find, control: control, rectOf: rectOf,
    describe: describe, deepActive: deepActive
  };
})();
"""

# Readiness probe. Deliberately NOT prefixed with the helper: it must stay
# cheap, and it runs while the document may still be about:blank.
_PROBE_JS = (
    "({href: location.href, rs: document.readyState, "
    "hook: window._fetchInterceptorActive === true})"
)

# fill_origins check, run before a fill touches the page.
_ORIGIN_JS = "location.origin"

# Where a screenshot step found the tab — after any redirect an earlier step caused.
_HREF_JS = "location.href"

_FIND_JS = "window.__ciActions.find"

_TARGET_GONE = (
    "if (!el || !el.isConnected) "
    "return { error: 'the matched element is gone (page re-rendered or navigated)' };"
)

_FILL_PREP_JS = r"""(function (clear) {
  var A = window.__ciActions, el = window.__ciActionTarget;
  """ + _TARGET_GONE + r"""
  var t = A.control(el, A.FILLABLE);
  if (!t) return { error: 'no input/textarea in or under ' + A.describe(el) };
  if (t.disabled || t.readOnly) return { error: A.describe(t) + ' is disabled or read-only' };
  t.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  t.focus();
  if (clear) {
    if (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA') {
      var proto = t.tagName === 'INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(t, '');
    } else {
      t.textContent = '';
    }
    t.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
  }
  window.__ciActionControl = t;
  return { focused: A.deepActive() === t, element: A.describe(t), origin: location.origin };
})"""

_FILL_DONE_JS = r"""(function () {
  var t = window.__ciActionControl;
  if (!t || !t.isConnected) return { error: 'the input went away while typing' };
  t.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
  t.blur();
  var v = (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA') ? t.value : t.textContent;
  return { element: window.__ciActions.describe(t), value_length: (v || '').length };
})"""

_CLICK_PREP_JS = r"""(function () {
  var A = window.__ciActions, el = window.__ciActionTarget;
  """ + _TARGET_GONE + r"""
  el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  var r = A.rectOf(el);
  if (!r) return { error: A.describe(el) + ' has no box to click' };
  return { x: r.left + r.width / 2, y: r.top + r.height / 2, element: A.describe(el) };
})"""

_CLICK_JS_JS = r"""(function () {
  var A = window.__ciActions, el = window.__ciActionTarget;
  """ + _TARGET_GONE + r"""
  el.click();
  return { element: A.describe(el) };
})"""

_FOCUS_JS = r"""(function () {
  var A = window.__ciActions, el = window.__ciActionTarget;
  """ + _TARGET_GONE + r"""
  var t = A.control(el, A.FILLABLE) || el;
  t.focus();
  return { focused: A.deepActive() === t, element: A.describe(t) };
})"""

_SELECT_JS = r"""(function (want) {
  var A = window.__ciActions, el = window.__ciActionTarget;
  """ + _TARGET_GONE + r"""
  var s = A.control(el, 'select');
  if (!s) return { error: 'no <select> in or under ' + A.describe(el) };
  var opts = Array.prototype.slice.call(s.options), o = null, i;
  for (i = 0; i < opts.length && !o; i++) if (opts[i].value === want) o = opts[i];
  for (i = 0; i < opts.length && !o; i++) if (opts[i].text.trim() === want) o = opts[i];
  if (!o) return { error: 'no option with value or text ' + JSON.stringify(want) + ' (options: ' +
    opts.slice(0, 20).map(function (x) { return JSON.stringify(x.value); }).join(', ') + ')' };
  s.focus();
  s.value = o.value;
  s.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
  s.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
  return { element: A.describe(s), value: s.value };
})"""

# The scroll step's container choice lives entirely in here, not in
# _HELPER_JS, so the helper's ``v: 1`` contract is unchanged. "Scrolls" means
# overflowing content in a box that lets it scroll: the document's
# scrollingElement, or an element with overflow-y auto / scroll / overlay.
# Slotted elements climb through their assigned <slot> (the composed tree is
# what scrolls them); a shadow root's top climbs to its host.
_SCROLL_JS = r"""(function (useTarget, to, by, block) {
  var A = window.__ciActions, el = null;
  if (useTarget) {
    el = window.__ciActionTarget;
    """ + _TARGET_GONE + r"""
  }
  var se = document.scrollingElement || document.documentElement;
  function scrolls(n) {
    if (!n || n.nodeType !== 1 || !(n.scrollHeight > n.clientHeight + 1)) return false;
    if (n === se) return true;
    var oy = getComputedStyle(n).overflowY;
    return oy === 'auto' || oy === 'scroll' || oy === 'overlay';
  }
  function up(n) {
    if (n.assignedSlot) return n.assignedSlot;
    if (n.parentElement) return n.parentElement;
    var r = n.getRootNode && n.getRootNode();
    return (r && r.host) || null;
  }
  function shownArea(n) {
    var r = A.rectOf(n);
    if (!r) return 0;
    var w = Math.min(r.right, innerWidth) - Math.max(r.left, 0);
    var h = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0);
    return w > 0 && h > 0 ? w * h : 0;
  }
  function mainScroller() {
    if (scrolls(se)) return se;
    var best = null, most = 0, stack = [document];
    while (stack.length) {
      var all = stack.pop().querySelectorAll('*');
      for (var i = 0; i < all.length; i++) {
        var n = all[i];
        if (n.shadowRoot) stack.push(n.shadowRoot);
        if (!scrolls(n)) continue;
        var a = shownArea(n);
        if (a > most) { best = n; most = a; }
      }
    }
    return best;
  }
  function containerOf(n) {
    for (var c = n; c; c = up(c)) if (scrolls(c)) return c;
    return mainScroller();
  }
  function boxed(n) {
    if (getComputedStyle(n).display !== 'contents') return n;
    var k = Array.prototype.slice.call(n.children).concat(
      n.shadowRoot ? Array.prototype.slice.call(n.shadowRoot.children) : []);
    for (var i = 0; i < k.length; i++) { var b = boxed(k[i]); if (A.rectOf(b)) return b; }
    return n;
  }
  function label(n) {
    if (n === se) return 'window';
    var d = A.describe(n), c = typeof n.className === 'string'
      ? n.className.trim().split(/\s+/).slice(0, 3).join(' ') : '';
    return c ? d.slice(0, -1) + ' class="' + c + '">' : d;
  }
  var t;
  if (el && to === null && by === null) {
    boxed(el).scrollIntoView({ block: block, inline: 'nearest', behavior: 'instant' });
    t = containerOf(el);
  } else {
    t = el ? containerOf(el) : mainScroller();
    if (t && to !== null) t.scrollTo({ top: to === 'top' ? 0 : t.scrollHeight, behavior: 'instant' });
    if (t && by !== null) t.scrollBy({ top: by, behavior: 'instant' });
  }
  var m = t || se, out = {
    target: t ? label(t) : null,
    scroll_top: Math.round(m.scrollTop),
    scroll_height: m.scrollHeight,
    client_height: m.clientHeight,
    at_bottom: m.scrollTop + m.clientHeight >= m.scrollHeight - 1
  };
  if (el) out.element = A.describe(el);
  return out;
})"""


def _call_js(fn_src: str, *args: Any) -> str:
    """``_HELPER_JS`` + ``fn_src(args…)`` with the args JSON-encoded, so user
    strings (selectors, values) never need escaping by hand."""
    return _HELPER_JS + fn_src + "(" + ", ".join(_json.dumps(a) for a in args) + ")"


# ── Errors used inside a run (never escape run_actions) ──────────────────────

class _StepFailure(Exception):
    """A step failed; the message is what lands in ``ActionResult.error``."""


class _NotReady(_StepFailure):
    """Chrome/tab not reachable yet (debug endpoint down, no page tab)."""


class _Disconnected(_StepFailure):
    """The side WebSocket dropped; the next call reconnects."""


class _ContextLost(_StepFailure):
    """The page's execution context went away — almost always a navigation."""


class _JsError(_StepFailure):
    """The evaluated script threw."""


class _Cancelled(Exception):
    pass


def _is_context_lost(msg: str) -> bool:
    return any(m in msg for m in _CONTEXT_LOST_MARKERS)


def _exception_text(details: dict) -> str:
    exc = details.get("exception") or {}
    text = exc.get("description")
    if not text and "value" in exc:
        text = f"Uncaught {exc.get('value')!r}"
    text = text or details.get("text") or "script threw"
    return text if len(text) <= 1000 else text[:1000] + "…"


class _Side:
    """The side WebSocket to the tab, reopened lazily after a drop."""

    def __init__(self, debug_port: int, url_hint: str) -> None:
        self._port = debug_port
        self._hint = url_hint
        self._ws = None
        self._rpc: Optional[_Rpc] = None

    def _connect(self) -> None:
        try:
            tabs = _list_tabs(self._port, timeout=3.0)
        except Exception as exc:
            raise _NotReady(f"Chrome debug endpoint on port {self._port} not reachable: {exc}") from exc
        tab = pick_page_tab(tabs, self._hint)
        if tab is None or not tab.get("webSocketDebuggerUrl"):
            raise _NotReady("no page tab in the debug-controlled Chrome")
        try:
            ws = _open_ws(tab["webSocketDebuggerUrl"], timeout=10.0)
        except Exception as exc:
            raise _NotReady(f"cannot attach to the tab: {exc}") from exc
        self._ws, self._rpc = ws, _Rpc(ws)

    def close(self) -> None:
        ws, self._ws, self._rpc = self._ws, None, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def call(self, method: str, params: Optional[dict] = None, *, timeout: float) -> dict:
        if self._rpc is None:
            self._connect()
        try:
            return self._rpc.call(method, params, timeout=timeout)
        except ScreenshotError as exc:
            msg = str(exc)
            if "websocket read failed" in msg:
                self.close()
                raise _Disconnected(msg) from exc
            if _is_context_lost(msg):
                raise _ContextLost(msg) from exc
            raise _StepFailure(msg) from exc
        except Exception as exc:  # send() on a closed socket and friends
            self.close()
            raise _Disconnected(f"{method}: websocket send failed: {exc}") from exc

    def evaluate(
        self,
        expression: str,
        *,
        timeout: float,
        await_promise: bool = False,
        user_gesture: bool = False,
    ) -> Any:
        params: dict[str, Any] = {"expression": expression, "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        if user_gesture:
            params["userGesture"] = True
        res = self.call("Runtime.evaluate", params, timeout=timeout)
        details = res.get("exceptionDetails")
        if details:
            text = _exception_text(details)
            if _is_context_lost(text):
                raise _ContextLost(text)
            raise _JsError(text)
        return (res.get("result") or {}).get("value")


@dataclass
class _Ctx:
    side: _Side
    login_res: list[re.Pattern]
    cancel: Callable[[], bool]
    poll: float
    gate: str = "page"                            # "page" | "login"
    fill_origins: Optional[frozenset[str]] = None  # None = no origin check

    def nap(self, seconds: float) -> None:
        """Sleep that wakes for cancel within ~50 ms."""
        end = time.monotonic() + seconds
        while True:
            if self.cancel():
                raise _Cancelled()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.05, left))


def _rpc_budget(deadline: float) -> float:
    return max(0.5, min(_RPC_TIMEOUT, deadline - time.monotonic()))


def _wait_ready(ctx: _Ctx, deadline: float) -> Optional[str]:
    """Block until the readiness gate passes (returns ``None``), the deadline
    passes (returns the last reason it wasn't ready), or cancel (returns
    ``"cancelled"``).

    ``ctx.gate == "login"`` drops the login-URL refusal and the capture-hook
    requirement: login steps run ON the login page, which has no reason to
    carry ``interceptor.js`` and every reason to match ``login_res``."""
    login_gate = ctx.gate == "login"
    reason = "not checked yet"
    while True:
        if ctx.cancel():
            return "cancelled"
        try:
            st = ctx.side.evaluate(_PROBE_JS, timeout=_rpc_budget(deadline))
            st = st if isinstance(st, dict) else {}
            href = st.get("href") or ""
            if not href or href == "about:blank":
                reason = f"tab still on {href or 'an empty URL'}"
            elif login_gate:
                if st.get("rs") == "complete":
                    return None
                reason = f"document.readyState={st.get('rs')!r} at {href}"
            elif any(rx.search(href) for rx in ctx.login_res):
                reason = f"login page {href}"
            elif st.get("rs") != "complete":
                reason = f"document.readyState={st.get('rs')!r} at {href}"
            elif not st.get("hook"):
                reason = f"capture hook (interceptor.js) not installed yet at {href}"
            else:
                return None
        except _StepFailure as exc:
            reason = str(exc)
        if time.monotonic() >= deadline:
            return reason
        try:
            ctx.nap(ctx.poll)
        except _Cancelled:
            return "cancelled"


def _lookup(ctx: _Ctx, a: Action, state: str) -> dict:
    """Poll the deep query until it finds a match in ``state``, or fail with
    the last reason once ``a.timeout_s`` is spent. On a lost context (the page
    navigated) it re-waits the readiness gate once, then keeps polling."""
    deadline = time.monotonic() + a.timeout_s
    expr = _call_js(_FIND_JS, a.selector, a.text, state)
    regated = False
    last = "not looked up yet"
    while True:
        if ctx.cancel():
            raise _Cancelled()
        try:
            res = ctx.side.evaluate(expr, timeout=_rpc_budget(deadline))
        except (_ContextLost, _Disconnected, _NotReady) as exc:
            last = str(exc)
            if not regated:
                regated = True
                if _wait_ready(ctx, deadline) == "cancelled":
                    raise _Cancelled()
                continue
        except _JsError:
            raise
        except _StepFailure as exc:  # an RPC timeout — the page may be busy
            last = str(exc)
        else:
            if not isinstance(res, dict):
                last = f"unexpected lookup result {res!r}"
            elif res.get("error"):
                raise _StepFailure(res["error"])
            elif res.get("ok"):
                return res
            else:
                last = res.get("reason") or "not found"
        if time.monotonic() >= deadline:
            what = repr(a.selector) + (f" with text {a.text!r}" if a.text else "")
            raise _StepFailure(f"timed out after {a.timeout_s:g}s waiting for {what} ({state}): {last}")
        ctx.nap(ctx.poll)


def _checked(res: Any) -> dict:
    if not isinstance(res, dict):
        raise _StepFailure(f"unexpected result from the page: {res!r}")
    if res.get("error"):
        raise _StepFailure(res["error"])
    return res


# ── Step executors ───────────────────────────────────────────────────────────

def _do_wait_for(ctx: _Ctx, a: Action) -> Any:
    hit = _lookup(ctx, a, a.state)
    return {"element": hit.get("element"), "matched": hit.get("matched"), "visible": hit.get("visible")}


def _check_fill_origin(ctx: _Ctx, origin: Any) -> None:
    if ctx.fill_origins is not None and origin not in ctx.fill_origins:
        raise _StepFailure(f"origin {origin} not allowed for this fill")


def _do_fill(ctx: _Ctx, a: Action) -> Any:
    if ctx.fill_origins is not None:
        # Before anything touches the page — a wrong-origin page never even
        # has a field focused or cleared. Checked again in the prep below,
        # which runs atomically with the focus, right before the typing.
        _check_fill_origin(ctx, ctx.side.evaluate(_ORIGIN_JS, timeout=_RPC_TIMEOUT))
    _lookup(ctx, a, "visible")
    prep = _checked(ctx.side.evaluate(_call_js(_FILL_PREP_JS, a.clear), timeout=_RPC_TIMEOUT))
    _check_fill_origin(ctx, prep.get("origin"))
    if not prep.get("focused"):
        raise _StepFailure(f"could not focus {prep.get('element')} — another element kept focus")
    if a.value:
        # Trusted text input: fires beforeinput/input exactly as typing would.
        ctx.side.call("Input.insertText", {"text": a.value}, timeout=_RPC_TIMEOUT)
    done = _checked(ctx.side.evaluate(_call_js(_FILL_DONE_JS), timeout=_RPC_TIMEOUT))
    return {"element": done.get("element"), "value_length": done.get("value_length")}


def _do_click(ctx: _Ctx, a: Action) -> Any:
    _lookup(ctx, a, "visible")
    if a.method == "js":
        return _checked(ctx.side.evaluate(_call_js(_CLICK_JS_JS), timeout=_RPC_TIMEOUT))
    box = _checked(ctx.side.evaluate(_call_js(_CLICK_PREP_JS), timeout=_RPC_TIMEOUT))
    x, y = float(box["x"]), float(box["y"])
    for params in (
        {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0},
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1},
        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1},
    ):
        ctx.side.call("Input.dispatchMouseEvent", params, timeout=_RPC_TIMEOUT)
    return {"element": box.get("element"), "x": round(x, 1), "y": round(y, 1)}


def _do_press(ctx: _Ctx, a: Action) -> Any:
    out: dict[str, Any] = {"key": a.key}
    if a.selector:
        _lookup(ctx, a, "visible")
        focus = _checked(ctx.side.evaluate(_call_js(_FOCUS_JS), timeout=_RPC_TIMEOUT))
        if not focus.get("focused"):
            raise _StepFailure(f"could not focus {focus.get('element')} before pressing {a.key}")
        out["element"] = focus.get("element")
    if a.key in PRESS_KEYS:
        key, code, vk, text = PRESS_KEYS[a.key]
    else:  # a single printable character
        key, code, text = a.key, "", a.key
        vk = ord(a.key.upper()) if a.key.isalnum() and a.key.isascii() else 0
    down: dict[str, Any] = {
        "type": "keyDown" if text else "rawKeyDown",
        "key": key, "code": code,
        "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
    }
    if text:
        down["text"] = text
        down["unmodifiedText"] = text
    ctx.side.call("Input.dispatchKeyEvent", down, timeout=_RPC_TIMEOUT)
    ctx.side.call(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "key": key, "code": code,
         "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk},
        timeout=_RPC_TIMEOUT,
    )
    return out


def _do_select(ctx: _Ctx, a: Action) -> Any:
    _lookup(ctx, a, "visible")
    return _checked(ctx.side.evaluate(_call_js(_SELECT_JS, a.value), timeout=_RPC_TIMEOUT))


def _do_wait(ctx: _Ctx, a: Action) -> Any:
    ctx.nap(a.seconds or 0.0)
    return None


def _evaluate_script(ctx: _Ctx, script: str, timeout_s: float) -> Any:
    """Run caller JS as given — an expression. Multi-statement code goes in an
    async IIFE: ``(async () => { …; return x; })()``. Promises are awaited;
    the (JSON-serialisable) result is returned by value.

    If the document goes away under the script (the gate can pass on a
    post-login landing page moments before the capture session re-navigates
    to the target), the gate is re-waited once and the script re-run on the
    new document — the one it was inspecting no longer exists."""
    try:
        return ctx.side.evaluate(script, timeout=timeout_s, await_promise=True, user_gesture=True)
    except (_ContextLost, _Disconnected) as exc:
        logger.debug("actions: script lost its document (%s) — re-gating and retrying once", exc)
        not_ready = _wait_ready(ctx, time.monotonic() + timeout_s)
        if not_ready == "cancelled":
            raise _Cancelled()
        if not_ready is not None:
            raise _StepFailure(f"{exc}; page not ready again within {timeout_s:g}s: {not_ready}") from exc
        return ctx.side.evaluate(script, timeout=timeout_s, await_promise=True, user_gesture=True)


def _do_evaluate(ctx: _Ctx, a: Action) -> Any:
    return _evaluate_script(ctx, a.script or "", a.timeout_s)


def _do_screenshot(ctx: _Ctx, a: Action) -> Any:
    """Image of the page as it is now. Settle first — the previous step may
    have been a click that navigated — but only best-effort: a page that is
    still not ready (or sits on a login page) is shot anyway, because seeing
    it is the reason the step is there."""
    deadline = time.monotonic() + a.timeout_s
    not_ready = _wait_ready(ctx, min(deadline, time.monotonic() + _SCREENSHOT_SETTLE_S))
    if not_ready == "cancelled":
        raise _Cancelled()
    if not_ready is not None:
        logger.debug("actions: screenshot taken before the page was ready: %s", not_ready)
    try:
        page_url = ctx.side.evaluate(_HREF_JS, timeout=_rpc_budget(deadline))
    except (_ContextLost, _Disconnected) as exc:
        # The document went away between the settle and the read — it is
        # mid-navigation. Settle once more and read the new one.
        logger.debug("actions: screenshot lost its document (%s) — settling again", exc)
        if _wait_ready(ctx, min(deadline, time.monotonic() + _SCREENSHOT_SETTLE_S)) == "cancelled":
            raise _Cancelled()
        page_url = ctx.side.evaluate(_HREF_JS, timeout=_rpc_budget(deadline))
    metrics = ctx.side.call("Page.getLayoutMetrics", timeout=_rpc_budget(deadline))
    try:
        params, width, height = build_capture_params(
            metrics, format=a.format, quality=a.quality, full_page=a.full_page,
            scale=a.scale, max_height=a.max_height,
        )
    except ScreenshotError as exc:
        raise _StepFailure(str(exc)) from exc
    if ctx.cancel():
        raise _Cancelled()
    # The rest of the step's budget, not _RPC_TIMEOUT: a tall full-page clip
    # can take several seconds to encode.
    shot = ctx.side.call("Page.captureScreenshot", params,
                         timeout=max(0.5, deadline - time.monotonic()))
    b64 = shot.get("data")
    if not b64:
        raise _StepFailure("Page.captureScreenshot returned no image data")
    try:
        size = len(base64.b64decode(b64, validate=True))
    except Exception as exc:
        raise _StepFailure(f"could not decode screenshot payload: {exc}") from exc
    return {
        "format": a.format,
        "mime_type": f"image/{a.format}",
        "width": width,
        "height": height,
        "full_page": a.full_page,
        "bytes": size,
        "page_url": page_url if isinstance(page_url, str) else "",
        "data_base64": b64,
    }


def _do_scroll(ctx: _Ctx, a: Action) -> Any:
    """Frame an element, jump to top/bottom, or nudge by pixels — on whichever
    container actually scrolls (see the module docstring). Only the lookup
    is bounded by ``timeout_s``; the scroll itself is one round trip."""
    if a.selector:
        _lookup(ctx, a, "visible")
    return _checked(ctx.side.evaluate(
        _call_js(_SCROLL_JS, bool(a.selector), a.to, a.by, a.block), timeout=_RPC_TIMEOUT,
    ))


_EXECUTORS: dict[str, Callable[[_Ctx, Action], Any]] = {
    "wait_for": _do_wait_for,
    "fill": _do_fill,
    "click": _do_click,
    "press": _do_press,
    "select": _do_select,
    "wait": _do_wait,
    "evaluate": _do_evaluate,
    "screenshot": _do_screenshot,
    "scroll": _do_scroll,
}


def _run_one(ctx: _Ctx, index: int, type_: str, fn: Callable[[], Any]) -> ActionResult:
    t0 = time.monotonic()

    def done(ok: bool, error: Optional[str] = None, value: Any = None) -> ActionResult:
        ms = int(round((time.monotonic() - t0) * 1000))
        return ActionResult(index=index, type=type_, ok=ok, elapsed_ms=ms, error=error, value=value)

    try:
        if ctx.cancel():
            raise _Cancelled()
        return done(True, value=fn())
    except _Cancelled:
        return done(False, "cancelled")
    except _StepFailure as exc:
        return done(False, str(exc) or type(exc).__name__)
    except Exception as exc:  # a bug here must not take the capture down
        logger.warning("actions: step %d (%s) raised unexpectedly: %r", index, type_, exc)
        return done(False, f"{type(exc).__name__}: {exc}")


# ── Entry point ──────────────────────────────────────────────────────────────

def run_actions(
    debug_port: int,
    actions: Sequence[Action],
    *,
    page_script: Optional[str] = None,
    tab_url_hint: str = "",
    login_url_patterns: Iterable[str | re.Pattern] = ("login", "signin", "/auth"),
    ready_timeout_s: float = 60.0,
    page_script_timeout_s: float = 30.0,
    cancel: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    poll_interval_s: float = 0.25,
    gate: Literal["page", "login"] = "page",
    fill_origins: Optional[Sequence[str]] = None,
    label: str = "actions",
) -> ActionsReport:
    """Drive the page tab of the Chrome on ``debug_port``.

    Order: readiness gate (up to ``ready_timeout_s``) → ``page_script`` (if
    given, awaited up to ``page_script_timeout_s``) → each action in order.
    Stops at the first failure; the remaining steps are reported as
    ``skipped`` — except after a ``screenshot`` step, whose failure is
    recorded and the next step runs. ``cancel`` is polled between and inside
    steps — return True to stop within ~50 ms (a single in-flight CDP call can
    still take up to its own timeout). ``on_progress(done, total)`` fires
    after each action.

    ``login_url_patterns`` are the same regexes the capture uses to spot a
    login wall; with the default ``gate="page"`` nothing runs while the tab
    matches one. ``gate="login"`` is for steps that are MEANT to run on the
    login page (see the module docstring): it waits only for a real URL and
    ``readyState === "complete"``, also when re-gating after a navigation.

    ``fill_origins``, when given, restricts every ``fill`` to a tab whose
    ``location.origin`` is in the list; anything else fails the step with
    ``origin <o> not allowed for this fill`` before a character is typed.
    ``label`` names the list in ``aborted_reason`` (``login_actions[1] (fill)
    failed: …``). Never raises.
    """
    actions = list(actions)
    cancel = cancel or (lambda: False)
    report = ActionsReport()
    try:
        login_res = [re.compile(p) if isinstance(p, str) else p for p in login_url_patterns]
    except re.error as exc:
        return ActionsReport.not_run(actions, page_script, f"invalid login_url_pattern: {exc}")

    ctx = _Ctx(side=_Side(debug_port, tab_url_hint), login_res=login_res,
               cancel=cancel, poll=max(0.01, poll_interval_s),
               gate="login" if gate == "login" else "page",
               fill_origins=None if fill_origins is None else frozenset(fill_origins))

    def abort(reason: str, from_index: int) -> ActionsReport:
        report.aborted_reason = reason
        report.actions.extend(_skipped(i, actions[i].type) for i in range(from_index, len(actions)))
        return report

    try:
        not_ready = _wait_ready(ctx, time.monotonic() + ready_timeout_s)
        if not_ready is not None:
            reason = "cancelled" if not_ready == "cancelled" else (
                f"not ready: {not_ready} (gave up after {ready_timeout_s:g}s)"
            )
            if page_script is not None:
                report.page_script = _skipped(-1, "page_script")
            return abort(reason, 0)

        if page_script is not None:
            res = _run_one(ctx, -1, "page_script",
                           lambda: _evaluate_script(ctx, page_script, page_script_timeout_s))
            report.page_script = res
            if not res.ok:
                return abort("cancelled" if res.error == "cancelled"
                             else f"page_script failed: {res.error}", 0)

        for i, a in enumerate(actions):
            res = _run_one(ctx, i, a.type, lambda a=a: _EXECUTORS[a.type](ctx, a))
            report.actions.append(res)
            if on_progress is not None:
                try:
                    on_progress(i + 1, len(actions))
                except Exception as exc:
                    logger.debug("actions: on_progress raised: %s", exc)
            # A failed screenshot is recorded and the run goes on — it only
            # observes the page. Cancellation stops the run whatever the step.
            if not res.ok and not (a.type == "screenshot" and res.error != "cancelled"):
                return abort("cancelled" if res.error == "cancelled"
                             else f"{label}[{i}] ({a.type}) failed: {res.error}", i + 1)
        return report
    except Exception as exc:  # belt and braces — the contract is "never raises"
        logger.warning("actions: run aborted unexpectedly: %r", exc)
        return abort(f"internal error: {type(exc).__name__}: {exc}", len(report.actions))
    finally:
        ctx.side.close()


__all__ = [
    "ACTION_TYPES",
    "PRESS_KEYS",
    "Action",
    "ActionError",
    "ActionResult",
    "ActionsReport",
    "parse_actions",
    "run_actions",
]
