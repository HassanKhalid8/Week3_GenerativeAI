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

Two further points, both load-bearing:

* Nothing the browser requests may live under `/api/*`. That prefix belongs to
  the platform: any `/api` URL without a matching file in this directory is
  answered 404 before the rewrite above is even consulted. The JSON API is
  served at `/studio/*` for that reason - see webapp/app.py.
* If a host cannot interpolate `:vpath*` into the destination query string, the
  parameter arrives as the literal template. That is recorded in the environ so
  the page can tell the browser to stop relying on the rewrite and call this
  function's own URL with `?__vpath=<route>` instead - a path that needs no
  rewriting to work.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.app import (  # noqa: E402
    FUNCTION_URL,
    PATH_PARAM,
    VPATH_ENVIRON_KEY,
    app,
)


def _usable(value: str) -> bool:
    """Reject an un-interpolated rewrite template like ':vpath*' or '$1'."""
    return not any(token in value for token in (":", "$", "*"))


class _VercelPath:
    """Restore the URL the visitor actually requested."""

    def __init__(self, wsgi_app, prefix: str = FUNCTION_URL):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        query = environ.get("QUERY_STRING", "")
        path = environ.get("PATH_INFO", "")
        state = "absent"

        if PATH_PARAM in query:
            carried: list[str] = []
            kept = []
            for key, value in parse_qsl(query, keep_blank_values=True):
                if key == PATH_PARAM:
                    carried.append(value)
                else:
                    # Drop our own parameter so the app sees only the caller's query.
                    kept.append((key, value))
            environ["QUERY_STRING"] = urlencode(kept)
            # A request may carry two: one the browser set and one the rewrite
            # added. Any interpolated value beats a bare template.
            chosen = next((value for value in carried if _usable(value)), None)
            if carried:
                # An empty value is a correctly interpolated "/" - the rewrite
                # worked - so only a template that survived counts as literal.
                state = "literal" if chosen is None else "ok"
            if chosen:
                path = "/" + chosen.lstrip("/")

        if path == self.prefix or path.startswith(self.prefix + "/"):
            path = path[len(self.prefix):] or "/"

        environ["PATH_INFO"] = path or "/"
        environ[VPATH_ENVIRON_KEY] = state
        # SCRIPT_NAME stays empty: url_for() must keep emitting site-root URLs,
        # because the browser addresses the site, not the function.
        environ["SCRIPT_NAME"] = ""
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _VercelPath(app.wsgi_app)

__all__ = ["app"]
