"""``faaspack optimize``: prune and compile an installed tree in place, at its final location
(the ``site-packages`` of a container image, for example)."""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from faaspack.binaries import StripReport, host_architecture, strip_binaries
from faaspack.bytecode import compile_tree
from faaspack.config import Options
from faaspack.errors import FaaspackError
from faaspack.prune import Pruner, PruneReport
from faaspack.uvcli import Uv, UvCommands


@dataclass
class OptimizeResult:
    prune: PruneReport = field(default_factory=PruneReport)
    strip: StripReport | None = None
    compile_failures: list[str] = field(default_factory=list)


def optimize(
    directories: Sequence[Path],
    *,
    python: str,
    options: Options,
    architecture: str | None = None,
    uv: UvCommands | None = None,
) -> OptimizeResult:
    """Prune, optionally strip, and compile ``directories`` in place.

    Unlike ``bundle``, scripts in ``bin/`` are kept (a virtual environment needs them) and
    ``co_filename`` keeps the real path, since the tree does not move afterwards.
    """
    python_path = _interpreter(python, uv)
    pruner = Pruner(
        trim_dist_info=options.trim_dist_info,
        prune_tests=options.prune_tests,
        prune_scripts=False,
        suffixes=options.prune_suffixes,
        keep=options.keep,
    )
    result = OptimizeResult()
    for directory in directories:
        if not directory.is_dir():
            raise FaaspackError(f"not a directory: {directory}")
        result.prune.merge(pruner.prune(directory))
        if options.strip:
            report = strip_binaries(
                directory,
                architecture=architecture or host_architecture(),
                mode=options.strip,
                include_vendored_libs=options.strip_vendored_libs,
                tool=options.strip_tool,
            )
            result.strip = report if result.strip is None else result.strip.merge(report)
        if options.compile:
            compiled = compile_tree(
                python_path, directory.resolve(), runtime_path=None, relocate=False
            )
            result.compile_failures += compiled.failures
    return result


def _interpreter(python: str, uv: UvCommands | None) -> Path:
    """``python`` is an interpreter path, a minor version that uv finds or installs, or a
    command on PATH (``python3``)."""
    candidate = Path(python)
    if candidate.is_file():
        return candidate
    if re.fullmatch(r"3\.\d+", python):
        return (uv or Uv()).find_python(python)
    if found := shutil.which(python):
        return Path(found)
    raise FaaspackError(
        f"--python must be an interpreter path, a command or a version, got {python!r}"
    )
