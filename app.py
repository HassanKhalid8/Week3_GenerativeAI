"""Convenience entrypoint: `python app.py` starts the studio web UI.

Deployment targets that expect a module-level `app` (gunicorn, Vercel, Render)
can import this file directly.
"""

from webapp.app import app, main

__all__ = ["app", "main"]

if __name__ == "__main__":
    main()
