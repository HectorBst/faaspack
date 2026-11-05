"""Offline tests of ``faaspack bundle``, with a fake uv that installs realistic trees."""

from __future__ import annotations

import json
import marshal
import sys
import zipfile
from pathlib import Path

import pytest

from faaspack.binaries import StripReport
from faaspack.errors import FaaspackError, Level
from faaspack.pipeline import _overlap_findings, bundle
from faaspack.results import BundleResult
from faaspack.targets.aws_lambda import LambdaTarget
from tests.fakes import FakeUv, make_project

CACHE_TAG = sys.implementation.cache_tag
HANDLER = """
import json
import boto3
import dep
import missing_module
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path / "demo", handler=HANDLER)


def names(result: BundleResult, index: int = 0) -> list[str]:
    with zipfile.ZipFile(result.builds[0].artifacts[index].archive.path) as archive:
        return archive.namelist()


def co_filename(result: BundleResult, index: int, entry: str) -> str:
    with zipfile.ZipFile(result.builds[0].artifacts[index].archive.path) as archive:
        data = archive.read(entry)
    assert int.from_bytes(data[4:8], "little") == 1  # unchecked-hash
    filename: str = marshal.loads(data[16:]).co_filename
    return filename


def codes(result: BundleResult, level: Level | None = None) -> list[str]:
    return [f.code for f in result.all_findings() if level is None or f.level == level]


def test_function_layout_prunes_compiles_and_reports(project: Path) -> None:
    uv = FakeUv()
    result = bundle(project, "lambda", {"output_dir": "out"}, uv=uv)

    (build,) = result.builds
    assert build.platform == "x86_64-manylinux_2_34"
    content = names(result)
    assert "demo_app/handler.py" in content
    assert f"demo_app/__pycache__/handler.{CACHE_TAG}.pyc" in content
    assert "dep/__init__.py" in content
    assert "dep-1.0.dist-info/METADATA" in content
    for removed in ("dep/tests/", "bin/", "dep/__init__.pyi", "dep-1.0.dist-info/RECORD", ".lock"):
        assert not any(name.startswith(removed) for name in content), removed
    pyc = f"demo_app/__pycache__/handler.{CACHE_TAG}.pyc"
    assert co_filename(result, 0, pyc) == "/var/task/demo_app/handler.py"
    assert build.prune.removed_files > 0
    assert codes(result) == ["FP402", "FP401"]  # boto3, then missing_module (sorted)
    assert uv.called("export")[0].package is None


def test_manifest_describes_every_artifact(project: Path) -> None:
    result = bundle(
        project, "lambda", {"output_dir": "out", "architectures": ("arm64",)}, uv=FakeUv()
    )

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert result.manifest.name == "demo-app-1.0.0-lambda.manifest.json"
    assert manifest["target"] == "lambda"
    assert manifest["project"] == {"name": "demo-app", "version": "1.0.0"}
    (build,) = manifest["builds"]
    assert build["platform"] == "aarch64-manylinux_2_34"
    (artifact,) = build["artifacts"]
    assert artifact["path"] == "demo-app-1.0.0-lambda-function-arm64.zip"
    assert len(artifact["sha256_base64"]) == 44
    assert {d["name"] for d in build["distributions"]} == {"dep", "demo-app"}
    assert build["modules"] == {"code": ["demo_app"], "deps": ["dep"]}
    assert "FP101" not in [f["code"] for f in build["findings"]]


def test_split_layout_puts_dependencies_in_a_layer(project: Path) -> None:
    result = bundle(project, "lambda", {"output_dir": "out", "layout": "split"}, uv=FakeUv())

    function, layer = result.builds[0].artifacts
    assert (function.kind, layer.kind) == ("function", "layer")
    assert all(name.startswith("demo_app") for name in names(result, 0))
    assert all(name.startswith("python/") for name in names(result, 1))
    assert co_filename(result, 1, f"python/dep/__pycache__/__init__.{CACHE_TAG}.pyc") == (
        "/opt/python/dep/__init__.py"
    )
    assert [f.code for f in result.findings] == ["FP301"]
    assert "FP204" in codes(result)


def test_layer_layout_puts_everything_under_python(project: Path) -> None:
    result = bundle(project, "lambda", {"output_dir": "out", "layout": "layer"}, uv=FakeUv())

    (layer,) = result.builds[0].artifacts
    assert layer.kind == "layer"
    assert {"python/demo_app/handler.py", "python/dep/__init__.py"} <= set(names(result))


def test_scaleway_uses_musl_and_a_package_directory(project: Path) -> None:
    uv = FakeUv()
    result = bundle(project, "scaleway-functions", {"output_dir": "out"}, uv=uv)

    assert uv.called("install-requirements")[0].platform == "x86_64-unknown-linux-musl"
    content = names(result)
    assert "demo_app/handler.py" in content
    assert "package/dep/__init__.py" in content
    assert codes(result) == ["FP401", "FP401"]  # boto3 is not provided by Scaleway


def test_one_build_per_architecture(project: Path) -> None:
    result = bundle(
        project, "lambda", {"output_dir": "out", "architectures": ("x86_64", "arm64")}, uv=FakeUv()
    )
    assert [b.architecture for b in result.builds] == ["x86_64", "arm64"]
    assert [b.artifacts[0].archive.path.name for b in result.builds] == [
        "demo-app-1.0.0-lambda-function-x86_64.zip",
        "demo-app-1.0.0-lambda-function-arm64.zip",
    ]


def test_native_module_for_the_wrong_cpu_is_an_error(project: Path) -> None:
    result = bundle(project, "lambda", {"output_dir": "out"}, uv=FakeUv(wrong_machine=183))
    assert "FP101" in codes(result, Level.ERROR)


def test_code_shadowing_a_dependency_in_split_layout_is_a_warning(project: Path) -> None:
    result = bundle(
        project, "lambda", {"output_dir": "out", "layout": "split"}, uv=FakeUv(("demo_app",))
    )
    (finding,) = [f for f in result.all_findings() if f.code == "FP105"]
    assert finding.level == Level.WARNING
    assert "shadows the layer" in finding.message


def test_same_file_from_code_and_dependency_in_one_archive_is_fatal(project: Path) -> None:
    with pytest.raises(FaaspackError, match=r"duplicate archive entry: demo_app/__init__\.py"):
        bundle(project, "lambda", {"output_dir": "out"}, uv=FakeUv(("demo_app",)))


def test_overlap_in_one_archive_is_an_error() -> None:
    plan = LambdaTarget().plan("function")
    (finding,) = _overlap_findings({"code": ["app", "x"], "deps": ["app"]}, plan)
    assert (finding.code, finding.level) == ("FP105", Level.ERROR)
    assert _overlap_findings({"code": ["app"], "deps": ["dep"]}, plan) == []


def test_code_option_copies_sources_without_building_a_wheel(tmp_path: Path) -> None:
    project = make_project(
        tmp_path / "demo",
        build_system=False,
        extra_toml='[tool.faaspack]\ncode = ["src/demo_app"]\n',
    )
    uv = FakeUv(dependencies=())
    result = bundle(project, "lambda", {"output_dir": "out"}, uv=uv)

    assert uv.called("build") == []
    assert uv.called("install-requirements") == []  # nothing to install
    assert "demo_app/handler.py" in names(result)
    assert not any(".dist-info" in name for name in names(result))


def test_missing_code_path_is_fatal(tmp_path: Path) -> None:
    project = make_project(
        tmp_path / "demo", build_system=False, extra_toml='[tool.faaspack]\ncode = ["nowhere"]\n'
    )
    with pytest.raises(FaaspackError, match="code path not found: nowhere"):
        bundle(project, "lambda", {"output_dir": "out"}, uv=FakeUv())


def test_files_that_do_not_compile_stay_as_sources(project: Path) -> None:
    (project / "src/demo_app/template.py").write_text("{% if user %}hello{% endif %}\n")
    result = bundle(project, "lambda", {"output_dir": "out"}, uv=FakeUv())

    assert len(result.builds[0].compile_failures) == 1
    assert "FP104" in codes(result, Level.INFO)
    assert "demo_app/template.py" in names(result)


def test_no_compile_ships_sources_only(project: Path) -> None:
    result = bundle(project, "lambda", {"output_dir": "out", "compile": False}, uv=FakeUv())
    assert not any(name.endswith(".pyc") for name in names(result))


@pytest.mark.parametrize(("check", "expected"), [("error", Level.ERROR), ("ignore", None)])
def test_import_check_level(tmp_path: Path, check: str, expected: Level | None) -> None:
    project = make_project(
        tmp_path / "demo",
        handler=HANDLER,
        extra_toml=f'[tool.faaspack]\nimport-check = "{check}"\n',
    )
    result = bundle(project, "lambda", {"output_dir": "out"}, uv=FakeUv())
    fp401 = [f.level for f in result.all_findings() if f.code == "FP401"]
    assert fp401 == ([expected] if expected else [])


def test_ignore_imports_and_bundled_sdk(tmp_path: Path) -> None:
    project = make_project(
        tmp_path / "demo",
        handler=HANDLER,
        extra_toml='[tool.faaspack]\nignore-imports = ["missing_module"]\n',
    )
    result = bundle(
        project, "lambda", {"output_dir": "out"}, uv=FakeUv(dependencies=("dep", "boto3"))
    )
    assert codes(result) == []


def test_import_sites_are_listed_with_an_ellipsis(tmp_path: Path) -> None:
    handler = "import missing_module\n" * 4
    project = make_project(tmp_path / "demo", handler=handler)
    result = bundle(project, "lambda", {"output_dir": "out"}, uv=FakeUv())
    (finding,) = result.all_findings()
    assert "demo_app/handler.py:3 ..." in finding.message


def test_builds_are_reproducible(project: Path) -> None:
    first = bundle(project, "lambda", {"output_dir": "a"}, uv=FakeUv())
    second = bundle(project, "lambda", {"output_dir": "b"}, uv=FakeUv())
    assert (
        first.builds[0].artifacts[0].archive.sha256 == second.builds[0].artifacts[0].archive.sha256
    )


def test_largest_contributors_merge_a_package_and_its_dist_info(project: Path) -> None:
    result = bundle(project, "lambda", {"output_dir": "out"}, uv=FakeUv())
    contributors = dict(result.builds[0].contributors)
    assert set(contributors) == {"dep", "demo_app"}


def test_strip_failures_are_warnings(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_strip(root: Path, **_: object) -> StripReport:
        return StripReport(tool="strip", failures=["dep/_speedups.so: boom"])

    monkeypatch.setattr("faaspack.pipeline.strip_binaries", fake_strip)
    result = bundle(project, "lambda", {"output_dir": "out", "strip": "debug"}, uv=FakeUv())
    assert result.builds[0].strip is not None
    assert "FP103" in codes(result, Level.WARNING)


def test_version_comes_from_the_wheel_when_dynamic(tmp_path: Path) -> None:
    project = make_project(tmp_path / "demo")
    pyproject = project / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace('version = "1.0.0"\n', ""))
    result = bundle(project, "lambda", {"output_dir": "out", "name": "My_App"}, uv=FakeUv())
    assert result.manifest.name == "my-app-1.0.0-lambda.manifest.json"


def test_settings_are_validated_before_running_uv(project: Path) -> None:
    uv = FakeUv()
    with pytest.raises(FaaspackError, match="unknown layout"):
        bundle(project, "lambda", {"layout": "zip"}, uv=uv)
    (project / "uv.lock").unlink()
    with pytest.raises(FaaspackError, match="uv lock"):
        bundle(project, "lambda", uv=uv)
    assert uv.calls == []
