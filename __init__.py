"""Quota Desk — Hermes Agent plugin.

The feature is split across two halves of one package:

* ``dashboard/`` — a FastAPI router mounted by the web server at
  ``/api/plugins/quota-desk/``; this is where credentials are resolved and the
  provider usage APIs are called (read-only).
* ``desktop/plugin.js`` — the desktop half, which only calls ``ctx.rest('/usage')``.

``register()`` exists so the plugin has a standard loader surface and can be
listed/enabled with ``hermes plugins``. It registers no tools and no hooks: the
dashboard route is the entire backend.
"""

from __future__ import annotations


def register(ctx) -> None:  # noqa: ARG001 — dashboard + desktop halves only
    return None
