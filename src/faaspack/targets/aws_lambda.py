"""AWS Lambda, Python managed runtimes, .zip deployment packages and layers.

Facts relied upon (checked 2026-09-21):
- python3.12+ runtimes run on Amazon Linux 2023, glibc 2.34 (``ldd --version`` in
  public.ecr.aws/lambda/python:3.14); older ones on Amazon Linux 2.
- Handler and dependencies at the root of the function .zip; layer content under
  ``python/``; /var/runtime/bootstrap.py puts /var/task first, then
  /opt/python/lib/pythonX.Y/site-packages and /opt/python, and only when PYTHONPATH is
  not set on the function.
- Quotas: 50 MB zipped for a direct upload, 250 MB unzipped including layers
  (262144000 bytes), 5 layers per function.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import ClassVar

from faaspack.errors import Finding, Level
from faaspack.targets.base import ArtifactSpec, BuiltArtifact, Target, VerifySpec

DIRECT_UPLOAD_LIMIT = 50 * 1024 * 1024
UNZIPPED_LIMIT = 262_144_000
CONTAINER_IMAGE_HINT = 200 * 1024 * 1024

_SDK_HINT = (
    "provided by the Lambda runtime with a version AWS may change at any time; AWS "
    "recommends bundling it: add it to your project dependencies to pin it"
)


class LambdaTarget(Target):
    name: ClassVar[str] = "lambda"
    description: ClassVar[str] = "AWS Lambda .zip function and/or layer (Python managed runtime)"
    architectures: ClassVar[tuple[str, ...]] = ("x86_64", "arm64")
    default_architectures: ClassVar[tuple[str, ...]] = ("x86_64",)  # AWS default
    layouts: ClassVar[tuple[str, ...]] = ("function", "layer", "split")
    default_layout: ClassVar[str] = "function"
    default_python: ClassVar[str] = "3.14"
    libc: ClassVar[str] = "gnu"
    runtime_provided: ClassVar[Mapping[str, str | None]] = MappingProxyType(
        {
            "boto3": _SDK_HINT,
            "botocore": _SDK_HINT,
            "s3transfer": _SDK_HINT,
            "jmespath": _SDK_HINT,
            "dateutil": _SDK_HINT,
            "urllib3": _SDK_HINT,
            "six": _SDK_HINT,
            "simplejson": _SDK_HINT,
            "awslambdaric": None,
            "snapshot_restore_py": None,  # SnapStart hooks, documented as part of the runtime
        }
    )

    def python_platform(self, *, python: str, architecture: str) -> str:
        cpu = {"x86_64": "x86_64", "arm64": "aarch64"}[architecture]
        minor = int(python.split(".")[1])
        # Amazon Linux 2023 (glibc 2.34) from python3.12, Amazon Linux 2 (glibc 2.26) before.
        policy = "manylinux_2_34" if minor >= 12 else "manylinux2014"
        return f"{cpu}-{policy}"

    def plan(self, layout: str) -> list[ArtifactSpec]:
        function = ArtifactSpec("function", (("code", ""), ("deps", "")), "/var/task/")
        if layout == "function":
            return [function]
        if layout == "layer":
            return [ArtifactSpec("layer", (("deps", "python/"), ("code", "python/")), "/opt/")]
        return [
            ArtifactSpec("function", (("code", ""),), "/var/task/"),
            ArtifactSpec("layer", (("deps", "python/"),), "/opt/"),
        ]

    def check_sizes(self, artifacts: Sequence[BuiltArtifact]) -> list[Finding]:
        findings: list[Finding] = []
        for artifact in artifacts:
            if artifact.size > DIRECT_UPLOAD_LIMIT:
                findings.append(
                    Finding(
                        "FP201",
                        Level.WARNING,
                        f"{artifact.kind} zip is {artifact.size / 2**20:.1f} MiB: above the 50 MB "
                        "direct upload quota, upload it through Amazon S3",
                    )
                )
        total = sum(a.unzipped_size for a in artifacts)
        if total > UNZIPPED_LIMIT:
            findings.append(
                Finding(
                    "FP202",
                    Level.ERROR,
                    f"unzipped size {total / 1e6:.1f} MB exceeds the 250 MB Lambda quota "
                    "(262144000 bytes, function and layers together)",
                )
            )
        elif total > CONTAINER_IMAGE_HINT:
            findings.append(
                Finding(
                    "FP203",
                    Level.INFO,
                    f"unzipped size {total / 2**20:.0f} MiB: past ~200 MB, a container image "
                    "function may start faster than a .zip (single public benchmark, measure it)",
                )
            )
        if any(a.kind == "layer" for a in artifacts):
            findings.append(
                Finding(
                    "FP204",
                    Level.INFO,
                    "function code, all its layers and this layer must fit together in 250 MB "
                    "unzipped",
                )
            )
        return findings

    def notes(self, layout: str) -> list[Finding]:
        if layout == "function":
            return []
        return [
            Finding(
                "FP301",
                Level.INFO,
                "layers are added to sys.path only when PYTHONPATH is not set on the function; "
                "attach a layer built for the same architecture as the function",
            )
        ]

    def verify_spec(self, *, python: str, architecture: str) -> VerifySpec:
        site_packages = f"/opt/python/lib/python{python}/site-packages"
        return VerifySpec(
            image=f"public.ecr.aws/lambda/python:{python}",
            docker_platform={"x86_64": "linux/amd64", "arm64": "linux/arm64"}[architecture],
            python_executable=f"/var/lang/bin/python{python}",
            mounts={"function": "/var/task", "layer": "/opt"},
            sys_path=["/var/task", site_packages, "/opt/python"],
            site_dirs=["/var/task", site_packages, "/opt/python"],
        )
