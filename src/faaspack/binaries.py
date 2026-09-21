"""Inspection and optional stripping of native binaries (ELF shared objects)."""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from faaspack.errors import FaaspackError, Finding, Level

# ELF e_machine values
ELF_MACHINES = {"x86_64": 62, "arm64": 183}
_EXT_LIBC = re.compile(r"-linux-(gnu|musl)[a-z]*\.so$")


def iter_shared_objects(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if (
            path.is_file()
            and not path.is_symlink()
            and (path.name.endswith(".so") or ".so." in path.name)
        ):
            yield path


def elf_machine(path: Path) -> int | None:
    """Return e_machine of an ELF file, or None if the file is not ELF."""
    with path.open("rb") as handle:
        header = handle.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        return None
    if header[5] == 1:
        return int.from_bytes(header[18:20], "little")
    return int.from_bytes(header[18:20], "big")


def check_binaries(root: Path, *, architecture: str, libc: str) -> list[Finding]:
    """Catch wheels built for the wrong CPU or libc before they reach the runtime."""
    findings: list[Finding] = []
    expected = ELF_MACHINES[architecture]
    for path in iter_shared_objects(root):
        rel = path.relative_to(root).as_posix()
        machine = elf_machine(path)
        if machine is not None and machine != expected:
            findings.append(
                Finding(
                    "FP101",
                    Level.ERROR,
                    f"{rel}: ELF machine {machine}, expected {expected} ({architecture})",
                )
            )
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


@dataclass
class StripReport:
    tool: str = ""
    stripped: int = 0
    saved_bytes: int = 0
    failures: list[str] = field(default_factory=list)


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
    if architecture == host_architecture():
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
        if not include_vendored_libs and any(p.endswith(".libs") for p in path.parts):
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
