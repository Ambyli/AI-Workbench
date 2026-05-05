"""
Claude Usage Observer — package entry point.
Run with:  python -m claude_observer
"""

from claude_observer.logging_setup import log
from claude_observer.core.widget import ClaudeUsageWidget


def main():
    log.info("Claude Usage Widget starting up")
    ClaudeUsageWidget().run()


if __name__ == "__main__":
    main()
