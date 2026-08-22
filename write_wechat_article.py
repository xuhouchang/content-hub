#!/usr/bin/env python3
"""Backward-compatible entry point for the canonical article writer.

The WeChat-specific writer was merged into ``write_article.py``.  Keep this
small launcher so old cron entries and manual commands continue to work while
all writing and queue behavior has a single implementation.
"""

import os
import sys
from pathlib import Path


def main() -> None:
    writer = Path(__file__).with_name("write_article.py")
    os.execv(sys.executable, [sys.executable, str(writer), *sys.argv[1:]])


if __name__ == "__main__":
    main()
