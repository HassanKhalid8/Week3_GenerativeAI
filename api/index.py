"""Vercel serverless entrypoint.

`@vercel/python` looks for a module-level WSGI callable named `app`, so this is
the same shim as the repository-root app.py, one directory deeper because Vercel
discovers functions under api/.

Getting the request path to Flask intact takes some care, because Vercel's
filesystem routing binds this file to exactly one URL - `/api/index` - and the
catch-all rewrite in vercel.json funnels the whole site into it:

* Sending the rewrite to `/api/index/<path>` does not work: no function is
  mounted at those sub-paths, so every URL except `/` 404s before Python runs.
* Sending it to a bare `/api/index` does reach the function, but Vercel now
  routes internal rewrites using the *rewritten* path, so Flask is asked for
  `/api/index` and answers 404 for the entire site.

So the rewrite carries the real path in a `__vpath` query parameter, and
`_VercelPath` below restores it before Flask sees the request. It falls back to
stripping the mount prefix, then to the path as-given, which keeps the app
correct under the older routing behaviour and under a plain local run too.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.app import app  # noqa: E402

#: The URL Vercel's filesystem routing assigns to this function.
FUNCTION_PREFIX = "/api/index"
#: Query parameter the rewrite uses to smuggle the original path through.
PATH_PARAM = "__vpath"


def _usable(value: str) -> bool:
    """Reject an un-interpolated rewrite template like ':vpath*' or '$1'."""
    return not any(token in value for token in (":", "$", "*"))


class _VercelPath:
    """Restore the URL the visitor actually requested."""

    def __init__(self, wsgi_app, prefix: str = FUNCTION_PREFIX):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        query = environ.get("QUERY_STRING", "")
        path = environ.get("PATH_INFO", "")

        if PATH_PARAM in query:
            pairs = parse_qsl(query, keep_blank_values=True)
            # Drop our own parameter so the app sees only the caller's query.
            carried = ""
            kept = []
            for key, value in pairs:
                if key == PATH_PARAM:
                    carried = value
                else:
                    kept.append((key, value))
            environ["QUERY_STRING"] = urlencode(kept)
            if _usable(carried):
                path = "/" + carried.lstrip("/")

        if path == self.prefix or path.startswith(self.prefix + "/"):
            path = path[len(self.prefix):] or "/"

        environ["PATH_INFO"] = path or "/"
        # SCRIPT_NAME stays empty: url_for() must keep emitting site-root URLs,
        # because the browser addresses the site, not the function.
        environ["SCRIPT_NAME"] = ""
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _VercelPath(app.wsgi_app)

__all__ = ["app"]
