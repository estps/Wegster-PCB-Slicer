
from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    BACKEND_DIR = Path(sys.executable).resolve().parent
else:
    BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))

__all__ = ["BACKEND_DIR"]
