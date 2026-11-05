"""Thin wrapper around the uv command line.

faaspack never re-implements resolution or installation: uv.lock stays the single
source of truth, exported with ``uv export --frozen`` and installed for the target
platform with ``uv pip install --target``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from faaspack.errors import FaaspackError


def locate_uv(explicit: str | None = None) -> str:
    """Find uv: explicit path, then ``$UV`` (set by uv for its subprocesses), then PATH."""
    for candidate in (explicit, os.environ.get("UV")):
        if candidate and Path(candidate).is_file():
            return candidate
    found = shutil.which("uv")
    if found:
        return found
    raise FaaspackError("uv not found: install it or set the UV environment variable")


class Uv:
    def __init__(
        self, binary: str | None = None, *, echo: Callable[[str], None] | None = None
    ) -> None:
        """``echo`` receives every uv command line before it runs (``--verbose``)."""
        self.binary = locate_uv(binary)
        self.echo = echo

    def run(self, args: Sequence[str], *, cwd: Path | None = None) -> str:
        cmd = [self.binary, *args]
        if self.echo is not None:
            self.echo(f"+ uv {shlex.join(args)}")
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-15:])
            raise FaaspackError(f"`uv {' '.join(args)}` failed (exit {proc.returncode}):\n{tail}")
        return proc.stdout

    def export_requirements(
        self,
        project: Path,
        output: Path,
        *,
        package: str | None,
        extras: Sequence[str],
        groups: Sequence[str],
        exclude: Sequence[str],
    ) -> None:
        """Export the locked, non-dev dependencies of the project (the project itself excluded)."""
        args = [
            "export",
            "--frozen",
            "--format",
            "requirements.txt",
            "--no-default-groups",
            "--no-editable",
            "--no-emit-project",
            "--no-header",
            "--output-file",
            str(output),
        ]
        if package:
            args += ["--package", package]
        for extra in extras:
            args += ["--extra", extra]
        for group in groups:
            args += ["--group", group]
        for name in exclude:
            args += ["--no-emit-package", name]
        self.run(args, cwd=project)

    def install_target(
        self,
        project: Path,
        target_dir: Path,
        *,
        python: str,
        platform: str,
        requirements: Path | None = None,
        wheels: Sequence[Path] = (),
        only_binary: bool = True,
    ) -> None:
        """Install into a flat directory for a foreign platform, without bytecode.

        ``--no-deps`` keeps the installed set exactly equal to the exported lock.
        Running from the project root keeps its [tool.uv] index configuration.
        """
        args = [
            "pip",
            "install",
            "--target",
            str(target_dir),
            "--python",
            python,
            "--python-platform",
            platform,
            "--no-deps",
            "--no-compile-bytecode",
            "--no-installer-metadata",
            "--quiet",
        ]
        if only_binary:
            args += ["--only-binary", ":all:"]
        if requirements is not None:
            args += ["--requirement", str(requirements)]
        args += [str(w) for w in wheels]
        self.run(args, cwd=project)

    def build_wheel(self, project: Path, out_dir: Path, *, package: str | None) -> Path:
        args = ["build", "--wheel", "--out-dir", str(out_dir), "--quiet"]
        if package:
            args += ["--package", package]
        self.run(args, cwd=project)
        wheels = sorted(out_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise FaaspackError(f"expected one wheel in {out_dir}, found {len(wheels)}")
        return wheels[0]

    def find_python(self, version: str) -> Path:
        """Return an interpreter of the requested version, downloading it with uv if needed."""
        try:
            out = self.run(["python", "find", "--no-project", version])
        except FaaspackError:
            self.run(["python", "install", version])
            out = self.run(["python", "find", "--no-project", version])
        path = Path(out.strip().splitlines()[-1])
        probe = subprocess.run(
            [str(path), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.stdout.strip() != version:
            raise FaaspackError(f"uv returned {path} for Python {version}: {probe.stdout.strip()}")
        return path
