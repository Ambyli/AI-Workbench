"""Module-level logger shared by the whole detector service.

Same shape as ai/classifier/logger.py: one named logger every module imports,
configured once in app.py so import order cannot decide the format.

Process flow position: imported by every other module.
"""

import logging

logger = logging.getLogger("detector")
