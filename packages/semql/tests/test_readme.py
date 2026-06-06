"""Pin the README's quick-start example to working code.

The README's job is to teach. A code block that doesn't run silently
gaslights new users — they can't tell whether they typed it wrong or
whether the docs are stale. This test extracts the first python
fenced block from the README and exec()s it; if the README ever
drifts from the real API, this test fails with the API error.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"


def _extract_first_python_block(markdown: str) -> str:
    match = re.search(r"```python\n(.*?)```", markdown, flags=re.DOTALL)
    assert match, "README has no python code block"
    return match.group(1)


def test_readme_quick_start_runs() -> None:
    block = _extract_first_python_block(README.read_text())
    # ``exec`` against a fresh namespace so tests stay hermetic.
    namespace: dict[str, object] = {}
    exec(compile(block, str(README), "exec"), namespace)  # noqa: S102
    # The quick-start should bind a `sql` (or similarly named result)
    # we can sanity-check — make the contract explicit so a future
    # rewrite doesn't quietly drop the demonstrable output.
    assert any(isinstance(v, str) and "SELECT" in v.upper() for v in namespace.values()), (
        "quick-start should produce a SQL string"
    )
