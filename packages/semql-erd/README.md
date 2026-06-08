# semql-erd

ER-diagram generator for [`semql`](../semql) catalogs. Walks the
cubes and joins in a `Catalog` and emits a [Graphviz](https://graphviz.org)
DOT source (and convenience PNG/SVG when the `graphviz` Python bindings
+ system `dot` binary are available).

Useful when:
- The catalog is past 10 cubes and reading the YAML/Python isn't
  enough to see the join shape at a glance.
- A PR touches a `Join` and the reviewer wants a visual diff of the
  before / after graph.
- Onboarding docs need a stable picture of what's in scope.

## Install

```sh
pip install semql-erd            # DOT source only — no system deps
pip install "semql-erd[image]"   # + graphviz Python bindings
                                 #   (also needs the `dot` binary)
```

## Quick start — DOT source

`render_dot(catalog)` is dependency-free: it produces a DOT-language
string you can paste into any Graphviz renderer
([Edotor](https://edotor.net) is a quick web one).

```python
from semql import Backend, Catalog, Cube, Dimension, Join, Measure
from semql_erd import render_dot

orders = Cube(
    name="orders",
    backend=Backend.POSTGRES,
    table="orders",
    alias="o",
    measures=[Measure(name="revenue", sql="{o}.amount", agg="sum", unit="currency")],
    dimensions=[Dimension(name="region", sql="{o}.region", type="string")],
    joins=[Join(to="customers", relationship="many_to_one", on="{o}.cid = {c}.id")],
)
customers = Cube(
    name="customers",
    backend=Backend.POSTGRES,
    table="customers",
    alias="c",
    dimensions=[Dimension(name="name", sql="{c}.name", type="string")],
)

print(render_dot(Catalog([orders, customers])))
```

## Quick start — PNG/SVG

```python
from semql_erd import render_image

# Requires `pip install "semql-erd[image]"` AND the `dot` binary on PATH.
render_image(catalog, "catalog.png")  # PNG by default
render_image(catalog, "catalog.svg", format="svg")
```

## Conventions

- **Nodes** are cubes. The label is a Graphviz record showing the cube
  name (+ `display_name` suffix if set), the backend, and three field
  sections (measures, dimensions, time-dimensions).
- **Edges** are `Join`s. Arrowhead shape encodes the relationship:
  - `many_to_one` → `crow` on the from-side, `tee` on the to-side
  - `one_to_many` → mirror of the above
  - `one_to_one` → `tee` on both sides
- **Filtering** mirrors the planner prompt: by default only cubes with
  `expose_in_prompt=True` (and non-META cubes) appear. Pass
  `only_exposed=False` for a full graph.
- **Layout** defaults to `rankdir="LR"` (left-to-right). Pass
  `rankdir="TB"` for top-to-bottom.

## CLI

```sh
python -m semql_erd path.to.module:catalog          # prints DOT to stdout
python -m semql_erd path.to.module:catalog out.svg  # writes a rendered image
```

## Status

Early development. The DOT format is stable; record-section ordering
and node ID naming may evolve.
