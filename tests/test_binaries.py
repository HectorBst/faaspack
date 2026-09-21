from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from faaspack.binaries import check_binaries, elf_machine, find_strip_tool, strip_binaries
from faaspack.errors import FaaspackError


def fake_elf(path: Path, machine: int) -> Path:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2  # 64-bit
    header[5] = 1  # little endian
    header[18:20] = machine.to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header))
    return path


def real_shared_objects() -> list[Path]:
    import sysconfig

    roots = [Path(sysconfig.get_path("platstdlib")) / "lib-dynload", Path("/usr/lib"), Path("/lib")]
    found = []
    for root in roots:
        if root.is_dir():
            found += [
                p
                for p in sorted(root.rglob("*.so*"))[:200]
                if p.is_file() and not p.is_symlink() and elf_machine(p) is not None
            ]
    return found


def test_elf_machine(tmp_path: Path) -> None:
    assert elf_machine(fake_elf(tmp_path / "a.so", 183)) == 183
    (tmp_path / "b.so").write_bytes(b"not elf")
    assert elf_machine(tmp_path / "b.so") is None


def test_wrong_architecture_and_libc_are_errors(tmp_path: Path) -> None:
    fake_elf(tmp_path / "pkg/_ok.cpython-314-aarch64-linux-gnu.so", 183)
    fake_elf(tmp_path / "pkg/_x86.cpython-314-x86_64-linux-gnu.so", 62)
    fake_elf(tmp_path / "pkg/_musl.cpython-314-aarch64-linux-musl.so", 183)
    codes = sorted(f.code for f in check_binaries(tmp_path, architecture="arm64", libc="gnu"))
    assert codes == ["FP101", "FP102"]


def test_missing_cross_strip_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(FaaspackError, match="no strip tool"):
        find_strip_tool("arm64")
    with pytest.raises(FaaspackError, match="not found"):
        find_strip_tool("x86_64", "my-strip")


@pytest.mark.skipif(
    shutil.which("strip") is None or sys.platform != "linux", reason="needs GNU strip on Linux"
)
def test_strip_real_extension_module(tmp_path: Path) -> None:
    source = next(iter(real_shared_objects()), None)
    if source is None:
        pytest.skip("no ELF shared object found on this machine")
    target = tmp_path / "pkg" / source.name
    target.parent.mkdir()
    shutil.copy2(source, target)
    vendored = tmp_path / "pkg.libs" / "libvendored.so"
    vendored.parent.mkdir()
    shutil.copy2(source, vendored)
    from faaspack.binaries import host_architecture

    report = strip_binaries(tmp_path, architecture=host_architecture(), mode="unneeded")
    assert report.stripped == 1  # the .libs copy is skipped by default
    assert report.failures == []
    assert vendored.stat().st_size == source.stat().st_size
