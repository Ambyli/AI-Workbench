"""
widget.py
---------
ClaudeUsageWidget — wires together the tray icon, popup window, usage parser,
and optional account-stats fetcher into a running application.

Public API
----------
ClaudeUsageWidget().run()
"""

import sys
import threading
from datetime import datetime, timedelta

from claude_observer import config
from claude_observer.logging_setup import log
from claude_observer.system.startup import add_to_startup, remove_from_startup, startup_registered
from claude_observer.ui.tray_icon import make_tray_icon
from claude_observer.browser.fetcher import BrowserLinker
from claude_observer.core.usage_parser import get_usage_summary
from claude_observer.ui.popup import UsagePopup


class ClaudeUsageWidget:
    def __init__(self):
        log.debug("Starting ClaudeUsageWidget.__init__")
        self._usage: dict | None      = None
        self._error: str | None       = None
        self._next_refresh_at: datetime | None = None
        self._stop_event              = threading.Event()

        self._fetcher = (BrowserLinker() if BrowserLinker.is_available() else None) \
            if config.CONSOLE_FETCHER_ENABLED else None
        self._settings_dialog = None  # created lazily on first open, then reused
        self._popup   = UsagePopup(
            console_available=self._fetcher is not None,
            on_link_browser=self._link_browser,
            on_go_headless=self._go_headless,
            on_go_visible=self._go_visible,
        )

        try:
            import pystray
        except ImportError as exc:
            log.error("pystray not found: %s", exc)
            print("pystray not found. Install with: pip install pystray Pillow")
            sys.exit(1)

        self._icon = pystray.Icon(
            name="claude-usage",
            icon=make_tray_icon("loading"),
            title="Claude Usage — loading...",
            menu=pystray.Menu(
                pystray.MenuItem("Show Usage", self._on_click, default=True),
                pystray.MenuItem("Refresh Now", self._refresh_now),
                pystray.MenuItem("Reposition Window", self._reposition_window),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Settings...", self._open_settings),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    lambda item: "Remove from Startup" if startup_registered() else "Add to Startup",
                    self._toggle_startup,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            ),
        )
        log.debug("Finished ClaudeUsageWidget.__init__")

    # ── Event handlers ────────────────────────────────────────────────────────

    def _open_popup(self):
        log.debug("Starting ClaudeUsageWidget._open_popup")
        self._popup.show(
            self._usage, self._error, self._next_refresh_at,
            on_refresh=self._refresh_and_reopen,
        )
        log.debug("Finished ClaudeUsageWidget._open_popup")

    def _refresh_and_reopen(self):
        log.debug("Starting ClaudeUsageWidget._refresh_and_reopen")
        self._do_refresh()
        self._popup.update(self._usage, self._error, self._next_refresh_at)
        log.debug("Finished ClaudeUsageWidget._refresh_and_reopen")

    def _on_click(self, icon, item=None):
        log.debug("Starting ClaudeUsageWidget._on_click")
        threading.Thread(target=self._open_popup, daemon=True).start()
        log.debug("Finished ClaudeUsageWidget._on_click")

    def _refresh_now(self, icon=None, item=None):
        log.debug("Starting ClaudeUsageWidget._refresh_now")
        def _do():
            log.debug("Starting ClaudeUsageWidget._refresh_now._do")
            self._popup.start_refresh_display()
            self._do_refresh()
            self._popup.update(self._usage, self._error, self._next_refresh_at)
            log.debug("Finished ClaudeUsageWidget._refresh_now._do")
        threading.Thread(target=_do, daemon=True).start()
        log.debug("Finished ClaudeUsageWidget._refresh_now")

    def _reposition_window(self, _icon=None, _item=None):
        log.debug("Starting ClaudeUsageWidget._reposition_window")
        self._popup.reposition()
        log.debug("Finished ClaudeUsageWidget._reposition_window")

    def _toggle_startup(self, icon, item):
        log.debug("Starting ClaudeUsageWidget._toggle_startup")
        if startup_registered():
            remove_from_startup()
        else:
            add_to_startup()
        log.debug("Finished ClaudeUsageWidget._toggle_startup")

    def _link_browser(self):
        """Called when the user clicks 'Link Browser' in the popup."""
        log.debug("Starting ClaudeUsageWidget._link_browser")
        if self._fetcher is not None:
            self._fetcher.launch(on_update=self._popup.apply_console)
        log.debug("Finished ClaudeUsageWidget._link_browser")

    def _go_headless(self):
        """Called when the user clicks 'Go Headless' in the popup."""
        log.debug("Starting ClaudeUsageWidget._go_headless")
        if self._fetcher is not None:
            self._fetcher.go_headless()
        log.debug("Finished ClaudeUsageWidget._go_headless")

    def _go_visible(self):
        """Called when the user clicks 'Go Visible' in the popup."""
        log.debug("Starting ClaudeUsageWidget._go_visible")
        if self._fetcher is not None:
            self._fetcher.go_visible()
        log.debug("Finished ClaudeUsageWidget._go_visible")

    def _open_settings(self, icon=None, item=None):
        log.debug("Starting ClaudeUsageWidget._open_settings")
        from claude_observer.ui.settings_dialog import SettingsDialog
        popup = self._popup

        def _on_tk():
            if self._settings_dialog is None or not self._settings_dialog._win.winfo_exists():
                self._settings_dialog = SettingsDialog(master=popup._win)
            else:
                self._settings_dialog.show()

        if not popup.schedule_on_tk(_on_tk):
            # Popup not yet created — start it in its own thread, then wait
            # for the Tk root to be ready before scheduling settings on it.
            def _open_both():
                threading.Thread(target=self._open_popup, daemon=True).start()
                import time
                for _ in range(50):  # wait up to 5 s
                    if popup._win and popup._win.winfo_exists():
                        popup.schedule_on_tk(_on_tk)
                        return
                    time.sleep(0.1)
            threading.Thread(target=_open_both, daemon=True).start()

        log.debug("Finished ClaudeUsageWidget._open_settings")

    def _quit(self, icon, item):
        log.debug("Starting ClaudeUsageWidget._quit")
        self._stop_event.set()
        from claude_observer.llm.backend import is_local_llm_active, deactivate_local_llm, stop_server
        if is_local_llm_active() and not config.KEEP_LLM_ACTIVE:
            stop_server()
            deactivate_local_llm()
            log.debug("Restored Claude API on quit")
        elif is_local_llm_active():
            stop_server()
            log.debug("Stopped server on quit (keeping LLM active)")
        if self._fetcher is not None:
            self._fetcher.quit()
        # Destroy the popup Tk root on its own thread. This cascades to all
        # Toplevel children (including the settings dialog) and causes mainloop()
        # to return, cleanly exiting the popup thread.
        win = self._popup._win
        if win is not None:
            try:
                win.after(0, win.destroy)
            except Exception:
                pass
        icon.stop()
        log.debug("Finished ClaudeUsageWidget._quit")

    # ── Data fetching ─────────────────────────────────────────────────────────

    def _do_refresh(self):
        log.debug("Starting ClaudeUsageWidget._do_refresh")
        self._icon.icon  = make_tray_icon("loading")
        self._icon.title = "Claude Usage — refreshing..."
        if self._fetcher is not None:
            self._popup.notify_cs_fetching()
            self._fetcher.fetch_now()
        try:
            self._usage  = get_usage_summary()
            self._error  = None
            d = self._usage["daily"]["total"]
            w = self._usage["weekly"]["total"]
            self._icon.icon  = make_tray_icon("ok")
            self._icon.title = f"Claude Usage\nToday: {d:,} tokens\nThis week: {w:,} tokens"
        except Exception as exc:
            log.error("Error in ClaudeUsageWidget._do_refresh: %s", exc)
            self._error      = str(exc)
            self._icon.icon  = make_tray_icon("error")
            self._icon.title = "Claude Usage — error"
        finally:
            log.debug("Entering finally block in ClaudeUsageWidget._do_refresh")
            self._next_refresh_at = datetime.now() + timedelta(seconds=config.REFRESH_INTERVAL_SECONDS)
        log.debug("Finished ClaudeUsageWidget._do_refresh")

    def _refresh_loop(self, icon):
        log.debug("Starting ClaudeUsageWidget._refresh_loop")
        icon.visible = True
        while not self._stop_event.is_set():
            self._do_refresh()
            self._stop_event.wait(config.REFRESH_INTERVAL_SECONDS)

    def run(self):
        log.debug("Starting ClaudeUsageWidget.run")
        # BrowserLinker is launched on demand via the "Link Browser" button;
        # nothing to start automatically here.
        self._icon.run(self._refresh_loop)
        log.debug("Finished ClaudeUsageWidget.run")
