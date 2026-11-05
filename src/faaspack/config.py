"""Configuration: ``[tool.faaspack]`` in pyproject.toml, merged with CLI overrides.

Resolution order (last wins): built-in defaults, ``[tool.faaspack]``,
``[tool.faaspack.<target>]``, command-line options.
"""

from __future__ import annotations

import os
import tomllib
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

from faaspack.errors import FaaspackError

StripMode = Literal["debug", "unneeded"]
ImportCheck = Literal["warn", "error", "ignore"]

DEFAULT_EPOCH = 315532800  # 1980-01-01T00:00:00Z, the smallest date a zip entry can hold
DEFAULT_PRUNE_SUFFIXES = (".pyi", ".c", ".h", ".pyx", ".pxd", ".pxi")


@dataclass(frozen=True)
class Options:
    """Every knob of a build. Field names map to kebab-case TOML keys."""

    python: str | None = None
    layout: str | None = None
    architectures: tuple[str, ...] = ()
    package: str | None = None
    code: tuple[str, ...] | None = None
    extras: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    exclude_packages: tuple[str, ...] = ()
    only_binary: bool = True
    compile: bool = True
    trim_dist_info: bool = True
    prune_tests: bool = True
    prune_scripts: bool = True
    prune_suffixes: tuple[str, ...] = DEFAULT_PRUNE_SUFFIXES
    keep: tuple[str, ...] = ()
    strip: StripMode | None = None
    strip_vendored_libs: bool = False
    strip_tool: str | None = None
    import_check: ImportCheck = "warn"
    ignore_imports: tuple[str, ...] = ()
    output_dir: str = "dist/faaspack"
    compression_level: int = 9
    source_date_epoch: int | None = None
    name: str | None = None

    def epoch(self) -> int:
        if self.source_date_epoch is not None:
            return self.source_date_epoch
        env = os.environ.get("SOURCE_DATE_EPOCH")
        if env:
            try:
                return int(env)
            except ValueError as exc:
                raise FaaspackError(f"SOURCE_DATE_EPOCH is not an integer: {env!r}") from exc
        return DEFAULT_EPOCH


@dataclass(frozen=True)
class Project:
    """What faaspack needs to know about the uv project being packaged."""

    root: Path
    name: str
    version: str | None
    has_build_system: bool
    tool_config: dict[str, Any] = field(default_factory=dict)

    @property
    def lockfile(self) -> Path:
        return self.root / "uv.lock"


def load_project(root: Path) -> Project:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        raise FaaspackError(f"no pyproject.toml in {root}")
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise FaaspackError(f"invalid {pyproject}: {exc}") from exc
    project = data.get("project", {})
    name = project.get("name")
    if not isinstance(name, str) or not name:
        raise FaaspackError(f"{pyproject} has no [project].name")
    version = project.get("version")
    tool = data.get("tool", {}).get("faaspack", {})
    if not isinstance(tool, dict):
        raise FaaspackError("[tool.faaspack] must be a table")
    return Project(
        root=root.resolve(),
        name=name,
        version=version if isinstance(version, str) else None,
        has_build_system="build-system" in data,
        tool_config=tool,
    )


def _string_list(key: str, value: Any) -> tuple[str, ...]:
    """A list of strings; a single string is accepted as a one-item list."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise FaaspackError(f"option {key!r} must be a list of strings")
    return tuple(value)


def _boolean(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise FaaspackError(f"option {key!r} must be true or false")
    return value


def _integer(key: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise FaaspackError(f"option {key!r} must be an integer")
    return value


def _compression_level(key: str, value: Any) -> int:
    level = _integer(key, value)
    if not 0 <= level <= zlib.Z_BEST_COMPRESSION:
        raise FaaspackError("option 'compression-level' must be between 0 and 9")
    return level


def _strip_mode(_: str, value: Any) -> StripMode | None:
    if value is False or value is None:
        return None
    if value not in ("debug", "unneeded"):
        raise FaaspackError("option 'strip' must be false, 'debug' or 'unneeded'")
    return cast(StripMode, value)


def _import_check(_: str, value: Any) -> ImportCheck:
    if value not in ("warn", "error", "ignore"):
        raise FaaspackError("option 'import-check' must be 'warn', 'error' or 'ignore'")
    return cast(ImportCheck, value)


def _optional_string(key: str, value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise FaaspackError(f"option {key!r} must be a string")
    return value


# Parser of each Options field, by field name.
_PARSERS: dict[str, Callable[[str, Any], Any]] = {
    **dict.fromkeys(
        (
            "architectures",
            "code",
            "exclude_packages",
            "extras",
            "groups",
            "ignore_imports",
            "keep",
            "prune_suffixes",
        ),
        _string_list,
    ),
    **dict.fromkeys(
        (
            "compile",
            "only_binary",
            "prune_scripts",
            "prune_tests",
            "strip_vendored_libs",
            "trim_dist_info",
        ),
        _boolean,
    ),
    **dict.fromkeys(
        ("layout", "name", "output_dir", "package", "python", "strip_tool"), _optional_string
    ),
    "compression_level": _compression_level,
    "import_check": _import_check,
    "source_date_epoch": _integer,
    "strip": _strip_mode,
}


def _coerce(key: str, value: Any) -> Any:
    """Validate the TOML value of ``key`` (kebab-case) and convert it to the field type."""
    name = key.replace("-", "_")
    if name not in _PARSERS:
        raise FaaspackError(f"unknown faaspack option: {key!r}")
    return _PARSERS[name](key, value)


def _apply(options: Options, table: dict[str, Any], *, skip: set[str]) -> Options:
    changes = {k.replace("-", "_"): _coerce(k, v) for k, v in table.items() if k not in skip}
    return replace(options, **changes)


def common_options(project: Project, target_names: set[str]) -> Options:
    """Options from ``[tool.faaspack]`` only (no target table), used by ``optimize``."""
    common = {k: v for k, v in project.tool_config.items() if k not in target_names}
    return _apply(Options(), common, skip=set())


def resolve_options(
    project: Project,
    target_names: set[str],
    target: str,
    overrides: dict[str, Any] | None = None,
) -> Options:
    """Merge defaults, ``[tool.faaspack]``, ``[tool.faaspack.<target>]`` and CLI overrides."""
    options = common_options(project, target_names)
    target_table = project.tool_config.get(target, {})
    if not isinstance(target_table, dict):
        raise FaaspackError(f"[tool.faaspack.{target}] must be a table")
    options = _apply(options, target_table, skip=set())
    if overrides:
        options = replace(options, **{k: v for k, v in overrides.items() if v is not None})
    return options
