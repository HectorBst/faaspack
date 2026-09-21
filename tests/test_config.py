from __future__ import annotations

from pathlib import Path

import pytest

from faaspack.config import DEFAULT_EPOCH, Options, load_project, resolve_options
from faaspack.errors import FaaspackError

TARGETS = {"lambda", "scaleway-functions"}


def write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.0"\n' + body, encoding="utf-8"
    )
    return tmp_path


def test_defaults_are_conservative() -> None:
    options = Options()
    assert options.compile is True
    assert options.trim_dist_info is True
    assert options.strip is None
    assert options.only_binary is True
    assert ".pyi" in options.prune_suffixes


def test_merge_order_common_then_target_then_cli(tmp_path: Path) -> None:
    project = load_project(
        write(
            tmp_path,
            """
[tool.faaspack]
python = "3.13"
strip = "debug"
keep = "numpy/_core/include/*"

[tool.faaspack.lambda]
layout = "split"
architectures = ["arm64"]
python = "3.14"
""",
        )
    )
    options = resolve_options(project, TARGETS, "lambda", {"strip": "unneeded", "layout": None})
    assert options.python == "3.14"
    assert options.layout == "split"
    assert options.architectures == ("arm64",)
    assert options.strip == "unneeded"
    assert options.keep == ("numpy/_core/include/*",)
    other = resolve_options(project, TARGETS, "scaleway-functions")
    assert other.python == "3.13"
    assert other.layout is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("unknown-key = 1", "unknown faaspack option"),
        ("strip = 'all'", "strip"),
        ("compile = 'yes'", "true or false"),
        ("compression-level = 12", "between 0 and 9"),
        ("groups = [1]", "list of strings"),
        ("import-check = 'maybe'", "import-check"),
    ],
)
def test_invalid_options(tmp_path: Path, body: str, message: str) -> None:
    project = load_project(write(tmp_path, f"[tool.faaspack]\n{body}\n"))
    with pytest.raises(FaaspackError, match=message):
        resolve_options(project, TARGETS, "lambda")


def test_strip_false_means_disabled(tmp_path: Path) -> None:
    project = load_project(write(tmp_path, "[tool.faaspack]\nstrip = false\n"))
    assert resolve_options(project, TARGETS, "lambda").strip is None


def test_epoch_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    assert Options().epoch() == DEFAULT_EPOCH
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    assert Options().epoch() == 1700000000
    assert Options(source_date_epoch=42).epoch() == 42
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "soon")
    with pytest.raises(FaaspackError):
        Options().epoch()


def test_project_metadata(tmp_path: Path) -> None:
    root = write(tmp_path, '[build-system]\nrequires = ["uv_build"]\n')
    project = load_project(root)
    assert project.name == "demo"
    assert project.version == "1.0"
    assert project.has_build_system
    with pytest.raises(FaaspackError, match="no pyproject"):
        load_project(tmp_path / "missing")
