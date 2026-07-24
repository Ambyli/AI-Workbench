"""Reusable logging setup.

``setup_logging(name, log_dir=None, debug=False)`` returns a configured
``logging.Logger`` with a file handler + console handler. The file handler
writes to ``<log_dir>/<name>.log`` (recreated on each call — this is the
widget's original behavior; useful during dev). If ``log_dir`` is None, only
the console handler is installed.

DEBUG_LOGGING semantics match the widget:
- File: DEBUG when debug=True, else INFO
- Console: DEBUG when debug=True, else WARNING
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional


def setup_logging(
    name: str,
    log_dir: Optional[str | Path] = None,
    debug: bool = False,
) -> logging.Logger:
    """Configure the root logger and return a named child logger.

    Parameters
    ----------
    name : str
        Name for the returned child logger. Also used as the log-file stem
        (``<log_dir>/<name>.log``) when ``log_dir`` is given.
    log_dir : str | Path | None
        Directory for the log file. Created if missing. If None, no file
        handler is installed.
    debug : bool
        Verbose mode — see module docstring for level implications.
    """
    file_level = logging.DEBUG if debug else logging.INFO
    console_level = logging.DEBUG if debug else logging.WARNING

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_dir is not None:
        log_path = Path(log_dir) / f"{name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Recreate on each call — matches widget's behavior of a fresh log
        # per session. If you want appending behavior, drop this line.
        log_path.unlink(missing_ok=True)
        handlers.insert(0, logging.FileHandler(log_path, encoding="utf-8"))

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(funcName)s: %(message)s",
        handlers=handlers,
        force=True,  # override any prior basicConfig call
    )

    root = logging.getLogger()
    # Handler order matches insertion above: file (if any) first, console last.
    if log_dir is not None:
        root.handlers[0].setLevel(file_level)
        root.handlers[1].setLevel(console_level)
    else:
        root.handlers[0].setLevel(console_level)

    return logging.getLogger(name)
