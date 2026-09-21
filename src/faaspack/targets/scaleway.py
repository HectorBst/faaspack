"""Scaleway Serverless Functions, Python runtimes, .zip deployment.

Facts relied upon (checked 2026-09-21):
- "Our Python runtimes run on top of Alpine Linux environments": musl libc, so
  musllinux wheels (the build image python-dep:3.14 runs Alpine 3.23, musl 1.2.5).
- Dependencies go in a ``package`` directory at the root of the archive; the official
  example imports them without touching sys.path.
- Limits: 100 MiB zip, 500 MiB code size per function. No layers.
- CPU architecture is not documented; the python-dep image is amd64 only.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from faaspack.errors import Finding, Level
from faaspack.targets.base import ArtifactSpec, BuiltArtifact, Target, VerifySpec

ZIP_LIMIT = 100 * 1024 * 1024
CODE_LIMIT = 500 * 1024 * 1024


class ScalewayFunctionsTarget(Target):
    name: ClassVar[str] = "scaleway-functions"
    description: ClassVar[str] = "Scaleway Serverless Functions .zip (Python runtime, Alpine)"
    architectures: ClassVar[tuple[str, ...]] = ("x86_64",)
    default_architectures: ClassVar[tuple[str, ...]] = ("x86_64",)
    layouts: ClassVar[tuple[str, ...]] = ("function",)
    default_layout: ClassVar[str] = "function"
    default_python: ClassVar[str] = "3.14"
    libc: ClassVar[str] = "musl"

    def python_platform(self, *, python: str, architecture: str) -> str:
        return "x86_64-unknown-linux-musl"

    def plan(self, layout: str) -> list[ArtifactSpec]:
        return [ArtifactSpec("function", (("code", ""), ("deps", "package/")), None)]

    def check_sizes(self, artifacts: Sequence[BuiltArtifact]) -> list[Finding]:
        findings: list[Finding] = []
        for artifact in artifacts:
            if artifact.size > ZIP_LIMIT:
                findings.append(
                    Finding(
                        "FP211",
                        Level.ERROR,
                        f"zip is {artifact.size / 2**20:.1f} MiB, above the 100 MiB Scaleway limit",
                    )
                )
            if artifact.unzipped_size > CODE_LIMIT:
                findings.append(
                    Finding(
                        "FP212",
                        Level.ERROR,
                        f"code size {artifact.unzipped_size / 2**20:.1f} MiB is above the 500 MiB "
                        "Scaleway limit",
                    )
                )
        return findings

    def verify_spec(self, *, python: str, architecture: str) -> VerifySpec:
        return VerifySpec(
            image=f"rg.fr-par.scw.cloud/scwfunctionsruntimes-public/python-dep:{python}",
            docker_platform="linux/amd64",
            python_executable="python3",
            mounts={"function": "/function"},
            sys_path=["/function", "/function/package"],
            site_dirs=[],
            note="python-dep is Scaleway's build image (Alpine, musl), not the runtime itself",
        )
