"""Inspection and optional stripping of native binaries (ELF shared objects)."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from faaspack.errors import FaaspackError, Finding, Level

# ELF e_machine values
ELF_MACHINES = {"x86_64": 62, "arm64": 183}
_ELF_MAGIC = b"\x7fELF"
_ELF_DATA_LITTLE_ENDIAN = 1  # EI_DATA byte of e_ident
_ELF_MACHINE_OFFSET = 18  # after e_ident (16 bytes) and e_type (2 bytes)
_EXT_LIBC = re.compile(r"-linux-(gnu|musl)[a-z]*\.so$")
# Magic numbers of native formats that Linux cannot load: a wheel built on the wrong machine.
_FOREIGN_FORMATS = {
    b"\xcf\xfa\xed\xfe": "Mach-O (macOS)",
    b"\xce\xfa\xed\xfe": "Mach-O (macOS)",
    b"\xca\xfe\xba\xbe": "Mach-O universal (macOS)",
    b"MZ": "PE (Windows)",
}
_NATIVE_SUFFIXES = (".so", ".dylib", ".pyd")


def iter_shared_objects(root: Path) -> Iterator[Path]:
    """Native libraries and extension modules under ``root``, whatever their format."""
    for path in sorted(root.rglob("*")):
        name = path.name
        if (
            path.is_file()
            and not path.is_symlink()
            and (name.endswith(_NATIVE_SUFFIXES) or ".so." in name)
        ):
            yield path


def elf_machine(path: Path) -> int | None:
    """Return e_machine of an ELF file, or None if the file is not ELF."""
    end = _ELF_MACHINE_OFFSET + 2
    with path.open("rb") as handle:
        header = handle.read(end)
    if len(header) < end or not header.startswith(_ELF_MAGIC):
        return None
    little_endian = header[5] == _ELF_DATA_LITTLE_ENDIAN
    return int.from_bytes(header[_ELF_MACHINE_OFFSET:end], "little" if little_endian else "big")


def foreign_format(path: Path) -> str | None:
    """Name of the non-Linux native format of ``path`` (Mach-O, PE), if it has one."""
    with path.open("rb") as handle:
        magic = handle.read(4)
    return next(
        (name for prefix, name in _FOREIGN_FORMATS.items() if magic.startswith(prefix)), None
    )


def check_binaries(root: Path, *, architecture: str, libc: str) -> list[Finding]:
    """Catch wheels built for the wrong CPU, OS or libc before they reach the runtime."""
    findings: list[Finding] = []
    for path in iter_shared_objects(root):
        rel = path.relative_to(root).as_posix()
        problem = _architecture_problem(path, architecture)
        if problem is not None:
            findings.append(Finding("FP101", Level.ERROR, f"{rel}: {problem}"))
        match = _EXT_LIBC.search(path.name)
        if match and match.group(1) != libc:
            findings.append(
                Finding(
                    "FP102",
                    Level.ERROR,
                    f"{rel}: built for {match.group(1)} libc, the runtime uses {libc}",
                )
            )
    return findings


def _architecture_problem(path: Path, architecture: str) -> str | None:
    expected = ELF_MACHINES[architecture]
    machine = elf_machine(path)
    if machine is None:
        kind = foreign_format(path)
        return None if kind is None else f"{kind} binary, the runtime loads Linux ELF files only"
    if machine != expected:
        return f"ELF machine {machine}, expected {expected} ({architecture})"
    return None


@dataclass
class StripReport:
    tool: str = ""
    stripped: int = 0
    saved_bytes: int = 0
    failures: list[str] = field(default_factory=list)

    def merge(self, other: StripReport) -> StripReport:
        """Add ``other`` to this report (the tool of the first report is kept)."""
        self.stripped += other.stripped
        self.saved_bytes += other.saved_bytes
        self.failures += other.failures
        return self


def host_architecture() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)


def find_strip_tool(architecture: str, explicit: str | None = None) -> str:
    """Pick a strip binary able to handle ``architecture`` ELF files.

    The host ``strip`` of GNU binutils only understands the host architecture;
    cross builds need a target binutils (``aarch64-linux-gnu-strip``) or llvm-strip.
    """
    if explicit:
        found = shutil.which(explicit)
        if not found:
            raise FaaspackError(f"strip tool not found: {explicit}")
        return found
    gnu_prefix = {"x86_64": "x86_64-linux-gnu-", "arm64": "aarch64-linux-gnu-"}[architecture]
    candidates = [f"{gnu_prefix}strip", "llvm-strip"]
    # Outside Linux, `strip` is the system's own (cctools on macOS) and rejects ELF files.
    if architecture == host_architecture() and sys.platform.startswith("linux"):
        candidates.insert(0, "strip")
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    raise FaaspackError(
        f"no strip tool for {architecture}: install one of {', '.join(candidates)} "
        "or set strip-tool"
    )


def strip_binaries(
    root: Path,
    *,
    architecture: str,
    mode: str,
    include_vendored_libs: bool = False,
    tool: str | None = None,
) -> StripReport:
    """Strip shared objects in place.

    Libraries vendored by auditwheel under ``<package>.libs/`` were rewritten with
    patchelf; stripping them is a known source of corrupted ELF files ("ELF load
    command address/offset not properly aligned"), so they are skipped by default.
    """
    report = StripReport(tool=find_strip_tool(architecture, tool))
    flag = {"debug": "--strip-debug", "unneeded": "--strip-unneeded"}[mode]
    for path in iter_shared_objects(root):
        if elf_machine(path) is None:
            continue
        vendored = any(part.endswith(".libs") for part in path.relative_to(root).parts[:-1])
        if vendored and not include_vendored_libs:
            continue
        before = path.stat().st_size
        proc = subprocess.run(
            [report.tool, flag, str(path)], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            report.failures.append(f"{path.relative_to(root)}: {proc.stderr.strip()[:200]}")
            continue
        report.stripped += 1
        report.saved_bytes += before - path.stat().st_size
    return report
