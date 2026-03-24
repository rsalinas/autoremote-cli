#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""Compatibility wrapper for the autoremote_cli package."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if _SRC.exists():
    sys.path.insert(0, str(_SRC))

from autoremote_cli.cli import *  # noqa: F403
from autoremote_cli.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
