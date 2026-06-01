"""
scrollbar.py
------------
Shared factory for the slim dark vertical scrollbar used across the app.
"""

from tkinter import ttk

_STYLE = "App.Vertical.TScrollbar"


def make_scrollbar(parent) -> ttk.Scrollbar:
    """Configure the shared slim scrollbar style on parent's Tk root and return the widget."""
    style = ttk.Style(parent)
    style.theme_use("default")
    style.configure(
        _STYLE,
        troughcolor="#13131a",
        background="#44445a",
        bordercolor="#13131a",
        arrowcolor="#13131a",
        relief="flat",
        borderwidth=0,
        padding=0,
        width=6,
        arrowsize=0,
    )
    style.map(
        _STYLE,
        background=[("active", "#6060a0"), ("!active", "#44445a")],
    )
    return ttk.Scrollbar(parent, orient="vertical", style=_STYLE)
