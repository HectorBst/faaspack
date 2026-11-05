"""What a bundle produces, per architecture: artifacts, distributions, reports, findings."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faaspack.archive import ArchiveInfo
from faaspack.binaries import StripReport
from faaspack.errors import Finding
from faaspack.prune import PruneReport
from faaspack.targets.base import Stage


@dataclass
class Distribution:
    """An installed distribution, read from its ``*.dist-info`` before pruning."""

    name: str
    version: str
    tags: list[str]
    stage: Stage

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "tags": self.tags, "stage": self.stage}


@dataclass
class ArtifactResult:
    kind: str
    archive: ArchiveInfo

    def as_dict(self, base: Path) -> dict[str, Any]:
        """Manifest entry, with the archive path relative to ``base`` (the manifest folder)."""
        a = self.archive
        return {
            "kind": self.kind,
            "path": a.path.relative_to(base).as_posix(),
            "size": a.size,
            "unzipped_size": a.unzipped_size,
            "files": a.files,
            "sha256": a.sha256,
            "sha256_base64": a.sha256_base64,
        }


@dataclass
class ArchBuild:
    """The build of one architecture."""

    architecture: str
    platform: str
    artifacts: list[ArtifactResult] = field(default_factory=list)
    distributions: list[Distribution] = field(default_factory=list)
    modules: dict[str, list[str]] = field(default_factory=dict)
    contributors: list[tuple[str, int]] = field(default_factory=list)
    prune: PruneReport = field(default_factory=PruneReport)
    strip: StripReport | None = None
    compile_failures: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


@dataclass
class BundleResult:
    target: str
    layout: str
    python: str
    manifest: Path
    builds: list[ArchBuild]
    findings: list[Finding]

    def all_findings(self) -> list[Finding]:
        return [*self.findings, *(f for b in self.builds for f in b.findings)]
