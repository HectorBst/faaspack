"""``faaspack verify``: smoke test of built artifacts in the runtime container image.

Artifacts are extracted and mounted read-only where the platform puts them; the target
interpreter imports every top-level module of the dependencies with ``-v`` so that
modules compiled from source (missing or rejected bytecode) are counted.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from faaspack.errors import FaaspackError, Finding, Level
from faaspack.manifest import load_manifest
from faaspack.targets import get_target
from faaspack.targets.base import VerifySpec

_RESULT = "FAASPACK_RESULT "
_SCRIPT = """
import importlib, json, site, sys
paths, sites, modules = json.loads(sys.argv[1])
for p in reversed(paths):
    sys.path.insert(0, p)
for d in sites:
    site.addsitedir(d)
res = {}
for m in modules:
    try:
        importlib.import_module(m)
        res[m] = None
    except BaseException as e:
        res[m] = type(e).__name__ + ": " + str(e)[:300]
sys.stdout.write("FAASPACK_RESULT " + json.dumps(res) + "\\n")
"""
# `python -v` prints "# code object from /path/mod.py" (unquoted) when it compiles a source
# file, and "# code object from '/path/x.pyc'" when bytecode is used.
_FROM_SOURCE = re.compile(r"^# code object from (/\S+\.py)$")
_STDERR_LINES_SHOWN = 5


@dataclass(frozen=True)
class VerifyOptions:
    architectures: tuple[str, ...] = ()  # empty: every architecture of the manifest
    extra_imports: tuple[str, ...] = ()
    skip: tuple[str, ...] = ()
    import_code: bool = False  # also import the project modules (runs their init code)
    timeout: int = 900  # seconds per container, image pull included


class ContainerRuntime(Protocol):
    """Runs a container. :class:`Docker` is the real one; tests use fakes."""

    def run(self, args: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        """Run ``<runtime> run <args>``; raise ``subprocess.TimeoutExpired`` on timeout."""


class Docker:
    def __init__(self, binary: str = "docker") -> None:
        found = shutil.which(binary)
        if found is None:
            raise FaaspackError(f"{binary} not found: verify needs Docker")
        self.binary = found

    def run(self, args: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, "run", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


@dataclass
class VerifyReport:
    architecture: str
    image: str
    imported: int = 0
    failures: dict[str, str] = field(default_factory=dict)
    from_source: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def verify(
    manifest_path: Path,
    options: VerifyOptions | None = None,
    *,
    runtime: ContainerRuntime | None = None,
) -> list[VerifyReport]:
    """Import the artifacts of each build of the manifest in the runtime image."""
    runtime = runtime or Docker()
    options = options or VerifyOptions()
    manifest = load_manifest(manifest_path)
    target = get_target(manifest["target"])
    builds = [
        build
        for build in manifest["builds"]
        if not options.architectures or build["architecture"] in options.architectures
    ]
    if not builds:
        built = ", ".join(build["architecture"] for build in manifest["builds"])
        raise FaaspackError(
            f"{manifest_path} has no build for {options.architectures} (built: {built})"
        )
    reports = []
    for build in builds:
        spec = target.verify_spec(python=manifest["python"], architecture=build["architecture"])
        reports.append(_verify_build(build, spec, manifest_path.parent, options, runtime))
    return reports


def _verify_build(
    build: dict[str, Any],
    spec: VerifySpec,
    base: Path,
    options: VerifyOptions,
    runtime: ContainerRuntime,
) -> VerifyReport:
    arch = build["architecture"]
    report = VerifyReport(architecture=arch, image=spec.image)
    modules = _modules_to_import(build, options)
    with tempfile.TemporaryDirectory(prefix="faaspack-verify-") as tmp:
        mounts = _extract_artifacts(build, spec, base, Path(tmp))
        payload = json.dumps([list(spec.sys_path), list(spec.site_dirs), modules])
        args = [
            "--rm",
            "--network",
            "none",
            "--platform",
            spec.docker_platform,
            "--entrypoint",
            spec.python_executable,
            *_volume_args(mounts),
            spec.image,
            "-v",
            "-c",
            _SCRIPT,
            payload,
        ]
        try:
            proc = runtime.run(args, timeout=options.timeout)
        except subprocess.TimeoutExpired:
            report.findings.append(
                Finding("FP503", Level.WARNING, f"[{arch}] verification timed out")
            )
            return report
    _read_results(report, proc, roots=list(mounts.values()), spec=spec)
    return report


def _modules_to_import(build: dict[str, Any], options: VerifyOptions) -> list[str]:
    modules = [m for m in build["modules"]["deps"] if m not in options.skip]
    if options.import_code:
        modules += [m for m in build["modules"]["code"] if m not in options.skip]
    return modules + list(options.extra_imports)


def _extract_artifacts(
    build: dict[str, Any], spec: VerifySpec, base: Path, destination: Path
) -> dict[Path, str]:
    """Extract each artifact; return the extracted directories and their mount points."""
    mounts: dict[Path, str] = {}
    for artifact in build["artifacts"]:
        extracted = destination / artifact["kind"]
        with zipfile.ZipFile(base / artifact["path"]) as archive:
            archive.extractall(extracted)
        mounts[extracted] = spec.mounts[artifact["kind"]]
    return mounts


def _volume_args(mounts: dict[Path, str]) -> list[str]:
    return [arg for source, mount in mounts.items() for arg in ("-v", f"{source}:{mount}:ro")]


def _read_results(
    report: VerifyReport,
    proc: subprocess.CompletedProcess[str],
    *,
    roots: Sequence[str],
    spec: VerifySpec,
) -> None:
    arch = report.architecture
    result_line = next((ln for ln in proc.stdout.splitlines() if ln.startswith(_RESULT)), None)
    if result_line is None:
        tail = "\n".join(proc.stderr.strip().splitlines()[-_STDERR_LINES_SHOWN:])
        report.findings.append(
            Finding(
                "FP503",
                Level.WARNING,
                f"[{arch}] could not run {spec.image} ({spec.docker_platform}); "
                f"foreign architectures need QEMU emulation (binfmt): {tail}",
            )
        )
        return
    results: dict[str, str | None] = json.loads(result_line.removeprefix(_RESULT))
    report.imported = sum(1 for error in results.values() if error is None)
    report.failures = {m: e for m, e in results.items() if e is not None}
    report.from_source = _source_compiled(proc.stderr, roots)
    report.findings += [
        Finding("FP501", Level.ERROR, f"[{arch}] import {module} failed: {error}")
        for module, error in sorted(report.failures.items())
    ]
    if report.from_source:
        report.findings.append(
            Finding(
                "FP502",
                Level.WARNING,
                f"[{arch}] {len(report.from_source)} module(s) compiled from source at import "
                f"(bytecode missing or rejected), e.g. {report.from_source[:3]}",
            )
        )
    note = f" ({spec.note})" if spec.note else ""
    report.findings.append(
        Finding(
            "FP500",
            Level.INFO,
            f"[{arch}] {report.imported}/{len(results)} modules imported in {spec.image}, "
            f"{len(report.from_source)} compiled from source{note}",
        )
    )


def _source_compiled(stderr: str, roots: Sequence[str]) -> list[str]:
    """Source files under ``roots`` that ``python -v`` reports as compiled at import."""
    found = []
    for line in stderr.splitlines():
        match = _FROM_SOURCE.match(line.strip())
        if match and any(match.group(1).startswith(root.rstrip("/") + "/") for root in roots):
            found.append(match.group(1))
    return found
