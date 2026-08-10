"""Vercel serverless entrypoint.

`@vercel/python` looks for a module-level WSGI callable named `app`, so this is
the same shim as the repository-root app.py, one directory deeper because Vercel
discovers functions under api/.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.app import app  # noqa: E402,F401
