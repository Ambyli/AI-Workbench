"""Tests for common.cdp_interceptor.launcher OS-adaptive behavior.

We don't actually launch a browser — we just verify that find_browser dispatches
to the correct platform branch and that ChromeNotFoundError is a subclass of
BrowserNotFoundError for backwards compat.
"""

from unittest.mock import patch

from common.cdp_interceptor.launcher import (
    BrowserNotFoundError,
    ChromeNotFoundError,
    find_browser,
    find_chrome,
)


def test_chrome_not_found_error_is_browser_not_found_error():
    """Widget code raises ChromeNotFoundError by name; must remain identity-equal."""
    assert ChromeNotFoundError is BrowserNotFoundError


def test_find_chrome_delegates_to_find_browser():
    """The backwards-compat alias must produce the same result as find_browser."""
    with patch("common.cdp_interceptor.launcher.find_browser") as mock_fb:
        mock_fb.return_value = "/mocked/path"
        assert find_chrome() == "/mocked/path"
        mock_fb.assert_called_once_with(None)


def test_find_browser_uses_windows_branch_on_win32():
    """On win32, find_browser calls _find_windows_chrome, not the playwright branch."""
    with patch("common.cdp_interceptor.launcher.sys.platform", "win32"), \
         patch("common.cdp_interceptor.launcher._find_windows_chrome") as mock_win, \
         patch("common.cdp_interceptor.launcher._find_playwright_chromium") as mock_lin:
        mock_win.return_value = r"C:\fake\chrome.exe"
        result = find_browser()
        assert result == r"C:\fake\chrome.exe"
        mock_win.assert_called_once()
        mock_lin.assert_not_called()


def test_find_browser_uses_playwright_branch_on_linux():
    """On non-win32, find_browser calls _find_playwright_chromium."""
    with patch("common.cdp_interceptor.launcher.sys.platform", "linux"), \
         patch("common.cdp_interceptor.launcher._find_windows_chrome") as mock_win, \
         patch("common.cdp_interceptor.launcher._find_playwright_chromium") as mock_lin:
        mock_lin.return_value = "/fake/chromium"
        result = find_browser()
        assert result == "/fake/chromium"
        mock_lin.assert_called_once()
        mock_win.assert_not_called()
