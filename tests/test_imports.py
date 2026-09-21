from __future__ import annotations

from pathlib import Path

from faaspack.imports import scan_imports, top_level_modules

CODE = """
import json, os.path
from typing import TYPE_CHECKING
import boto3
from pydantic import BaseModel
from . import sibling
from .sub import thing

try:
    import orjson
except ImportError:
    orjson = None

try:
    import ujson
except (ValueError, ModuleNotFoundError):
    pass

try:
    import must_have
except ValueError:
    pass

if TYPE_CHECKING:
    import mypy_boto3_s3

def lazy():
    import numpy.linalg
"""


def test_scan_ignores_guarded_and_relative_imports(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app/handler.py").write_text(CODE)
    (tmp_path / "app/broken.py").write_text("def (:\n")
    found = scan_imports([tmp_path])
    assert set(found) == {"json", "os", "typing", "boto3", "pydantic", "must_have", "numpy"}
    assert found["boto3"] == ["app/handler.py:4"]


def test_top_level_modules(tmp_path: Path) -> None:
    for rel in [
        "pkg/__init__.py",
        "ns/part/__init__.py",
        "single.py",
        "_speedups.cpython-314-x86_64-linux-gnu.so",
        "pkg-1.0.dist-info/METADATA",
        "pkg.libs/libfoo.so.1",
        "bin/tool",
        "__pycache__/x.pyc",
        "not-an-identifier/x.py",
    ]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    assert top_level_modules(tmp_path) == ["_speedups", "ns", "pkg", "single"]
    assert top_level_modules(tmp_path / "missing") == []
