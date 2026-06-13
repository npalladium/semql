"""Doctest collection for the ``semql`` core package.

Runs the ``>>>`` blocks embedded in public-surface / pure-helper
docstrings as a single ``test_doctest`` pytest item per module. The
curated set (errors, _resolve, spec, cnf, units, model) covers the
uniform error envelope, identifier resolution, spec parsing, CNF
rewriting, the unit registry, and the catalog model — all stable
output by design. We keep the scope tight on purpose: a future
refactor that changes a repr / ordering will break the suite
loudly with a line-number pointing at the docstring.

The plugin is opt-in: a conftest hook discovers the modules and
registers one item per module. Anything that imports a runtime
dependency the doctest doesn't need is not exercised here.
"""

from __future__ import annotations

import doctest
import importlib

import pytest

# Modules to doctest. Keep this list curated — adding a module
# means auditing its docstrings for stable output. Order matches
# AGENTS.md's model / compiler / discovery layering so a regression
# points at the right layer.
_DOCTEST_MODULES = (
    "semql.errors",
    "semql._resolve",
    "semql.spec",
    "semql.cnf",
    "semql.units",
    "semql.model",
)


@pytest.mark.parametrize("module_name", _DOCTEST_MODULES)
def test_doctest(module_name: str) -> None:
    """Run stdlib ``doctest`` over the named module's docstrings.

    Failures carry a line-number / expected-vs-got diff pointing
    straight at the docstring, so a regression's first responder
    can find it without spelunking the test file."""
    module = importlib.import_module(module_name)
    results = doctest.testmod(
        module,
        name=module_name,
        optionflags=doctest.NORMALIZE_WHITESPACE,
        raise_on_error=False,
    )
    # ``results`` is (attempted, failures); zero failures is a pass.
    assert results.failed == 0, (
        f"{results.failed} of {results.attempted} doctest examples "
        f"failed in {module_name}. Re-run with "
        f"``uv run python -m doctest packages/semql/src/<module>.py`` "
        "for the full diff."
    )
