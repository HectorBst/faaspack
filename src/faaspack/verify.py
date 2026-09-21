"""Smoke test of built artifacts in the runtime container image (``faaspack verify``).

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
from typing import Any

from faaspack.errors import FaaspackError, Finding, Level
from faaspack.targets import get_target

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


@dataclass
class VerifyReport:
    architecture: str
    image: str
    imported: int = 0
    failures: dict[str, str] = field(default_factory=dict)
    from_source: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


def _source_compiled(stderr: str, roots: Sequence[str]) -> list[str]:
    # `python -v` prints "# code object from /path/mod.py" (unquoted) when it compiles
    # a source file, and "# code object from '/path/x.pyc'" when bytecode is used.
    pattern = re.compile(r"^# code object from (/\S+\.py)$")
    found = []
    for line in stderr.splitlines():
        match = pattern.match(line.strip())
        if match and any(match.group(1).startswith(root.rstrip("/") + "/") for root in roots):
            found.append(match.group(1))
    return found


def verify(
    manifest_path: Path,
    *,
    docker: str = "docker",
    architectures: Sequence[str] = (),
    extra_imports: Sequence[str] = (),
    skip: Sequence[str] = (),
    import_code: bool = False,
    timeout: int = 900,
) -> list[VerifyReport]:
    docker_bin = shutil.which(docker)
    if docker_bin is None:
        raise FaaspackError(f"{docker} not found: verify needs Docker")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = get_target(manifest["target"])
    python = manifest["python"]
    base = manifest_path.parent
    reports = []
    for build in manifest["builds"]:
        arch = build["architecture"]
        if architectures and arch not in architectures:
            continue
        spec = target.verify_spec(python=python, architecture=arch)
        report = VerifyReport(architecture=arch, image=spec.image)
        modules = [m for m in build["modules"]["deps"] if m not in skip]
        if import_code:
            modules += [m for m in build["modules"]["code"] if m not in skip]
        modules += list(extra_imports)
        with tempfile.TemporaryDirectory(prefix="faaspack-verify-") as tmp:
            mounts: list[str] = []
            roots: list[str] = []
            for artifact in build["artifacts"]:
                extracted = Path(tmp) / artifact["kind"]
                with zipfile.ZipFile(base / artifact["path"]) as archive:
                    archive.extractall(extracted)
                mount = spec.mounts[artifact["kind"]]
                mounts += ["-v", f"{extracted}:{mount}:ro"]
                roots.append(mount)
            payload = json.dumps([list(spec.sys_path), list(spec.site_dirs), modules])
            cmd = [
                docker_bin,
                "run",
                "--rm",
                "--network",
                "none",
                "--platform",
                spec.docker_platform,
                "--entrypoint",
                spec.python_executable,
                *mounts,
                spec.image,
                "-v",
                "-c",
                _SCRIPT,
                payload,
            ]
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout, check=False
                )
            except subprocess.TimeoutExpired:
                report.findings.append(
                    Finding("FP503", Level.WARNING, f"[{arch}] verification timed out")
                )
                reports.append(report)
                continue
        result_line = next((ln for ln in proc.stdout.splitlines() if ln.startswith(_RESULT)), None)
        if result_line is None:
            tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
            report.findings.append(
                Finding(
                    "FP503",
                    Level.WARNING,
                    f"[{arch}] could not run {spec.image} ({spec.docker_platform}); "
                    f"foreign architectures need QEMU emulation (binfmt): {tail}",
                )
            )
            reports.append(report)
            continue
        results: dict[str, str | None] = json.loads(result_line.removeprefix(_RESULT))
        report.imported = sum(1 for error in results.values() if error is None)
        report.failures = {m: e for m, e in results.items() if e is not None}
        report.from_source = _source_compiled(proc.stderr, roots)
        for module, error in sorted(report.failures.items()):
            report.findings.append(
                Finding("FP501", Level.ERROR, f"[{arch}] import {module} failed: {error}")
            )
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
        reports.append(report)
    return reports
