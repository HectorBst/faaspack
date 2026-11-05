from __future__ import annotations

from pathlib import Path

from faaspack.prune import Pruner


def make(root: Path, rel: str, content: bytes = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def build_tree(root: Path) -> None:
    for rel in [
        "pkg/__init__.py",
        "pkg/__pycache__/mod.cpython-314.pyc",
        "pkg/tests/test_a.py",
        "pkg/sub/test/helper.py",
        "pkg/testing/__init__.py",
        "pkg/_core.pyi",
        "pkg/_core.c",
        "pkg/include/api.h",
        "pkg-1.0.dist-info/METADATA",
        "pkg-1.0.dist-info/RECORD",
        "pkg-1.0.dist-info/WHEEL",
        "pkg-1.0.dist-info/INSTALLER",
        "pkg-1.0.dist-info/entry_points.txt",
        "pkg-1.0.dist-info/licenses/LICENSE",
        "tests/__init__.py",
        "bin/pkg-cli",
    ]:
        make(root, rel)
    make(root, ".lock", b"")


def test_default_pruning(tmp_path: Path) -> None:
    build_tree(tmp_path)
    report = Pruner(suffixes=(".pyi", ".c", ".h")).prune(tmp_path)
    remaining = sorted(
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    )
    assert remaining == [
        "pkg-1.0.dist-info/METADATA",
        "pkg-1.0.dist-info/entry_points.txt",
        "pkg-1.0.dist-info/licenses/LICENSE",
        "pkg/__init__.py",
        "pkg/testing/__init__.py",
    ]
    assert report.removed_files == 12
    assert set(report.by_rule) == {
        "pycache",
        "scripts",
        "dist-info",
        "tests",
        "suffix",
        "installer",
    }


def test_everything_can_be_disabled(tmp_path: Path) -> None:
    build_tree(tmp_path)
    report = Pruner(trim_dist_info=False, prune_tests=False, prune_scripts=False).prune(tmp_path)
    assert report.by_rule.keys() == {"pycache", "installer"}
    assert (tmp_path / "pkg/tests/test_a.py").exists()
    assert (tmp_path / "pkg-1.0.dist-info/RECORD").exists()


def test_keep_globs_win(tmp_path: Path) -> None:
    build_tree(tmp_path)
    Pruner(suffixes=(".h",), keep=("pkg/include/*", "pkg/tests")).prune(tmp_path)
    assert (tmp_path / "pkg/include/api.h").exists()
    assert (tmp_path / "pkg/tests/test_a.py").exists()
    assert not (tmp_path / "pkg/sub/test").exists()


def test_keep_globs_protect_files_inside_pruned_directories(tmp_path: Path) -> None:
    build_tree(tmp_path)
    make(tmp_path, "pkg/tests/data/fixture.json", b"{}")
    make(tmp_path, "bin/pkg-tool")
    report = Pruner(keep=("pkg/tests/data/*.json", "bin/pkg-tool")).prune(tmp_path)
    assert (tmp_path / "pkg/tests/data/fixture.json").exists()
    assert not (tmp_path / "pkg/tests/test_a.py").exists()
    assert (tmp_path / "bin/pkg-tool").exists()
    assert not (tmp_path / "bin/pkg-cli").exists()
    assert report.by_rule["tests"] == 3  # bytes of test_a.py, helper.py and tests/__init__.py


def test_non_empty_lock_file_is_kept(tmp_path: Path) -> None:
    make(tmp_path, ".lock", b"data")
    Pruner().prune(tmp_path)
    assert (tmp_path / ".lock").exists()
