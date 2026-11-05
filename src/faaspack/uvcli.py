"""Adapter for the uv command line.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from faaspack.errors import FaaspackError

_ERROR_LINES_SHOWN = 15


@dataclass(frozen=True)
class LockSelection:
    """The part of uv.lock to export: a workspace member, its extras and groups, minus some
    packages."""

    package: str | None = None
    extras: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallPlatform:
    """The Python minor version and the uv ``--python-platform`` an installation targets."""

    python: str
    platform: str


class UvCommands(Protocol):
    """The uv operations a bundle needs. :class:`Uv` runs the real uv; tests use fakes."""

    def export_requirements(self, project: Path, output: Path, selection: LockSelection) -> None:
        """Write the locked, non-dev dependencies of ``project`` (itself excluded)."""

    def build_wheel(self, project: Path, out_dir: Path, *, package: str | None) -> Path:
        """Build the project wheel into ``out_dir`` and return its path."""

    def install_requirements(
        self,
        project: Path,
        target_dir: Path,
        requirements: Path,
        *,
        platform: InstallPlatform,
        only_binary: bool,
    ) -> None:
        """Install exactly the listed requirements into the flat directory ``target_dir``."""

    def install_wheels(
        self, project: Path, target_dir: Path, wheels: Sequence[Path], *, platform: InstallPlatform
    ) -> None:
        """Install the given wheels, without their dependencies, into ``target_dir``."""

    def find_python(self, version: str) -> Path:
        """Return an interpreter of the requested minor version."""


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
    """Runs uv commands, from the project root so that its ``[tool.uv]`` settings apply."""

    def __init__(
        self, binary: str | None = None, *, echo: Callable[[str], None] | None = None
    ) -> None:
        """``echo`` receives every uv command line before it runs (``--verbose``)."""
        self.binary = locate_uv(binary)
        self.echo = echo

    def run(self, args: Sequence[str], *, cwd: Path | None = None) -> str:
        """Run ``uv <args>`` and return its standard output; raise on a non-zero exit."""
        if self.echo is not None:
            self.echo(f"+ uv {shlex.join(args)}")
        proc = subprocess.run(
            [self.binary, *args], cwd=cwd, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            output = (proc.stderr or proc.stdout).strip().splitlines()
            tail = "\n".join(output[-_ERROR_LINES_SHOWN:])
            raise FaaspackError(f"`uv {' '.join(args)}` failed (exit {proc.returncode}):\n{tail}")
        return proc.stdout

    def export_requirements(self, project: Path, output: Path, selection: LockSelection) -> None:
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
        if selection.package:
            args += ["--package", selection.package]
        args += _repeat("--extra", selection.extras)
        args += _repeat("--group", selection.groups)
        args += _repeat("--no-emit-package", selection.exclude)
        self.run(args, cwd=project)

    def install_requirements(
        self,
        project: Path,
        target_dir: Path,
        requirements: Path,
        *,
        platform: InstallPlatform,
        only_binary: bool,
    ) -> None:
        # --no-deps keeps the installed set exactly equal to the exported lock.
        extra = ["--only-binary", ":all:"] if only_binary else []
        self._install(project, target_dir, platform, [*extra, "--requirement", str(requirements)])

    def install_wheels(
        self, project: Path, target_dir: Path, wheels: Sequence[Path], *, platform: InstallPlatform
    ) -> None:
        self._install(project, target_dir, platform, [str(wheel) for wheel in wheels])

    def _install(
        self, project: Path, target_dir: Path, platform: InstallPlatform, sources: Sequence[str]
    ) -> None:
        args = [
            "pip",
            "install",
            "--target",
            str(target_dir),
            "--python",
            platform.python,
            "--python-platform",
            platform.platform,
            "--no-deps",
            "--no-compile-bytecode",  # compiled later, in unchecked-hash mode
            "--no-installer-metadata",  # hidden but accepted: drops INSTALLER, REQUESTED...
            "--quiet",
            *sources,
        ]
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


def _repeat(flag: str, values: Sequence[str]) -> list[str]:
    return [item for value in values for item in (flag, value)]
