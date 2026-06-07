"""CLI: ``python -m semql_erd <module:attr> [out_path]``.

``module:attr`` is a Python import path to a ``Catalog`` instance.
With no output path, prints DOT to stdout. With an output path,
renders an image (format inferred from suffix; defaults to PNG).

Example:

    python -m semql_erd my.catalog:CATALOG
    python -m semql_erd my.catalog:CATALOG catalog.svg
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from semql import Catalog

from semql_erd.dot import render_dot
from semql_erd.image import render_image


def _load_catalog(spec: str) -> Catalog:
    if ":" not in spec:
        raise SystemExit(
            f"expected '<module>:<attr>', got {spec!r}. Example: 'my_project.catalog:CATALOG'"
        )
    module_name, attr = spec.split(":", 1)
    module = importlib.import_module(module_name)
    if not hasattr(module, attr):
        raise SystemExit(f"module {module_name!r} has no attribute {attr!r}.")
    obj = getattr(module, attr)
    if not isinstance(obj, Catalog):
        raise SystemExit(f"{spec} is not a Catalog instance (got {type(obj).__name__}).")
    return obj


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    spec = args[0]
    catalog = _load_catalog(spec)

    if len(args) == 1:
        sys.stdout.write(render_dot(catalog))
        return 0

    out = Path(args[1])
    fmt = out.suffix.lstrip(".") or "png"
    rendered = render_image(catalog, out, format=fmt)
    print(f"wrote {rendered}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
