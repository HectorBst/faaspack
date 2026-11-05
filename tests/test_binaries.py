from __future__ import annotations

import shutil
import subprocess
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


@pytest.mark.parametrize(
    ("magic", "kind"),
    [
        (b"\xcf\xfa\xed\xfe", "Mach-O (macOS)"),
        (b"\xca\xfe\xba\xbe", "Mach-O universal (macOS)"),
        (b"MZ\x90\x00", "PE (Windows)"),
    ],
)
def test_binaries_for_another_os_are_errors(tmp_path: Path, magic: bytes, kind: str) -> None:
    for name in ("_speedups.cpython-314-darwin.so", "libfoo.dylib", "_win.pyd"):
        (tmp_path / "pkg").mkdir(exist_ok=True)
        (tmp_path / "pkg" / name).write_bytes(magic + bytes(60))
    findings = check_binaries(tmp_path, architecture="x86_64", libc="gnu")
    assert [f.code for f in findings] == ["FP101"] * 3
    message = findings[0].message
    assert message.startswith(f"pkg/_speedups.cpython-314-darwin.so: {kind} binary")
    assert message.endswith("the runtime loads Linux ELF files only")


def test_other_files_named_like_libraries_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "libc.so").write_text("/* GNU ld script */\nGROUP ( libc.so.6 )\n")
    assert check_binaries(tmp_path, architecture="x86_64", libc="gnu") == []


def test_native_strip_is_only_used_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"strip", "llvm-strip"} else None,
    )
    monkeypatch.setattr("faaspack.binaries.host_architecture", lambda: "arm64")
    monkeypatch.setattr(sys, "platform", "linux")
    assert find_strip_tool("arm64") == "/usr/bin/strip"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert find_strip_tool("arm64") == "/usr/bin/llvm-strip"


def test_vendored_libraries_are_recognised_inside_the_tree_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "build.libs" / "deps"  # a parent of the tree ends with .libs
    fake_elf(root / "pkg/_ext.so", 62)
    fake_elf(root / "pkg.libs/libvendored.so", 62)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "faaspack.binaries.subprocess.run",
        lambda args, **_: subprocess.CompletedProcess(args, 0, "", ""),
    )
    report = strip_binaries(root, architecture="x86_64", mode="debug", tool="strip")
    assert report.stripped == 1  # pkg/_ext.so, not the vendored library


def test_missing_cross_strip_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
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
