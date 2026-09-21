"""Deterministic zip archives.

Same inputs give byte-identical archives: sorted entries, a fixed timestamp
(SOURCE_DATE_EPOCH), normalized permissions (0644 files, 0755 directories and
executables, as AWS Lambda requires) and a fixed compression level.
"""

from __future__ import annotations

import base64
import hashlib
import stat
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_MIN_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class ArchiveInfo:
    path: Path
    size: int
    unzipped_size: int
    files: int
    sha256: str
    sha256_base64: str


def _zip_time(epoch: int) -> tuple[int, int, int, int, int, int]:
    stamp = time.gmtime(epoch)
    value = (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, stamp.tm_hour, stamp.tm_min, stamp.tm_sec)
    return max(value, _MIN_ZIP_TIME)


def _collect(parts: Sequence[tuple[Path, str]]) -> dict[str, Path | None]:
    """Map archive names to source files (None for directories)."""
    entries: dict[str, Path | None] = {}
    for source, raw_prefix in parts:
        prefix = raw_prefix.strip("/")
        if prefix:
            for i in range(1, len(prefix.split("/")) + 1):
                entries.setdefault("/".join(prefix.split("/")[:i]) + "/", None)
        if not source.is_dir():
            continue
        for path in source.rglob("*"):
            if path.is_symlink():
                continue
            rel = path.relative_to(source).as_posix()
            name = f"{prefix}/{rel}" if prefix else rel
            if path.is_dir():
                entries.setdefault(name + "/", None)
            elif name in entries:
                raise ValueError(f"duplicate archive entry: {name}")
            else:
                entries[name] = path
    return entries


def write_zip(
    destination: Path,
    parts: Sequence[tuple[Path, str]],
    *,
    epoch: int,
    compression_level: int = 9,
) -> ArchiveInfo:
    """Write ``parts`` (source directory, prefix inside the archive) to ``destination``."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    entries = _collect(parts)
    date_time = _zip_time(epoch)
    unzipped = files = 0
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            source = entries[name]
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.create_system = 3  # Unix, so that external_attr carries permissions
            if source is None:
                info.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
                archive.writestr(info, b"")
                continue
            executable = source.stat().st_mode & 0o111
            mode = 0o755 if executable else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            data = source.read_bytes()
            unzipped += len(data)
            files += 1
            archive.writestr(
                info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=compression_level
            )
    digest = hashlib.sha256(destination.read_bytes()).digest()
    return ArchiveInfo(
        path=destination,
        size=destination.stat().st_size,
        unzipped_size=unzipped,
        files=files,
        sha256=digest.hex(),
        sha256_base64=base64.b64encode(digest).decode("ascii"),
    )
