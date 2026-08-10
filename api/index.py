"""Vercel serverless entrypoint.

`@vercel/python` looks for a module-level WSGI callable named `app`, so this is
the same shim as the repository-root app.py, one directory deeper because Vercel
discovers functions under api/.

The catch-all rewrite in vercel.json sends every request here. Vercel used to
hand the function the *original* request path, but now routes internal rewrites
using the rewritten destination - so Flask would be asked for `/api/index` and
answer 404 for the whole site. `_MountedAtPrefix` normalizes the path back,
which makes the app correct under either behaviour and under a plain local run.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.app import app  # noqa: E402

#: The URL Vercel's filesystem routing assigns to this function.
FUNCTION_PREFIX = "/api/index"


class _MountedAtPrefix:
    """Strip the function's own mount path so Flask sees the route the user asked for."""

    def __init__(self, wsgi_app, prefix: str = FUNCTION_PREFIX):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == self.prefix or path.startswith(self.prefix + "/"):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
            # SCRIPT_NAME stays empty: url_for() must keep emitting site-root
            # URLs, because the browser addresses the site, not the function.
            environ["SCRIPT_NAME"] = ""
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _MountedAtPrefix(app.wsgi_app)

__all__ = ["app"]
