"""Target plugin interface.

A target describes a serverless runtime: which wheels it accepts, how artifacts are laid
out, which limits apply and how to smoke-test an artifact in a container. New targets
are registered through the ``faaspack.targets`` entry point group.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal

from faaspack.errors import FaaspackError, Finding

Stage = Literal["code", "deps"]
_PYTHON = re.compile(r"^3\.\d+$")


@dataclass(frozen=True)
class ArtifactSpec:
    """One zip file to produce.

    ``parts`` maps each build stage to its directory inside the archive.
    ``runtime_root`` is where the archive is extracted at runtime (used to rewrite
    co_filename in bytecode), or None when unknown.
    """

    kind: str
    parts: tuple[tuple[Stage, str], ...]
    runtime_root: str | None


@dataclass(frozen=True)
class BuiltArtifact:
    kind: str
    size: int
    unzipped_size: int


@dataclass(frozen=True)
class VerifySpec:
    """How to run the official (or closest available) runtime image for a smoke test."""

    image: str
    docker_platform: str
    python_executable: str
    mounts: Mapping[str, str]  # artifact kind -> mount point in the container
    sys_path: Sequence[str]  # inserted in order at the front of sys.path
    site_dirs: Sequence[str]  # processed with site.addsitedir (.pth files)
    note: str = ""


class Target(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    architectures: ClassVar[tuple[str, ...]]
    default_architectures: ClassVar[tuple[str, ...]]
    layouts: ClassVar[tuple[str, ...]]
    default_layout: ClassVar[str]
    default_python: ClassVar[str]
    libc: ClassVar[str]
    # Modules importable at runtime without being bundled, with a hint for the user.
    # A hint of None means "provided and fine to rely on".
    runtime_provided: ClassVar[Mapping[str, str | None]] = {}

    def validate(self, *, python: str, layout: str, architectures: Sequence[str]) -> None:
        if not _PYTHON.match(python):
            raise FaaspackError(f"python must be a minor version like '3.14', got {python!r}")
        if layout not in self.layouts:
            raise FaaspackError(
                f"{self.name}: unknown layout {layout!r} (choose from {', '.join(self.layouts)})"
            )
        for arch in architectures:
            if arch not in self.architectures:
                raise FaaspackError(
                    f"{self.name}: unsupported architecture {arch!r} "
                    f"(supported: {', '.join(self.architectures)})"
                )

    @abstractmethod
    def python_platform(self, *, python: str, architecture: str) -> str:
        """uv ``--python-platform`` value for this runtime."""

    @abstractmethod
    def plan(self, layout: str) -> list[ArtifactSpec]:
        """Artifacts to produce for a layout; each stage must appear exactly once."""

    @abstractmethod
    def check_sizes(self, artifacts: Sequence[BuiltArtifact]) -> list[Finding]:
        """Compare artifact sizes with the platform quotas."""

    def notes(self, layout: str) -> list[Finding]:
        """Layout-specific reminders, printed with every build."""
        return []

    @abstractmethod
    def verify_spec(self, *, python: str, architecture: str) -> VerifySpec:
        """Container used by ``faaspack verify``."""
