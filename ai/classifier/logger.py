"""Shared logger instance for the entire classifier service.

All modules import `logger` from here rather than creating their own via
logging.getLogger(__name__).  This means:
  - One logger name ("classifier") appears in every log line.
  - A single setLevel() call in main.py controls verbosity everywhere.
  - The RequestIDFilter (middleware.py) attaches the correlation ID to every
    record, regardless of which module emits it.

Process flow position: imported by every module that needs logging.
"""

import logging

logger = logging.getLogger("classifier")
