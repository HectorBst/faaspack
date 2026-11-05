"""Remove files that are useless at runtime from an installed tree.

Defaults are deliberately conservative (see README): ``*.dist-info`` directories are
kept because ``importlib.metadata`` needs ``METADATA`` and ``entry_points.txt`` at
runtime (the MCP Python SDK, OpenTelemetry and transformers break without them);
only installer bookkeeping files are dropped. ``testing`` packages are never removed
because some are public APIs (``numpy.testing``, ``pandas.testing``).
"""

from __future__ import annotations

import fnmatch
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# Installer bookkeeping, never read at import time. METADATA, entry_points.txt,
# top_level.txt and licenses/ are kept.
DIST_INFO_BOOKKEEPING = ("RECORD", "INSTALLER", "REQUESTED", "direct_url.json", "WHEEL")
TEST_DIR_NAMES = ("tests", "test")


@dataclass
class PruneReport:
    removed_files: int = 0
    removed_bytes: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)

    def add(self, rule: str, size: int, count: int = 1) -> None:
        self.removed_files += count
        self.removed_bytes += size
        self.by_rule[rule] = self.by_rule.get(rule, 0) + size

    def merge(self, other: PruneReport) -> None:
        self.removed_files += other.removed_files
        self.removed_bytes += other.removed_bytes
        for rule, size in other.by_rule.items():
            self.by_rule[rule] = self.by_rule.get(rule, 0) + size


def tree_size(path: Path) -> tuple[int, int]:
    """Return (bytes, files) of a file or directory tree."""
    if path.is_file():
        return path.stat().st_size, 1
    total = count = 0
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += item.stat().st_size
            count += 1
    return total, count


class Pruner:
    def __init__(
        self,
        *,
        trim_dist_info: bool = True,
        prune_tests: bool = True,
        prune_scripts: bool = True,
        suffixes: Iterable[str] = (),
        keep: Iterable[str] = (),
    ) -> None:
        self.trim_dist_info = trim_dist_info
        self.prune_tests = prune_tests
        self.prune_scripts = prune_scripts
        self.suffixes = tuple(suffixes)
        self.keep = tuple(keep)

    def _kept(self, root: Path, path: Path) -> bool:
        rel = path.relative_to(root).as_posix()
        return any(fnmatch.fnmatch(rel, pattern) for pattern in self.keep)

    def _remove(self, root: Path, path: Path, rule: str, report: PruneReport) -> None:
        if self._kept(root, path):
            return
        size, count = tree_size(path)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        report.add(rule, size, count)

    def prune(self, root: Path) -> PruneReport:
        """Prune ``root`` in place (a ``--target`` install or an application tree)."""
        report = PruneReport()
        if not root.is_dir():
            return report
        self._prune_bytecode(root, report)
        self._prune_uv_lock_file(root, report)
        if self.prune_scripts:
            self._prune_scripts(root, report)
        if self.trim_dist_info:
            self._trim_dist_info(root, report)
        if self.prune_tests:
            self._prune_tests(root, report)
        if self.suffixes:
            self._prune_suffixes(root, report)
        return report

    def _prune_bytecode(self, root: Path, report: PruneReport) -> None:
        # Stale bytecode from wheels or earlier runs; recompiled later with unchecked-hash.
        for cache in sorted(root.rglob("__pycache__"), reverse=True):
            if cache.is_dir():
                self._remove(root, cache, "pycache", report)

    def _prune_uv_lock_file(self, root: Path, report: PruneReport) -> None:
        # `uv pip install --target` leaves an empty `.lock` file at the root of the target.
        uv_lock = root / ".lock"
        if uv_lock.is_file() and uv_lock.stat().st_size == 0:
            self._remove(root, uv_lock, "installer", report)

    def _prune_scripts(self, root: Path, report: PruneReport) -> None:
        # Console scripts written by `uv pip install --target`: their shebang points to the
        # build machine interpreter, so they cannot run on the target anyway.
        scripts = root / "bin"
        if scripts.is_dir():
            self._remove(root, scripts, "scripts", report)

    def _trim_dist_info(self, root: Path, report: PruneReport) -> None:
        for dist_info in root.glob("*.dist-info"):
            for name in DIST_INFO_BOOKKEEPING:
                candidate = dist_info / name
                if candidate.is_file():
                    self._remove(root, candidate, "dist-info", report)

    def _prune_tests(self, root: Path, report: PruneReport) -> None:
        # Directories named `testing` are never touched: some are public APIs.
        candidates = [
            path
            for path in root.rglob("*")
            if path.is_dir()
            and path.name in TEST_DIR_NAMES
            and not any(part.endswith(".dist-info") for part in path.relative_to(root).parts)
        ]
        for path in sorted(candidates, key=lambda p: len(p.parts)):
            if path.exists():  # not already removed with a parent
                self._remove(root, path, "tests", report)

    def _prune_suffixes(self, root: Path, report: PruneReport) -> None:
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in self.suffixes:
                self._remove(root, path, "suffix", report)
