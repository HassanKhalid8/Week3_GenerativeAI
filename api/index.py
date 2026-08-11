"""Vercel serverless entrypoint.

`@vercel/python` looks for a module-level WSGI callable named `app`, so this is
the same shim as the repository-root app.py, one directory deeper because Vercel
discovers functions under api/.

Nothing is wrapped here. The path restoration this deployment needs lives on the
app itself, in webapp/app.py, because the platform - not this file - decides
which module gets imported: Vercel detects Flask as a backend framework and may
serve the root app.py instead of this one, in which case a wrapper applied here
would never run. See the `_RestorePath` note in webapp/app.py for what the
routing actually does, and the README's deploy section for why the browser talks
to /studio/* rather than /api/*.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.app import app  # noqa: E402

__all__ = ["app"]
