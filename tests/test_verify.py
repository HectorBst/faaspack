"""``faaspack verify`` with a fake container runtime: command line and result parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from faaspack.errors import FaaspackError, Level
from faaspack.pipeline import bundle
from faaspack.verify import Docker, VerifyOptions, verify
from tests.fakes import FakeRuntime, FakeUv, make_project


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    project = make_project(tmp_path / "demo")
    options = {"output_dir": "out", "layout": "split", "architectures": ("x86_64", "arm64")}
    return bundle(project, "lambda", options, uv=FakeUv()).manifest


def result_line(results: dict[str, str | None]) -> str:
    return "noise\nFAASPACK_RESULT " + json.dumps(results) + "\n"


def test_artifacts_are_mounted_where_lambda_extracts_them(manifest: Path) -> None:
    runtime = FakeRuntime(stdout=result_line({"dep": None}))
    (report,) = verify(manifest, VerifyOptions(architectures=("arm64",)), runtime=runtime)

    (args,) = runtime.calls
    assert args[args.index("--platform") + 1] == "linux/arm64"
    assert args[args.index("--network") + 1] == "none"
    volumes = [args[i + 1] for i, arg in enumerate(args) if arg == "-v" and ":" in args[i + 1]]
    assert [volume.split(":", 1)[1] for volume in volumes] == ["/var/task:ro", "/opt:ro"]
    assert "public.ecr.aws/lambda/python:3.14" in args
    paths, sites, modules = json.loads(args[-1])
    assert paths == ["/var/task", "/opt/python/lib/python3.14/site-packages", "/opt/python"]
    assert sites == paths
    assert modules == ["dep"]
    assert (report.architecture, report.imported, report.failures) == ("arm64", 1, {})
    assert [f.code for f in report.findings] == ["FP500"]


def test_module_selection(manifest: Path) -> None:
    runtime = FakeRuntime(stdout=result_line({}))
    options = VerifyOptions(
        architectures=("x86_64",), skip=("dep",), import_code=True, extra_imports=("extra",)
    )
    verify(manifest, options, runtime=runtime)
    assert json.loads(runtime.calls[0][-1])[2] == ["demo_app", "extra"]


def test_failed_imports_and_source_compilation_are_reported(manifest: Path) -> None:
    stderr = (
        "# code object from /var/task/demo_app/handler.py\n"
        "# code object from '/opt/python/dep/__pycache__/__init__.cpython-314.pyc'\n"
        "# code object from /var/lang/lib/python3.14/json/__init__.py\n"
    )
    runtime = FakeRuntime(
        stdout=result_line({"dep": None, "demo_app": "ImportError: x"}), stderr=stderr
    )
    reports = verify(manifest, runtime=runtime)

    assert len(reports) == 2  # every architecture by default
    report = reports[0]
    assert report.from_source == [
        "/var/task/demo_app/handler.py"
    ]  # the runtime's own files are ignored
    assert report.failures == {"demo_app": "ImportError: x"}
    levels = {f.code: f.level for f in report.findings}
    assert levels == {"FP501": Level.ERROR, "FP502": Level.WARNING, "FP500": Level.INFO}


def test_a_container_that_does_not_run_is_a_warning(manifest: Path) -> None:
    runtime = FakeRuntime(stderr="exec format error")
    report = verify(manifest, VerifyOptions(architectures=("arm64",)), runtime=runtime)[0]
    (finding,) = report.findings
    assert (finding.code, finding.level) == ("FP503", Level.WARNING)
    assert "exec format error" in finding.message


def test_timeouts_are_warnings(manifest: Path) -> None:
    report = verify(
        manifest, VerifyOptions(architectures=("x86_64",)), runtime=FakeRuntime(timeout=True)
    )[0]
    assert [f.message for f in report.findings] == ["[x86_64] verification timed out"]


def test_scaleway_uses_the_build_image(tmp_path: Path) -> None:
    project = make_project(tmp_path / "demo")
    manifest = bundle(project, "scaleway-functions", {"output_dir": "out"}, uv=FakeUv()).manifest
    runtime = FakeRuntime(stdout=result_line({"dep": None}))
    (report,) = verify(manifest, runtime=runtime)
    assert report.image.endswith("/python-dep:3.14")
    assert "python-dep is Scaleway's build image" in report.findings[-1].message


def test_docker_adapter(tmp_path: Path) -> None:
    with pytest.raises(FaaspackError, match="not found: verify needs Docker"):
        Docker(str(tmp_path / "no-docker"))
    process = Docker(sys.executable).run(["-c", "pass"], timeout=30)
    assert process.args[1:3] == ["run", "-c"]
