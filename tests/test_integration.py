"""End-to-end builds of the sample project with the real uv (network access to PyPI)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from faaspack.pipeline import bundle

pytestmark = pytest.mark.integration


def pyc_flags(archive: zipfile.ZipFile) -> set[int]:
    return {
        int.from_bytes(archive.read(name)[4:8], "little")
        for name in archive.namelist()
        if name.endswith(".pyc")
    }


def test_lambda_function_both_architectures(sample_app: Path) -> None:
    result = bundle(sample_app, "lambda", {"output_dir": "out"})
    assert [b.architecture for b in result.builds] == ["x86_64", "arm64"]
    for build in result.builds:
        (artifact,) = build.artifacts
        with zipfile.ZipFile(artifact.archive.path) as archive:
            names = set(archive.namelist())
            assert "sample_app/handler.py" in names
            assert "sample_app/__pycache__/handler.cpython-314.pyc" in names
            assert not any("/tests/" in n or n.startswith("bin/") for n in names)
            assert not any(n.endswith(("/RECORD", "/INSTALLER", ".pyi")) for n in names)
            assert any(n.endswith(("pydantic-" in n and "/METADATA") or "") for n in names)
            assert pyc_flags(archive) == {1}
            so = [n for n in names if n.startswith("pydantic_core/") and n.endswith(".so")]
            cpu = {"x86_64": "x86_64", "arm64": "aarch64"}[build.architecture]
            assert so
            assert cpu in so[0]
        codes = [f.code for f in build.findings]
        assert codes.count("FP402") == 1  # boto3 imported but not declared
        assert "FP401" not in codes  # orjson is guarded, mypy_boto3_s3 is TYPE_CHECKING only
        assert "FP101" not in codes
    manifest = json.loads(result.manifest.read_text())
    assert manifest["target"] == "lambda"
    assert {d["name"] for d in manifest["builds"][0]["distributions"]} >= {
        "pydantic",
        "httpx",
        "sample-app",
    }
    assert "pydantic" in manifest["builds"][0]["modules"]["deps"]


def test_lambda_split_is_reproducible(sample_app: Path) -> None:
    options = {"output_dir": "a", "layout": "split", "architectures": ("x86_64",)}
    first = bundle(sample_app, "lambda", options)
    second = bundle(sample_app, "lambda", {**options, "output_dir": "b"})
    hashes = [[a.archive.sha256 for a in r.builds[0].artifacts] for r in (first, second)]
    assert hashes[0] == hashes[1]
    function, layer = first.builds[0].artifacts
    with zipfile.ZipFile(function.archive.path) as archive:
        assert all(n.startswith("sample_app") for n in archive.namelist())
    with zipfile.ZipFile(layer.archive.path) as archive:
        assert all(n.startswith("python/") for n in archive.namelist())
    assert "FP301" in [f.code for f in first.findings]


def test_scaleway_function_uses_musl_wheels(sample_app: Path) -> None:
    result = bundle(sample_app, "scaleway-functions", {"output_dir": "out"})
    (build,) = result.builds
    with zipfile.ZipFile(build.artifacts[0].archive.path) as archive:
        names = archive.namelist()
        assert "sample_app/handler.py" in names
        assert any(
            n.startswith("package/pydantic_core/") and n.endswith("linux-musl.so") for n in names
        )
    assert "FP401" in [f.code for f in build.findings]  # boto3 is not provided on Scaleway


def test_ignore_imports_and_code_paths(sample_app: Path) -> None:
    pyproject = sample_app / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text() + "\n[tool.faaspack.scaleway-functions]\n"
        'ignore-imports = ["boto3"]\ncode = ["src/sample_app"]\n'
    )
    result = bundle(sample_app, "scaleway-functions", {"output_dir": "out"})
    (build,) = result.builds
    assert build.findings == []
    with zipfile.ZipFile(build.artifacts[0].archive.path) as archive:
        # copied sources: no dist-info for the project itself
        assert not any(n.startswith("sample_app-") for n in archive.namelist())


def test_exclude_packages_removes_them_from_the_bundle(sample_app: Path) -> None:
    result = bundle(
        sample_app,
        "lambda",
        {"output_dir": "out", "architectures": ("x86_64",), "exclude_packages": ("certifi",)},
    )
    (build,) = result.builds
    with zipfile.ZipFile(build.artifacts[0].archive.path) as archive:
        names = archive.namelist()
        assert not any(n.startswith("certifi") for n in names)
        assert any(n.startswith("httpx/") for n in names)
    assert "certifi" not in {d.name for d in build.distributions}
