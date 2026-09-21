"""Build orchestration: ``bundle`` (uv project -> zip artifacts) and ``optimize``
(post-process an installed tree in place, e.g. inside a Dockerfile)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from email.parser import HeaderParser
from pathlib import Path
from typing import Any

from faaspack import __version__
from faaspack.archive import ArchiveInfo, write_zip
from faaspack.binaries import StripReport, check_binaries, host_architecture, strip_binaries
from faaspack.bytecode import compile_tree
from faaspack.config import Options, Project, load_project, resolve_options
from faaspack.errors import FaaspackError, Finding, Level
from faaspack.imports import scan_imports, top_level_modules
from faaspack.prune import Pruner, PruneReport, tree_size
from faaspack.targets import available_targets, get_target
from faaspack.targets.base import BuiltArtifact, Stage, Target
from faaspack.uvcli import Uv

Log = Callable[[str], None]


def _silent(_: str) -> None:
    return None


@dataclass
class Distribution:
    name: str
    version: str
    tags: list[str]
    stage: Stage

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "tags": self.tags, "stage": self.stage}


@dataclass
class ArtifactResult:
    kind: str
    archive: ArchiveInfo

    def as_dict(self, base: Path) -> dict[str, Any]:
        a = self.archive
        return {
            "kind": self.kind,
            "path": a.path.relative_to(base).as_posix(),
            "size": a.size,
            "unzipped_size": a.unzipped_size,
            "files": a.files,
            "sha256": a.sha256,
            "sha256_base64": a.sha256_base64,
        }


@dataclass
class ArchBuild:
    architecture: str
    platform: str
    artifacts: list[ArtifactResult] = field(default_factory=list)
    distributions: list[Distribution] = field(default_factory=list)
    modules: dict[str, list[str]] = field(default_factory=dict)
    contributors: list[tuple[str, int]] = field(default_factory=list)
    prune: PruneReport = field(default_factory=PruneReport)
    strip: StripReport | None = None
    compile_failures: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)


@dataclass
class BundleResult:
    target: str
    layout: str
    python: str
    manifest: Path
    builds: list[ArchBuild]
    findings: list[Finding]

    def all_findings(self) -> list[Finding]:
        return [*self.findings, *(f for b in self.builds for f in b.findings)]


def _read_distributions(root: Path, stage: Stage) -> list[Distribution]:
    result = []
    for dist_info in sorted(root.glob("*.dist-info")):
        metadata = dist_info / "METADATA"
        if not metadata.is_file():
            continue
        headers = HeaderParser().parsestr(metadata.read_text(encoding="utf-8", errors="replace"))
        wheel = dist_info / "WHEEL"
        tags: list[str] = []
        if wheel.is_file():
            wheel_headers = HeaderParser().parsestr(wheel.read_text(encoding="utf-8"))
            tags = wheel_headers.get_all("Tag") or []
        result.append(
            Distribution(
                headers.get("Name", dist_info.name), headers.get("Version", "?"), tags, stage
            )
        )
    return result


def _has_requirements(requirements: Path) -> bool:
    for line in requirements.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "-")):
            return True
    return False


def _stdlib_names(python: Path) -> set[str]:
    proc = subprocess.run(
        [str(python), "-c", "import json, sys; print(json.dumps(sorted(sys.stdlib_module_names)))"],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(json.loads(proc.stdout))


def _copy_code(project: Project, paths: Sequence[str], destination: Path) -> None:
    for item in paths:
        source = (project.root / item).resolve()
        if not source.exists():
            raise FaaspackError(f"code path not found: {item}")
        target = destination / source.name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)


def _contributors(stages: dict[Stage, Path]) -> list[tuple[str, int]]:
    sizes: dict[str, int] = {}
    for root in stages.values():
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            name = entry.name
            key = name.split("-")[0] if name.endswith(".dist-info") else name.split(".")[0]
            sizes[key] = sizes.get(key, 0) + tree_size(entry)[0]
    return sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))


def _base_name(project: Project, options: Options, wheel: Path | None) -> str:
    name = options.name or project.name
    version = project.version
    if version is None and wheel is not None:
        version = wheel.name.split("-")[1]
    slug = re.sub(r"[-_.]+", "-", name).lower()
    return f"{slug}-{version}" if version else slug


def _import_findings(
    target: Target, options: Options, code_dir: Path, known: set[str]
) -> list[Finding]:
    if options.import_check == "ignore":
        return []
    level = Level.ERROR if options.import_check == "error" else Level.WARNING
    findings = []
    for module, places in sorted(scan_imports([code_dir]).items()):
        if module in known or module in options.ignore_imports:
            continue
        where = ", ".join(places[:3]) + (" ..." if len(places) > 3 else "")
        if module in target.runtime_provided:
            hint = target.runtime_provided[module]
            if hint is not None:
                findings.append(
                    Finding(
                        "FP402",
                        Level.WARNING,
                        f"'{module}' is imported ({where}) but not bundled: {hint}",
                    )
                )
            continue
        findings.append(
            Finding(
                "FP401",
                level,
                f"'{module}' is imported ({where}) but provided neither by the bundle nor by the "
                "standard library; add it to your dependencies or to ignore-imports",
            )
        )
    return findings


def _build_architecture(
    *,
    arch: str,
    project: Project,
    target: Target,
    options: Options,
    layout: str,
    python: str,
    python_path: Path,
    stdlib: set[str],
    requirements: Path | None,
    wheel: Path | None,
    work: Path,
    out_dir: Path,
    base_name: str,
    uv: Uv,
    log: Log,
) -> ArchBuild:
    platform = target.python_platform(python=python, architecture=arch)
    build = ArchBuild(architecture=arch, platform=platform)
    stages: dict[Stage, Path] = {"code": work / arch / "code", "deps": work / arch / "deps"}
    for directory in stages.values():
        directory.mkdir(parents=True)

    log(f"[{arch}] installing locked dependencies for {platform}")
    if requirements is not None:
        uv.install_target(
            project.root,
            stages["deps"],
            python=python,
            platform=platform,
            requirements=requirements,
            only_binary=options.only_binary,
        )
    if wheel is not None:
        uv.install_target(
            project.root,
            stages["code"],
            python=python,
            platform=platform,
            wheels=[wheel],
            only_binary=False,
        )
    else:
        _copy_code(project, options.code or (), stages["code"])
    build.distributions = [
        *_read_distributions(stages["deps"], "deps"),
        *_read_distributions(stages["code"], "code"),
    ]

    pruner = Pruner(
        trim_dist_info=options.trim_dist_info,
        prune_tests=options.prune_tests,
        prune_scripts=options.prune_scripts,
        suffixes=options.prune_suffixes,
        keep=options.keep,
    )
    for directory in stages.values():
        build.prune.merge(pruner.prune(directory))

    if options.strip:
        log(f"[{arch}] stripping native libraries ({options.strip})")
        build.strip = strip_binaries(
            stages["deps"],
            architecture=arch,
            mode=options.strip,
            include_vendored_libs=options.strip_vendored_libs,
            tool=options.strip_tool,
        )
        for failure in build.strip.failures:
            build.findings.append(Finding("FP103", Level.WARNING, f"strip failed: {failure}"))
    for directory in stages.values():
        build.findings += check_binaries(directory, architecture=arch, libc=target.libc)

    plan = target.plan(layout)
    if options.compile:
        log(f"[{arch}] compiling bytecode (unchecked-hash) with Python {python}")
        for spec in plan:
            for stage, prefix in spec.parts:
                runtime = spec.runtime_root + prefix if spec.runtime_root is not None else None
                report = compile_tree(python_path, stages[stage], runtime_path=runtime)
                build.compile_failures += report.failures
        if build.compile_failures:
            build.findings.append(
                Finding(
                    "FP104",
                    Level.INFO,
                    f"{len(build.compile_failures)} file(s) are not valid Python {python} and stay "
                    f"as sources (templates, Python 2 files...): {build.compile_failures[:3]}",
                )
            )

    code_names = top_level_modules(stages["code"])
    deps_names = top_level_modules(stages["deps"])
    build.modules = {"code": code_names, "deps": deps_names}
    overlap = sorted(set(code_names) & set(deps_names))
    if overlap:
        same_zip = any({s for s, _ in spec.parts} == {"code", "deps"} for spec in plan)
        build.findings.append(
            Finding(
                "FP105",
                Level.ERROR if same_zip else Level.WARNING,
                f"project code and dependencies both provide {overlap}"
                + ("" if same_zip else " (the function code shadows the layer at runtime)"),
            )
        )

    for spec in plan:
        parts = [(stages[stage], prefix) for stage, prefix in spec.parts]
        destination = out_dir / f"{base_name}-{target.name}-{spec.kind}-{arch}.zip"
        try:
            info = write_zip(
                destination,
                parts,
                epoch=options.epoch(),
                compression_level=options.compression_level,
            )
        except ValueError as exc:
            raise FaaspackError(str(exc)) from exc
        build.artifacts.append(ArtifactResult(spec.kind, info))
        log(f"[{arch}] wrote {destination}")
    build.findings += target.check_sizes(
        [BuiltArtifact(a.kind, a.archive.size, a.archive.unzipped_size) for a in build.artifacts]
    )
    known = stdlib | set(code_names) | set(deps_names) | {"__future__"}
    build.findings += _import_findings(target, options, stages["code"], known)
    build.contributors = _contributors(stages)
    return build


def bundle(
    project_root: Path,
    target_name: str,
    overrides: dict[str, Any] | None = None,
    *,
    uv: Uv | None = None,
    log: Log = _silent,
    keep_work_dir: Path | None = None,
) -> BundleResult:
    project = load_project(project_root)
    target = get_target(target_name)
    options = resolve_options(project, set(available_targets()), target_name, overrides)
    python = options.python or target.default_python
    layout = options.layout or target.default_layout
    architectures = options.architectures or target.default_architectures
    target.validate(python=python, layout=layout, architectures=architectures)
    if not project.lockfile.is_file():
        raise FaaspackError(f"{project.lockfile} not found: run `uv lock` first")
    if options.code is None and not project.has_build_system:
        raise FaaspackError(
            "the project has no [build-system]: list the files and directories to ship "
            "with `code = [...]` in [tool.faaspack]"
        )
    uv = uv or Uv()
    out_dir = project.root / options.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="faaspack-") as tmp:
        work = Path(tmp)
        log(f"exporting {project.lockfile.name} (frozen, no dev groups)")
        requirements_file = work / "requirements.txt"
        uv.export_requirements(
            project.root,
            requirements_file,
            package=options.package,
            extras=options.extras,
            groups=options.groups,
            exclude=options.exclude_packages,
        )
        requirements = requirements_file if _has_requirements(requirements_file) else None
        wheel = None
        if options.code is None:
            log("building the project wheel")
            wheel = uv.build_wheel(project.root, work / "wheel", package=options.package)
        python_path = uv.find_python(python)
        stdlib = _stdlib_names(python_path)
        base_name = _base_name(project, options, wheel)

        builds = [
            _build_architecture(
                arch=arch,
                project=project,
                target=target,
                options=options,
                layout=layout,
                python=python,
                python_path=python_path,
                stdlib=stdlib,
                requirements=requirements,
                wheel=wheel,
                work=work,
                out_dir=out_dir,
                base_name=base_name,
                uv=uv,
                log=log,
            )
            for arch in architectures
        ]
        if keep_work_dir is not None:
            shutil.copytree(work, keep_work_dir, dirs_exist_ok=True)

    result = BundleResult(
        target=target.name,
        layout=layout,
        python=python,
        manifest=out_dir / f"{base_name}-{target.name}.manifest.json",
        builds=builds,
        findings=target.notes(layout),
    )
    _write_manifest(result, project, options, out_dir)
    return result


def _write_manifest(
    result: BundleResult, project: Project, options: Options, out_dir: Path
) -> None:
    data = {
        "tool": {"name": "faaspack", "version": __version__},
        "project": {"name": project.name, "version": project.version},
        "target": result.target,
        "layout": result.layout,
        "python": result.python,
        "source_date_epoch": options.epoch(),
        "options": {
            "only_binary": options.only_binary,
            "compile": options.compile,
            "trim_dist_info": options.trim_dist_info,
            "prune_tests": options.prune_tests,
            "strip": options.strip,
        },
        "findings": [f.as_dict() for f in result.findings],
        "builds": [
            {
                "architecture": b.architecture,
                "platform": b.platform,
                "artifacts": [a.as_dict(out_dir) for a in b.artifacts],
                "distributions": [d.as_dict() for d in b.distributions],
                "modules": b.modules,
                "pruned_bytes": b.prune.removed_bytes,
                "stripped_bytes": b.strip.saved_bytes if b.strip else 0,
                "findings": [f.as_dict() for f in b.findings],
            }
            for b in result.builds
        ],
    }
    result.manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@dataclass
class OptimizeResult:
    prune: PruneReport
    strip: StripReport | None
    compile_failures: list[str]


def optimize(
    directories: Sequence[Path],
    *,
    python: str,
    options: Options,
    architecture: str | None = None,
    uv: Uv | None = None,
) -> OptimizeResult:
    """Prune, optionally strip and compile trees in place (their final location)."""
    candidate = Path(python)
    if candidate.is_file():
        python_path = candidate
    elif re.fullmatch(r"3\.\d+", python):
        python_path = (uv or Uv()).find_python(python)
    else:
        raise FaaspackError(f"--python must be an interpreter path or a version, got {python!r}")
    pruner = Pruner(
        trim_dist_info=options.trim_dist_info,
        prune_tests=options.prune_tests,
        prune_scripts=False,
        suffixes=options.prune_suffixes,
        keep=options.keep,
    )
    result = OptimizeResult(prune=PruneReport(), strip=None, compile_failures=[])
    for directory in directories:
        if not directory.is_dir():
            raise FaaspackError(f"not a directory: {directory}")
        result.prune.merge(pruner.prune(directory))
        if options.strip:
            report = strip_binaries(
                directory,
                architecture=architecture or host_architecture(),
                mode=options.strip,
                include_vendored_libs=options.strip_vendored_libs,
                tool=options.strip_tool,
            )
            if result.strip is None:
                result.strip = report
            else:
                result.strip.stripped += report.stripped
                result.strip.saved_bytes += report.saved_bytes
                result.strip.failures += report.failures
        if options.compile:
            report_c = compile_tree(
                python_path, directory.resolve(), runtime_path=None, relocate=False
            )
            result.compile_failures += report_c.failures
    return result
