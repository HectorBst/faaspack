from __future__ import annotations

import marshal
import sys
from pathlib import Path

from faaspack.bytecode import compile_tree

PYC_HEADER = 16


def read_pyc(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    flags = int.from_bytes(data[4:8], "little")
    code = marshal.loads(data[PYC_HEADER:])
    return flags, code.co_filename


def test_unchecked_hash_and_relocated_filename(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/mod.py").write_text("VALUE = 1\n")
    (tmp_path / "pkg/template.py").write_text("{% if user %}hello{% endif %}\n")
    report = compile_tree(Path(sys.executable), tmp_path, runtime_path="/var/task/")
    pyc = next((tmp_path / "pkg/__pycache__").glob("mod.*.pyc"))
    flags, filename = read_pyc(pyc)
    assert flags == 0b01  # hash-based, check_source off: unchecked-hash
    assert filename == "/var/task/pkg/mod.py"
    assert report.failures == ["pkg/template.py"]


def test_relative_filename_when_runtime_root_unknown(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("x = 1\n")
    compile_tree(Path(sys.executable), tmp_path, runtime_path=None)
    _, filename = read_pyc(next((tmp_path / "__pycache__").glob("m.*.pyc")))
    assert filename == "m.py"


def test_in_place_keeps_real_path(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("x = 1\n")
    compile_tree(Path(sys.executable), tmp_path, runtime_path=None, relocate=False)
    _, filename = read_pyc(next((tmp_path / "__pycache__").glob("m.*.pyc")))
    assert filename == str(tmp_path / "m.py")


def test_empty_directory(tmp_path: Path) -> None:
    assert (
        compile_tree(Path(sys.executable), tmp_path / "nothing", runtime_path=None).failures == []
    )
