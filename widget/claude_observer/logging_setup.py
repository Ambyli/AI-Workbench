"""
logging_setup.py
----------------
Configures the application-wide logger via the shared ``common.logging_setup``
helper. Import ``log`` from here in every other module so that all components
share the same handler configuration.
"""

from pathlib import Path

from common.logging_setup import setup_logging
from claude_observer import config as _config

log = setup_logging(
    name="claude_usage_widget",
    log_dir=Path(__file__).parent.parent,
    debug=_config.DEBUG_LOGGING,
)
