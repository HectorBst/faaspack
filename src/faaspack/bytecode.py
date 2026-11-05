"""Bytecode compilation with ``unchecked-hash`` invalidation (PEP 552).

Timestamp-based .pyc files are rejected once zip tools rewrite modification times
(AWS SAM writes 1980, some tools write 2098), and the read-only ``/var/task`` of AWS
Lambda means they are recompiled at every cold start. ``unchecked-hash`` .pyc files are
loaded without looking at the source, which suits immutable deployment artifacts.

The compiling interpreter must have the same minor version as the target runtime;
bytecode does not depend on the CPU architecture.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from faaspack.errors import FaaspackError


@dataclass
class CompileReport:
    failures: list[str] = field(default_factory=list)  # relative to the compiled directory


def compile_tree(
    python: Path, directory: Path, *, runtime_path: str | None, relocate: bool = True
) -> CompileReport:
    """Compile every .py under ``directory`` in place.

    With ``relocate`` (build directories), the file name recorded in code objects
    (``co_filename``) is rewritten from the build directory to ``runtime_path``, the
    path the tree will have at runtime (relative when unknown). This also keeps the .pyc
    bytes independent from the temporary build directory. Without ``relocate`` (trees
    optimized in place, e.g. in a Dockerfile), the real path is recorded.
    """
    report = CompileReport()
    if not directory.is_dir() or not any(directory.rglob("*.py")):
        return report
    cmd = [
        str(python),
        "-m",
        "compileall",
        "-q",
        "-f",  # rewrite .pyc files even if a valid timestamp .pyc exists
        "-j",
        "0",
        "--invalidation-mode",
        "unchecked-hash",
    ]
    if relocate:
        cmd += ["-s", str(directory)]
        if runtime_path is not None:
            cmd += ["-p", runtime_path]
    cmd.append(str(directory))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode not in (0, 1):
        raise FaaspackError(f"compileall crashed (exit {proc.returncode}): {proc.stderr[-2000:]}")
    if proc.returncode == 1:
        # Some distributions ship templates or Python 2 files with a .py suffix: they
        # cannot be compiled and are simply left as sources.
        for line in proc.stdout.splitlines():
            if line.startswith("*** Error compiling"):
                path = Path(line.removeprefix("*** Error compiling ").strip(" '."))
                report.failures.append(_relative(path, directory))
    return report


def _relative(path: Path, directory: Path) -> str:
    """``path`` relative to ``directory``, so that reports do not depend on temporary paths."""
    try:
        return path.relative_to(directory).as_posix()
    except ValueError:
        return path.as_posix()
