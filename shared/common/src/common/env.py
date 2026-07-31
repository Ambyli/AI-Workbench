"""Walk-up .env loader.

Use ``load_env()`` at the top of an entry-point module (before importing
anything that reads ``os.environ`` at import time) to populate the process
environment from the nearest ``.env`` file found by walking up from the
current working directory.

The walk-up behavior matters when a service lives several directories deep
in a monorepo — e.g. ``ai/roofix/scraper/app.py`` uses the repo-root ``.env``.
"""

from __future__ import annotations

from typing import Optional


def load_env(path: Optional[str] = None) -> Optional[str]:
    """Load a .env file into ``os.environ``.

    Parameters
    ----------
    path : str | None
        Explicit path to a .env file. When None (default), walks up from the
        current working directory looking for a .env.

    Returns
    -------
    str | None
        The .env path that was loaded, or None if nothing was found / loaded.

    Notes
    -----
    Values already present in the environment are NOT overridden — .env is
    a fallback source, not an override. This matches the standard dotenv
    convention and lets operators use environment injection (docker-compose,
    CI, k8s secrets) to override the file.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        # python-dotenv not installed — silent no-op so the library remains
        # importable when the optional dep is missing.
        return None

    dotenv_path = path or find_dotenv(usecwd=True)
    if not dotenv_path:
        return None
    load_dotenv(dotenv_path, override=False)
    return dotenv_path
