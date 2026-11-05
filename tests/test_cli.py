from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

import faaspack.cli
from faaspack.cli import EXIT_FATAL, EXIT_FINDINGS, EXIT_INTERRUPTED, EXIT_OK, main
from tests.fakes import FakeRuntime, FakeUv, make_project

HANDLER = "import boto3\n"  # FP402: a warning


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    """Replace uv and Docker in the CLI with fakes; return the fake runtime."""
    runtime = FakeRuntime(stdout='FAASPACK_RESULT {"dep": null}\n')
    monkeypatch.setattr(faaspack.cli, "Uv", lambda *_, **__: FakeUv())
    monkeypatch.setattr(faaspack.cli, "Docker", lambda *_: runtime)
    return runtime


def test_targets_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["targets"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "lambda" in out
    assert "scaleway-functions" in out


def test_fatal_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bundle", "lambda", "--project", str(tmp_path)]) == EXIT_FATAL
    assert "no pyproject.toml" in capsys.readouterr().err


def test_verify_without_a_manifest_exits_2(
    tmp_path: Path, offline: FakeRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["verify", str(tmp_path / "missing.json")]) == EXIT_FATAL
    assert "error: cannot read manifest" in capsys.readouterr().err


def test_interruption_exits_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(faaspack.cli, "available_targets", interrupted)
    assert main(["targets"]) == EXIT_INTERRUPTED
    assert capsys.readouterr().err == "interrupted\n"


def test_bundle_requires_lock_and_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1"\n')
    assert main(["bundle", "lambda", "--project", str(tmp_path)]) == EXIT_FATAL
    assert "uv lock" in capsys.readouterr().err
    (tmp_path / "uv.lock").write_text("")
    assert main(["bundle", "lambda", "--project", str(tmp_path)]) == EXIT_FATAL
    assert "code = [...]" in capsys.readouterr().err
    assert main(["bundle", "lambda", "--project", str(tmp_path), "--arch", "sparc"]) == EXIT_FATAL
    assert "unsupported architecture" in capsys.readouterr().err


def test_bundle_prints_artifacts_and_findings(
    tmp_path: Path, offline: FakeRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project(tmp_path / "demo", handler=HANDLER)
    assert main(["bundle", "lambda", "--project", str(project), "--layout", "split"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "[x86_64] installing locked dependencies" in out
    assert "lambda / split / x86_64" in out
    assert "largest: " in out
    assert "[WARNING FP402]" in out
    assert out.count("[INFO FP301]") == 1


def test_strict_bundle_fails_on_warnings_and_verify_runs(
    tmp_path: Path, offline: FakeRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project(tmp_path / "demo", handler=HANDLER)
    args = ["bundle", "lambda", "--project", str(project), "--arch", "x86_64,arm64"]
    assert main([*args, "--strict", "--verify", "--quiet", "--no-compile"]) == EXIT_FINDINGS
    out = capsys.readouterr().out
    assert "installing" not in out  # quiet
    assert "[INFO FP500] [arm64] 1/1 modules imported" in out
    assert len(offline.calls) == 2


def test_verify_command(
    tmp_path: Path, offline: FakeRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    project = make_project(tmp_path / "demo")
    main(["bundle", "lambda", "--project", str(project), "-q"])
    manifest = project / "dist/faaspack/demo-app-1.0.0-lambda.manifest.json"
    capsys.readouterr()
    code = main(["verify", str(manifest), "--arch", "x86_64", "--import-code", "--skip", "dep"])
    assert code == EXIT_OK
    assert "[INFO FP500]" in capsys.readouterr().out
    assert offline.calls[0][-1].endswith('["demo_app"]]')


def test_optimize_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site-packages"
    (site / "pkg/tests").mkdir(parents=True)
    (site / "pkg/__init__.py").write_text("x = 1\n")
    (site / "pkg/tests/test_x.py").write_text("")
    make_project(tmp_path / "project", extra_toml="[tool.faaspack]\nprune-tests = false\n")
    monkeypatch.chdir(tmp_path / "project")
    args = [
        "optimize",
        str(site),
        "--python",
        sys.executable,
        "--from-config",
        "--no-trim-dist-info",
    ]
    assert main(args) == EXIT_OK
    assert (site / "pkg/tests/test_x.py").exists()  # prune-tests = false, from the config
    assert "0 file(s) left uncompiled" in capsys.readouterr().out


def test_module_entry_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["faaspack", "targets"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("faaspack", run_name="__main__")
    assert exit_info.value.code == EXIT_OK
    assert "scaleway-functions" in capsys.readouterr().out
