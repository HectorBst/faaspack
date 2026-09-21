"""Configuration: ``[tool.faaspack]`` in pyproject.toml, merged with CLI overrides.

Resolution order (last wins): built-in defaults, ``[tool.faaspack]``,
``[tool.faaspack.<target>]``, command-line options.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
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


_TUPLE_FIELDS = {
    "architectures",
    "extras",
    "groups",
    "exclude_packages",
    "prune_suffixes",
    "keep",
    "ignore_imports",
}
_BOOL_FIELDS = {
    "only_binary",
    "compile",
    "trim_dist_info",
    "prune_tests",
    "prune_scripts",
    "strip_vendored_libs",
}
_KNOWN = {f.name for f in fields(Options)}


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


def _coerce(key: str, value: Any) -> Any:
    name = key.replace("-", "_")
    if name not in _KNOWN:
        raise FaaspackError(f"unknown faaspack option: {key!r}")
    if name in _TUPLE_FIELDS or name == "code":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise FaaspackError(f"option {key!r} must be a list of strings")
        return tuple(value)
    if name in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise FaaspackError(f"option {key!r} must be true or false")
        return value
    if name == "strip":
        if value is False or value is None:
            return None
        if value not in ("debug", "unneeded"):
            raise FaaspackError("option 'strip' must be false, 'debug' or 'unneeded'")
        return cast(StripMode, value)
    if name == "import_check":
        if value not in ("warn", "error", "ignore"):
            raise FaaspackError("option 'import-check' must be 'warn', 'error' or 'ignore'")
        return value
    if name in ("compression_level", "source_date_epoch"):
        if not isinstance(value, int) or isinstance(value, bool):
            raise FaaspackError(f"option {key!r} must be an integer")
        if name == "compression_level" and not 0 <= value <= 9:
            raise FaaspackError("option 'compression-level' must be between 0 and 9")
        return value
    if value is not None and not isinstance(value, str):
        raise FaaspackError(f"option {key!r} must be a string")
    return value


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
