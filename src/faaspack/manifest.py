"""The JSON manifest written next to the artifacts: the input of deployment tools and of
``faaspack verify``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from faaspack import __version__
from faaspack.config import Options, Project
from faaspack.results import ArchBuild, BundleResult


def write_manifest(result: BundleResult, project: Project, options: Options) -> None:
    """Write ``result.manifest``; artifact paths are relative to its folder."""
    base = result.manifest.parent
    data = {
        "tool": {"name": "faaspack", "version": __version__},
        "project": {"name": project.name, "version": project.version},
        "target": result.target,
        "layout": result.layout,
        "python": result.python,
        "source_date_epoch": options.epoch(),
        "options": {
            "only_binary": options.only_binary,
            "compile": options.compile,
            "trim_dist_info": options.trim_dist_info,
            "prune_tests": options.prune_tests,
            "strip": options.strip,
        },
        "findings": [f.as_dict() for f in result.findings],
        "builds": [_build_entry(build, base) for build in result.builds],
    }
    result.manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _build_entry(build: ArchBuild, base: Path) -> dict[str, Any]:
    return {
        "architecture": build.architecture,
        "platform": build.platform,
        "artifacts": [a.as_dict(base) for a in build.artifacts],
        "distributions": [d.as_dict() for d in build.distributions],
        "modules": build.modules,
        "pruned_bytes": build.prune.removed_bytes,
        "stripped_bytes": build.strip.saved_bytes if build.strip else 0,
        "findings": [f.as_dict() for f in build.findings],
    }
