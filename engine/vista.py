#!/usr/bin/env python
"""Entry point for the VISTA visual harness CLI.

Usage::

    python vista.py init --window "My Game"
    python vista.py capture --frames 5
    python vista.py inspect --turn 3 --region 100,120,64,64 --zoom 8 --montage
    python vista.py read_pixels --turn 3 --region 0,0,64,64 --rows 8 --cols 8
    python vista.py act --click-frame 32,32
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vista_harness.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
