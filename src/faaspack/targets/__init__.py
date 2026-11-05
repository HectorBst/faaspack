"""Target registry, backed by the ``faaspack.targets`` entry point group."""

from __future__ import annotations

from importlib.metadata import entry_points

from faaspack.errors import FaaspackError
from faaspack.targets.aws_lambda import LambdaTarget
from faaspack.targets.base import Target
from faaspack.targets.scaleway import ScalewayFunctionsTarget

GROUP = "faaspack.targets"


def available_targets() -> dict[str, type[Target]]:
    targets: dict[str, type[Target]] = {}
    for entry in entry_points(group=GROUP):
        loaded = entry.load()
        if not (isinstance(loaded, type) and issubclass(loaded, Target)):
            raise FaaspackError(f"entry point {entry.name!r} is not a faaspack Target")
        targets[entry.name] = loaded
    if not targets:  # running from a source tree without installed metadata
        targets = {
            LambdaTarget.name: LambdaTarget,
            ScalewayFunctionsTarget.name: ScalewayFunctionsTarget,
        }
    return targets


def get_target(name: str) -> Target:
    targets = available_targets()
    if name not in targets:
        raise FaaspackError(f"unknown target {name!r} (available: {', '.join(sorted(targets))})")
    return targets[name]()
