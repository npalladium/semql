"""scripts/check_frozen_models.py — the value-object discipline lint. Pins
that the real tree stays all-frozen and that the check resolves frozen-ness
through inheritance (so it neither misses a bare model nor false-positives a
subclass of a frozen base)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "check_frozen_models.py"

_spec = importlib.util.spec_from_file_location("check_frozen_models", _SCRIPT)
assert _spec is not None and _spec.loader is not None
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def _names(violations: list[object]) -> set[str]:
    return {v.name for v in violations}  # type: ignore[attr-defined]


def test_real_tree_is_all_frozen() -> None:
    assert lint.main() == 0


def test_flags_a_bare_model(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from pydantic import BaseModel\nclass Loose(BaseModel):\n    x: int\n"
    )
    assert _names(lint.find_violations([tmp_path])) == {"Loose"}


def test_accepts_directly_frozen_model(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "class Tight(BaseModel):\n"
        "    model_config = ConfigDict(frozen=True)\n"
        "    x: int\n"
    )
    assert lint.find_violations([tmp_path]) == []


def test_accepts_frozen_via_inheritance(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from pydantic import BaseModel, ConfigDict\n"
        "class FrozenBase(BaseModel):\n"
        "    model_config = ConfigDict(frozen=True)\n"
        "class Child(FrozenBase):\n"
        "    x: int\n"
    )
    assert lint.find_violations([tmp_path]) == []


def test_accepts_class_config_frozen(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from pydantic import BaseModel\n"
        "class Old(BaseModel):\n"
        "    class Config:\n"
        "        frozen = True\n"
    )
    assert lint.find_violations([tmp_path]) == []


def test_ignores_non_models(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "from enum import StrEnum\nclass Plain:\n    pass\nclass Color(StrEnum):\n    RED = 'red'\n"
    )
    assert lint.find_violations([tmp_path]) == []


def test_ignores_tests_dir(tmp_path: Path) -> None:
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "m.py").write_text(
        "from pydantic import BaseModel\nclass Loose(BaseModel):\n    x: int\n"
    )
    assert lint.find_violations([tmp_path]) == []
