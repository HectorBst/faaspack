from __future__ import annotations

import sys
from pathlib import Path

import pytest

from faaspack.cli import EXIT_FATAL, EXIT_OK, main
from faaspack.config import Options
from faaspack.pipeline import optimize


def test_targets_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["targets"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "lambda" in out
    assert "scaleway-functions" in out


def test_fatal_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bundle", "lambda", "--project", str(tmp_path)]) == EXIT_FATAL
    assert "no pyproject.toml" in capsys.readouterr().err


def test_bundle_requires_lock_and_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1"\n')
    assert main(["bundle", "lambda", "--project", str(tmp_path)]) == EXIT_FATAL
    assert "uv lock" in capsys.readouterr().err
    (tmp_path / "uv.lock").write_text("")
    assert main(["bundle", "lambda", "--project", str(tmp_path)]) == EXIT_FATAL
    assert "code = [...]" in capsys.readouterr().err
    assert main(["bundle", "lambda", "--project", str(tmp_path), "--arch", "sparc"]) == EXIT_FATAL
    assert "unsupported architecture" in capsys.readouterr().err


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
    from faaspack.errors import FaaspackError

    with pytest.raises(FaaspackError, match="interpreter path or a version"):
        optimize([tmp_path], python="python-latest", options=Options())
