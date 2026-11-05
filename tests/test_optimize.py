from __future__ import annotations

import sys
from pathlib import Path

import pytest

from faaspack.binaries import StripReport
from faaspack.config import Options
from faaspack.errors import FaaspackError
from faaspack.optimize import optimize
from tests.fakes import PYTHON, FakeUv


def test_optimize_in_place(tmp_path: Path) -> None:
    site = tmp_path / "site-packages"
    (site / "pkg/tests").mkdir(parents=True)
    (site / "pkg/__init__.py").write_text("x = 1\n")
    (site / "pkg/tests/test_x.py").write_text("")
    (site / "pkg/__pycache__").mkdir()
    (site / "pkg/__pycache__/__init__.cpython-399.pyc").write_bytes(b"stale")
    (site / "bin").mkdir()
    (site / "bin/tool").write_text("")
    result = optimize([site], python=sys.executable, options=Options())
    assert not (site / "pkg/tests").exists()
    assert (site / "bin/tool").exists()  # scripts are kept in place (venvs need them)
    pycs = list((site / "pkg/__pycache__").iterdir())
    assert len(pycs) == 1
    assert int.from_bytes(pycs[0].read_bytes()[4:8], "little") == 1
    assert result.compile_failures == []


def test_optimize_rejects_bad_python(tmp_path: Path) -> None:
    with pytest.raises(FaaspackError, match="interpreter path or a version"):
        optimize([tmp_path], python="python-latest", options=Options())


def test_optimize_accepts_a_version_found_by_uv(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("x = 1\n")
    uv = FakeUv()
    optimize([tmp_path], python=PYTHON, options=Options(), uv=uv)
    assert uv.called("find-python") == [PYTHON]


def test_optimize_rejects_missing_directories(tmp_path: Path) -> None:
    with pytest.raises(FaaspackError, match="not a directory"):
        optimize([tmp_path / "missing"], python=sys.executable, options=Options())


def test_optimize_merges_strip_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports = iter([StripReport("strip", 1, 10, []), StripReport("strip", 2, 5, ["x: boom"])])
    monkeypatch.setattr("faaspack.optimize.strip_binaries", lambda *_, **__: next(reports))
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    options = Options(strip="debug", compile=False)
    result = optimize(
        [tmp_path / "a", tmp_path / "b"],
        python=sys.executable,
        options=options,
        architecture="x86_64",
    )
    assert result.strip == StripReport("strip", 3, 15, ["x: boom"])
