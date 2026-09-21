from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from faaspack.archive import write_zip


def tree(root: Path) -> None:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg/__init__.py").write_text("x = 1\n")
    tool = root / "pkg/tool.sh"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o700)
    (root / "top.py").write_text("y = 2\n")


def test_zip_is_deterministic_and_normalized(tmp_path: Path) -> None:
    tree(tmp_path / "a")
    tree(tmp_path / "b")
    (tmp_path / "b/top.py").touch()  # different mtime must not matter
    first = write_zip(tmp_path / "1.zip", [(tmp_path / "a", "")], epoch=1_700_000_000)
    second = write_zip(tmp_path / "2.zip", [(tmp_path / "b", "")], epoch=1_700_000_000)
    assert first.sha256 == second.sha256
    assert first.files == 3
    assert first.unzipped_size == len("x = 1\n") + len("#!/bin/sh\n") + len("y = 2\n")
    with zipfile.ZipFile(tmp_path / "1.zip") as archive:
        infos = {i.filename: i for i in archive.infolist()}
        assert list(infos) == sorted(infos)
        assert stat.S_IMODE(infos["top.py"].external_attr >> 16) == 0o644
        assert stat.S_IMODE(infos["pkg/tool.sh"].external_attr >> 16) == 0o755
        assert stat.S_IMODE(infos["pkg/"].external_attr >> 16) == 0o755
        assert infos["top.py"].date_time == (2023, 11, 14, 22, 13, 20)


def test_prefixes_and_old_epoch(tmp_path: Path) -> None:
    tree(tmp_path / "deps")
    (tmp_path / "code").mkdir()
    (tmp_path / "code/handler.py").write_text("def handle(e, c): ...\n")
    info = write_zip(
        tmp_path / "f.zip", [(tmp_path / "code", ""), (tmp_path / "deps", "package/")], epoch=0
    )
    with zipfile.ZipFile(info.path) as archive:
        names = archive.namelist()
        assert "handler.py" in names
        assert "package/" in names
        assert "package/pkg/__init__.py" in names
        assert archive.getinfo("handler.py").date_time == (1980, 1, 1, 0, 0, 0)
    assert len(info.sha256_base64) == 44


def test_duplicate_files_are_rejected(tmp_path: Path) -> None:
    tree(tmp_path / "a")
    tree(tmp_path / "b")
    with pytest.raises(ValueError, match="duplicate archive entry"):
        write_zip(tmp_path / "x.zip", [(tmp_path / "a", ""), (tmp_path / "b", "")], epoch=0)
