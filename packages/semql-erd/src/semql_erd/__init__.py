"""Graphviz ER-diagram generator for semql Catalogs.

``render_dot(catalog)`` returns a DOT-language string and has no
third-party dependencies. ``render_image(catalog, path)`` shells out
to Graphviz via the ``graphviz`` Python bindings — install with
``pip install "semql-erd[image]"`` and a system ``dot`` binary.
"""

from __future__ import annotations

from semql_erd.dot import render_dot
from semql_erd.image import render_image

__all__ = ["render_dot", "render_image"]
