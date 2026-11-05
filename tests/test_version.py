from __future__ import annotations

import importlib
import importlib.metadata
import tomllib
from pathlib import Path

import pytest

import faaspack


def test_version_comes_from_the_project_metadata() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert faaspack.__version__ == expected


def test_version_of_a_source_tree_that_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def not_installed(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", not_installed)
    try:
        assert importlib.reload(faaspack).__version__ == "0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(faaspack)
