"""Error types and build findings."""

from __future__ import annotations

import enum
from dataclasses import dataclass


class FaaspackError(Exception):
    """Fatal error: the build cannot continue (bad configuration, uv failure...)."""


class Level(enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class Finding:
    """A non-fatal observation produced during a build.

    ``code`` is stable and documented (README) so that CI can grep or ignore it.
    """

    code: str
    level: Level
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "level": self.level.value, "message": self.message}

    def __str__(self) -> str:
        return f"[{self.level.value.upper()} {self.code}] {self.message}"
