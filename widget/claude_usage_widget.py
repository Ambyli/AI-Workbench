"""
claude_usage_widget.py
-----------------------
Thin launcher — kept so the Windows startup registry entry still works.
Run this directly or via:  python -m claude_observer
"""

import sys
import os

# Ensure the sibling package is on sys.path when run as a plain script
# (e.g. from the Windows startup registry).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from claude_observer.__main__ import main

if __name__ == "__main__":
    main()
