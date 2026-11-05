"""Target registry, backed by the ``faaspack.targets`` entry point group."""

from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points

from faaspack.errors import FaaspackError
from faaspack.targets.aws_lambda import LambdaTarget
from faaspack.targets.base import Target
from faaspack.targets.scaleway import ScalewayFunctionsTarget

GROUP = "faaspack.targets"


def available_targets() -> dict[str, type[Target]]:
    """Targets registered in the ``faaspack.targets`` entry point group, by name."""
    targets = {entry.name: _load(entry) for entry in entry_points(group=GROUP)}
    if not targets:  # running from a source tree without installed metadata
        targets = {
            LambdaTarget.name: LambdaTarget,
            ScalewayFunctionsTarget.name: ScalewayFunctionsTarget,
        }
    return targets


def _load(entry: EntryPoint) -> type[Target]:
    try:
        loaded = entry.load()
    except Exception as exc:  # a third-party plugin can fail in any way while importing
        raise FaaspackError(
            f"target {entry.name!r} ({entry.value}) cannot be loaded: {exc}"
        ) from exc
    if not (isinstance(loaded, type) and issubclass(loaded, Target)):
        raise FaaspackError(f"entry point {entry.name!r} is not a faaspack Target")
    return loaded


def get_target(name: str) -> Target:
    targets = available_targets()
    if name not in targets:
        raise FaaspackError(f"unknown target {name!r} (available: {', '.join(sorted(targets))})")
    return targets[name]()
