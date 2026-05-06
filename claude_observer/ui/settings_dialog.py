"""
settings_dialog.py
------------------
Dark-themed settings window that edits config.json in-place and applies
changes live via config.apply_updates().

Public API
----------
SettingsDialog(master)   — create the window (initially visible)
  .show()                — deiconify and raise
  .hide()                — withdraw without destroying
  .destroy()             — permanently destroy (called on app quit)
"""

import json
import logging
import tkinter as tk

from claude_observer import config
from claude_observer.ui.scrollbar import make_scrollbar

BG = "#1e1e23"
BG_DARK = "#13131a"
BG_ROW = "#24242e"
FG = "#c8c8d8"
FG_DIM = "#606070"
FG_HEAD = "#a0a0b0"
ACCENT = "#2a5a8a"
ACCENT_H = "#3a6a9a"
SEP = "#3a3a45"

# (key, label, widget_type, hint)
# widget_type: "str" | "int" | "bool"
_FIELDS = [
    ("__head__", "General", None, None),
    (
        "REFRESH_INTERVAL_SECONDS",
        "Refresh interval (s)",
        "int",
        "Seconds between local token-stat refreshes",
    ),
    (
        "DEBUG_LOGGING",
        "Debug logging",
        "bool",
        "Write DEBUG-level entries to the log file",
    ),
    ("__head__", "Data", None, None),
    (
        "INCLUDE_PATHS",
        "Include paths",
        "str",
        "Comma-separated path prefixes to count (empty = all)",
    ),
    (
        "EXCLUDE_WEEKDAYS",
        "Exclude weekdays",
        "str",
        "Weekday numbers excluded from rolling average (0=Mon…6=Sun)",
    ),
    ("__head__", "Browser / Account stats", None, None),
    (
        "CONSOLE_FETCHER_ENABLED",
        "Fetcher enabled",
        "bool",
        "Scrape account stats from claude.ai via CDP",
    ),
    (
        "BROWSER_DEBUG_PORT",
        "Chrome debug port",
        "int",
        "CDP remote-debugging port for Chrome",
    ),
    ("__head__", "Local LLM", None, None),
    ("LLM_URL", "LLM URL", "str", "Base URL of local llama-server / Ollama"),
    ("LLM_API_KEY", "LLM API key", "str", "API key sent to local server"),
    ("LLM_MODEL", "LLM model", "str", "Model name passed to local server"),
    (
        "LLAMA_SERVER_CMD",
        "Llama server command",
        "str",
        "Full command to launch llama-server (split on spaces)",
    ),
    (
        "LLM_LOG_MAX_LINES",
        "Log max lines",
        "int",
        "Max lines kept in the server-output log box",
    ),
]

_LABEL_W = 158  # px — fixed label column width


class SettingsDialog:
    """Persistent settings Toplevel.  Create once, hide/show as needed."""

    def __init__(self, master: tk.Tk) -> None:
        """Build the window on *master*'s Tk thread and make it visible."""
        self._master = master
        self._win = tk.Toplevel(master)
        self._entries: dict[str, tk.Variable] = {}
        self._status_var: tk.StringVar | None = None
        self._status_lbl: tk.Label | None = None
        self._build()

    # ── Public API ────────────────────────────────────────────────────────────

    def show(self) -> None:
        """Deiconify and raise the window, refreshing field values."""
        if not self._win.winfo_exists():
            return
        self._refresh_fields()
        self._win.deiconify()
        self._win.lift()
        self._win.focus_force()

    def hide(self) -> None:
        """Withdraw the window without destroying it."""
        if self._win.winfo_exists():
            self._win.withdraw()

    def destroy(self) -> None:
        """Permanently destroy the window (called on app quit)."""
        if self._win.winfo_exists():
            self._win.destroy()

    # ── Construction (runs once on the Tk thread) ─────────────────────────────

    def _build(self) -> None:
        win = self._win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG)
        win.geometry("360x3000+0+0")  # large temp height; resized after content is measured

        # ── Title bar ─────────────────────────────────────────────────────────
        bar = tk.Frame(win, bg=BG_DARK, cursor="fleur")
        bar.pack(fill="x")
        tk.Label(
            bar,
            text="Claude Usage — Settings",
            font=("Segoe UI", 10, "bold"),
            fg="#ffffff",
            bg=BG_DARK,
            anchor="w",
        ).pack(side="left", padx=12, pady=8)
        tk.Button(
            bar,
            text="✕",
            command=self.hide,
            font=("Segoe UI", 9),
            bg=BG_DARK,
            fg="#606070",
            relief="flat",
            bd=0,
            padx=8,
            cursor="hand2",
            activebackground="#e05050",
            activeforeground="#ffffff",
        ).pack(side="right", pady=4, padx=4)

        def _ds(e):
            win._dx, win._dy = e.x, e.y

        def _dm(e):
            win.geometry(
                f"+{win.winfo_x() + e.x - win._dx}+{win.winfo_y() + e.y - win._dy}"
            )

        bar.bind("<ButtonPress-1>", _ds)
        bar.bind("<B1-Motion>", _dm)

        # ── Scrollable body ───────────────────────────────────────────────────
        body = tk.Frame(win, bg=BG)
        body.pack(fill="both", expand=True)

        canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        sb = make_scrollbar(body)
        sb.configure(command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        content = tk.Frame(canvas, bg=BG)
        cw = canvas.create_window((0, 0), window=content, anchor="nw")

        def _on_content_resize(e):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_resize(e):
            canvas.itemconfig(cw, width=e.width)

        content.bind("<Configure>", _on_content_resize)
        canvas.bind("<Configure>", _on_canvas_resize)

        def _scroll(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        # Bind scroll only to this canvas so it doesn't hijack the main popup.
        canvas.bind("<Enter>", lambda _: canvas.bind_all("<MouseWheel>", _scroll))
        canvas.bind("<Leave>", lambda _: canvas.unbind_all("<MouseWheel>"))

        # ── Fields ────────────────────────────────────────────────────────────
        entries: dict[str, tk.Variable] = {}

        raw = dict(config._raw)  # snapshot of current live config

        for key, label, wtype, hint in _FIELDS:
            if key == "__head__":
                tk.Frame(content, height=1, bg=SEP).pack(fill="x", padx=12, pady=(10, 0))
                tk.Label(
                    content,
                    text=label,
                    font=("Segoe UI", 8, "bold"),
                    fg=FG_HEAD,
                    bg=BG_DARK,
                    anchor="w",
                    padx=16,
                    pady=5,
                ).pack(fill="x")
                continue

            wrap = tk.Frame(content, bg=BG_ROW)
            wrap.pack(fill="x", padx=12, pady=(1, 0))

            top = tk.Frame(wrap, bg=BG_ROW)
            top.pack(fill="x")

            lbl_cell = tk.Frame(top, bg=BG_ROW, width=_LABEL_W)
            lbl_cell.pack_propagate(False)
            lbl_cell.pack(side="left", fill="y")
            tk.Label(
                lbl_cell,
                text=label,
                font=("Segoe UI", 9),
                fg=FG,
                bg=BG_ROW,
                anchor="w",
            ).pack(side="left", padx=(10, 0), pady=(7, 0))

            inp_cell = tk.Frame(top, bg=BG_ROW)
            inp_cell.pack(side="left", fill="x", expand=True, padx=(4, 10))

            if wtype == "bool":
                var = tk.BooleanVar(master=win, value=bool(raw.get(key, False)))
                tk.Checkbutton(
                    inp_cell,
                    variable=var,
                    bg=BG_ROW,
                    fg=FG,
                    selectcolor="#2a2a38",
                    activebackground=BG_ROW,
                    activeforeground=FG,
                    relief="flat",
                    bd=0,
                    cursor="hand2",
                ).pack(side="left", pady=(7, 0))
            else:
                var = tk.StringVar(master=win, value=str(raw.get(key, "")))
                tk.Entry(
                    inp_cell,
                    textvariable=var,
                    font=("Segoe UI", 9),
                    bg="#2a2a38",
                    fg=FG,
                    insertbackground=FG,
                    relief="flat",
                    bd=4,
                    highlightthickness=1,
                    highlightbackground=SEP,
                    highlightcolor=ACCENT,
                ).pack(fill="x", pady=(5, 0))

            entries[key] = var

            if hint:
                tk.Label(
                    wrap,
                    text=hint,
                    font=("Segoe UI", 7),
                    fg=FG_DIM,
                    bg=BG_ROW,
                    anchor="w",
                ).pack(anchor="w", padx=(_LABEL_W + 14, 10), pady=(0, 5))
            else:
                tk.Frame(wrap, height=5, bg=BG_ROW).pack()

        self._entries = entries

        # ── Save / Cancel bar ─────────────────────────────────────────────────
        tk.Frame(content, height=1, bg=SEP).pack(fill="x", padx=12, pady=(10, 0))
        btn_row = tk.Frame(content, bg=BG)
        btn_row.pack(fill="x", padx=12, pady=10)

        status_var = tk.StringVar(master=win)
        self._status_var = status_var
        status_lbl = tk.Label(
            btn_row,
            textvariable=status_var,
            font=("Segoe UI", 8),
            fg="#50d490",
            bg=BG,
            anchor="w",
        )
        self._status_lbl = status_lbl
        status_lbl.pack(side="left", padx=4)

        tk.Button(
            btn_row,
            text="Cancel",
            command=self.hide,
            font=("Segoe UI", 9),
            bg="#3a3a50",
            fg=FG,
            relief="flat",
            bd=0,
            padx=14,
            pady=5,
            cursor="hand2",
            activebackground="#4a4a60",
            activeforeground="#ffffff",
        ).pack(side="right", padx=(6, 0))

        tk.Button(
            btn_row,
            text="Save",
            command=self._save,
            font=("Segoe UI", 9, "bold"),
            bg=ACCENT,
            fg="#ffffff",
            relief="flat",
            bd=0,
            padx=14,
            pady=5,
            cursor="hand2",
            activebackground=ACCENT_H,
            activeforeground="#ffffff",
        ).pack(side="right")

        # ── Size and position ─────────────────────────────────────────────────
        win.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))

        max_h = int(win.winfo_screenheight() * 0.82)
        bar_h = bar.winfo_reqheight()
        cont_h = content.winfo_reqheight()
        final_h = min(cont_h + bar_h + 4, max_h)

        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = (sw - 360) // 2
        y = (sh - final_h) // 2
        win.geometry(f"360x{final_h}+{x}+{y}")

    def _refresh_fields(self) -> None:
        """Sync field values to the current live config (called on show)."""
        raw = dict(config._raw)
        for key, _label, wtype, _hint in _FIELDS:
            if key == "__head__" or key not in self._entries:
                continue
            var = self._entries[key]
            if wtype == "bool":
                var.set(bool(raw.get(key, False)))
            else:
                var.set(str(raw.get(key, "")))

    def _save(self) -> None:
        updates: dict = {}
        errors: list[str] = []
        for key, _label, wtype, _hint in _FIELDS:
            if key == "__head__" or key not in self._entries:
                continue
            var = self._entries[key]
            if wtype == "bool":
                updates[key] = var.get()
            elif wtype == "int":
                try:
                    updates[key] = int(var.get())
                except ValueError:
                    errors.append(f"{_label}: must be an integer")
            else:
                updates[key] = var.get()

        if errors:
            self._status_var.set("Error: " + errors[0])
            return

        config.apply_updates(updates)

        try:
            root = logging.getLogger()
            f_lvl = logging.DEBUG if config.DEBUG_LOGGING else logging.INFO
            c_lvl = logging.DEBUG if config.DEBUG_LOGGING else logging.WARNING
            if root.handlers:
                root.handlers[0].setLevel(f_lvl)
            if len(root.handlers) > 1:
                root.handlers[1].setLevel(c_lvl)
        except Exception:
            pass

        self._status_var.set("Saved.")
        self._status_lbl.config(fg="#50d490")

        def _fade(step: int = 0, total: int = 15) -> None:
            if not self._win.winfo_exists():
                return
            if step > total:
                self._status_var.set("")
                self._status_lbl.config(fg="#50d490")
                return
            t = step / total
            r = int(0x50 + (0x1e - 0x50) * t)
            g = int(0xd4 + (0x1e - 0xd4) * t)
            b = int(0x90 + (0x23 - 0x90) * t)
            self._status_lbl.config(fg=f"#{r:02x}{g:02x}{b:02x}")
            self._win.after(30, lambda: _fade(step + 1, total))

        self._win.after(1200, _fade)
