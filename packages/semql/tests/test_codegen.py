"""Type-safe field-ref codegen: emit a Python module of ``QualifiedRef``
constants from a catalog so ``orders.revenue`` is checked at the call site
(typos become attribute errors the type checker catches) and autocompletes.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from semql import Catalog, QualifiedRef
from semql.__main__ import main
from semql.codegen import generate_refs_module
from semql.model import Cube, Dialect, Dimension, Measure, TimeDimension


def _catalog() -> Catalog:
    orders = Cube(
        name="orders",
        dialect=Dialect.POSTGRES,
        table="orders",
        alias="o",
        measures=[Measure(name="revenue", sql="{o}.amount", agg="sum")],
        dimensions=[Dimension(name="region", sql="{o}.region", type="string")],
        time_dimensions=[
            TimeDimension(name="created_at", sql="{o}.created_at", granularities=("day",))
        ],
    )
    return Catalog([orders])


def _exec(src: str) -> dict[str, object]:
    ns: dict[str, object] = {}
    exec(compile(src, "<generated>", "exec"), ns)  # noqa: S102 — exercising generated output
    return ns


def test_output_is_valid_python() -> None:
    src = generate_refs_module(_catalog())
    compile(src, "<generated>", "exec")  # raises SyntaxError if malformed


def test_generated_refs_are_qualified_refs() -> None:
    ns = _exec(generate_refs_module(_catalog()))
    orders = ns["orders"]
    assert orders.revenue == "orders.revenue"  # type: ignore[attr-defined]
    assert isinstance(orders.revenue, QualifiedRef)  # type: ignore[attr-defined]
    assert orders.region == "orders.region"  # type: ignore[attr-defined]
    assert orders.created_at == "orders.created_at"  # type: ignore[attr-defined]


def test_generated_ref_is_usable_in_a_query() -> None:
    ns = _exec(generate_refs_module(_catalog()))
    orders = ns["orders"]
    from semql import SemanticQuery

    q = SemanticQuery(measures=[orders.revenue], dimensions=[orders.region])  # type: ignore[attr-defined]
    assert q.measures == ["orders.revenue"]


def test_deterministic() -> None:
    cat = _catalog()
    assert generate_refs_module(cat) == generate_refs_module(cat)


def test_python_keyword_field_is_skipped_not_crashing() -> None:
    # A field whose name is a Python keyword can't be a class attribute;
    # codegen must skip it (with a comment) rather than emit invalid Python.
    cube = Cube(
        name="events",
        dialect=Dialect.POSTGRES,
        table="events",
        alias="e",
        measures=[Measure(name="count", sql="*", agg="count")],
        dimensions=[
            Dimension(name="class", sql="{e}.class", type="string"),
            Dimension(name="kind", sql="{e}.kind", type="string"),
        ],
    )
    src = generate_refs_module(Catalog([cube]))
    compile(src, "<generated>", "exec")  # must not be a SyntaxError
    ns = _exec(src)
    events = ns["events"]
    assert events.kind == "events.kind"  # type: ignore[attr-defined]
    assert not hasattr(events, "class")  # the keyword field was skipped
    assert "class" in src and "keyword" in src.lower()  # documented as skipped


def test_cli_codegen_writes_importable_module(tmp_path: Path) -> None:
    # A catalog module the --catalog locator can import.
    pkg = tmp_path / "gencat.py"
    pkg.write_text(
        "from semql import Catalog\n"
        "from semql.model import Cube, Dialect, Measure, Dimension\n"
        "cat = Catalog([Cube(name='orders', dialect=Dialect.POSTGRES, table='orders', alias='o',\n"
        "    measures=[Measure(name='revenue', sql='{o}.amount', agg='sum')],\n"
        "    dimensions=[Dimension(name='region', sql='{o}.region', type='string')])])\n"
    )
    out = tmp_path / "refs.py"
    sys.path.insert(0, str(tmp_path))
    try:
        rc = main(["codegen", "--catalog", "gencat:cat", "--out", str(out)])
        assert rc == 0
        assert out.exists()

        mod = importlib.import_module("refs")
        importlib.reload(mod)
        orders = vars(mod)["orders"]
        assert orders.revenue == "orders.revenue"
        assert isinstance(orders.revenue, QualifiedRef)
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
        sys.modules.pop("refs", None)
        sys.modules.pop("gencat", None)
