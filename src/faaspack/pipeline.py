"""``faaspack bundle``: from a uv project to deterministic zip artifacts.

For each architecture, the dependencies (``deps`` stage) and the project code (``code``
stage) are installed into separate directories, pruned, checked, compiled, then zipped
according to the target layout.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from email.parser import HeaderParser
from pathlib import Path
from typing import Any

from faaspack.archive import write_zip
from faaspack.binaries import StripReport, check_binaries, strip_binaries
from faaspack.bytecode import compile_tree
from faaspack.config import Options, Project, load_project, resolve_options
from faaspack.errors import FaaspackError, Finding, Level
from faaspack.imports import scan_imports, top_level_modules
from faaspack.manifest import write_manifest
from faaspack.prune import Pruner, PruneReport, tree_size
from faaspack.results import ArchBuild, ArtifactResult, BundleResult, Distribution
from faaspack.targets import available_targets, get_target
from faaspack.targets.base import ArtifactSpec, BuiltArtifact, Stage, Target
from faaspack.uvcli import InstallPlatform, LockSelection, Uv, UvCommands

Log = Callable[[str], None]
Stages = dict[Stage, Path]

_LOCATIONS_SHOWN = 3  # import sites quoted in an FP401/FP402 message


def _silent(_: str) -> None:
    return None


@dataclass(frozen=True)
class BundleSettings:
    """The resolved inputs of a bundle: options merged, defaults of the target applied."""

    project: Project
    target: Target
    options: Options
    python: str
    layout: str
    architectures: tuple[str, ...]


@dataclass(frozen=True)
class BuildContext:
    """Everything the per-architecture builds of one bundle share."""

    settings: BundleSettings
    requirements: Path | None  # None when the project has no dependency
    wheel: Path | None  # None when the code is copied (``code`` option)
    python_path: Path  # interpreter of the target version, used to compile
    stdlib: frozenset[str]
    base_name: str  # prefix of the artifact file names
    work: Path
    out_dir: Path
    uv: UvCommands
    log: Log


def bundle(
    project_root: Path,
    target_name: str,
    overrides: dict[str, Any] | None = None,
    *,
    uv: UvCommands | None = None,
    log: Log = _silent,
) -> BundleResult:
    """Build the artifacts of ``target_name`` for the project, and write their manifest."""
    settings = resolve_settings(project_root, target_name, overrides)
    uv = uv or Uv()
    out_dir = settings.project.root / settings.options.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="faaspack-") as tmp:
        context = _prepare(settings, Path(tmp), out_dir, uv, log)
        builds = [_build_architecture(context, arch) for arch in settings.architectures]
    target = settings.target
    result = BundleResult(
        target=target.name,
        layout=settings.layout,
        python=settings.python,
        manifest=out_dir / f"{context.base_name}-{target.name}.manifest.json",
        builds=builds,
        findings=target.notes(settings.layout),
    )
    write_manifest(result, settings.project, settings.options)
    return result


def resolve_settings(
    project_root: Path, target_name: str, overrides: dict[str, Any] | None = None
) -> BundleSettings:
    """Load the project and merge its options; raise on anything that would fail later."""
    project = load_project(project_root)
    target = get_target(target_name)
    options = resolve_options(project, set(available_targets()), target_name, overrides)
    settings = BundleSettings(
        project=project,
        target=target,
        options=options,
        python=options.python or target.default_python,
        layout=options.layout or target.default_layout,
        architectures=options.architectures or target.default_architectures,
    )
    target.validate(
        python=settings.python, layout=settings.layout, architectures=settings.architectures
    )
    if not project.lockfile.is_file():
        raise FaaspackError(f"{project.lockfile} not found: run `uv lock` first")
    if options.code is None and not project.has_build_system:
        raise FaaspackError(
            "the project has no [build-system]: list the files and directories to ship "
            "with `code = [...]` in [tool.faaspack]"
        )
    return settings


def _prepare(
    settings: BundleSettings, work: Path, out_dir: Path, uv: UvCommands, log: Log
) -> BuildContext:
    """Export the lock, build the project wheel and find the compiler: once per bundle."""
    project, options = settings.project, settings.options
    log(f"exporting {project.lockfile.name} (frozen, no dev groups)")
    requirements = work / "requirements.txt"
    selection = LockSelection(
        package=options.package,
        extras=options.extras,
        groups=options.groups,
        exclude=options.exclude_packages,
    )
    uv.export_requirements(project.root, requirements, selection)
    wheel = None
    if options.code is None:
        log("building the project wheel")
        wheel = uv.build_wheel(project.root, work / "wheel", package=options.package)
    python_path = uv.find_python(settings.python)
    return BuildContext(
        settings=settings,
        requirements=requirements if _has_requirements(requirements) else None,
        wheel=wheel,
        python_path=python_path,
        stdlib=_stdlib_names(python_path),
        base_name=_base_name(project, options, wheel),
        work=work,
        out_dir=out_dir,
        uv=uv,
        log=log,
    )


def _build_architecture(context: BuildContext, arch: str) -> ArchBuild:
    settings = context.settings
    target, options = settings.target, settings.options
    platform = InstallPlatform(
        settings.python, target.python_platform(python=settings.python, architecture=arch)
    )
    build = ArchBuild(architecture=arch, platform=platform.platform)
    context.log(f"[{arch}] installing locked dependencies for {platform.platform}")
    stages = _install_stages(context, arch, platform)
    build.distributions = [
        *_read_distributions(stages["deps"], "deps"),
        *_read_distributions(stages["code"], "code"),
    ]
    build.prune = _prune(options, stages)
    build.strip = _strip(context, arch, stages["deps"])
    if build.strip is not None:
        build.findings += _strip_findings(build.strip)
    for directory in stages.values():
        build.findings += check_binaries(directory, architecture=arch, libc=target.libc)

    plan = target.plan(settings.layout)
    if options.compile:
        build.compile_failures = _compile(context, arch, stages, plan)
        build.findings += _compile_findings(build.compile_failures, settings.python)

    build.modules = {stage: top_level_modules(stages[stage]) for stage in ("code", "deps")}
    build.findings += _overlap_findings(build.modules, plan)
    build.artifacts = [_write_artifact(context, arch, stages, spec) for spec in plan]
    build.findings += target.check_sizes(
        [BuiltArtifact(a.kind, a.archive.size, a.archive.unzipped_size) for a in build.artifacts]
    )
    known = context.stdlib | {name for names in build.modules.values() for name in names}
    build.findings += _import_findings(target, options, stages["code"], known | {"__future__"})
    build.contributors = _contributors(stages)
    return build


def _install_stages(context: BuildContext, arch: str, platform: InstallPlatform) -> Stages:
    project, options = context.settings.project, context.settings.options
    stages: Stages = {"code": context.work / arch / "code", "deps": context.work / arch / "deps"}
    for directory in stages.values():
        directory.mkdir(parents=True)
    if context.requirements is not None:
        context.uv.install_requirements(
            project.root,
            stages["deps"],
            context.requirements,
            platform=platform,
            only_binary=options.only_binary,
        )
    if context.wheel is not None:
        context.uv.install_wheels(project.root, stages["code"], [context.wheel], platform=platform)
    else:
        _copy_code(project, options.code or (), stages["code"])
    return stages


def _prune(options: Options, stages: Stages) -> PruneReport:
    pruner = Pruner(
        trim_dist_info=options.trim_dist_info,
        prune_tests=options.prune_tests,
        prune_scripts=options.prune_scripts,
        suffixes=options.prune_suffixes,
        keep=options.keep,
    )
    report = PruneReport()
    for directory in stages.values():
        report.merge(pruner.prune(directory))
    return report


def _strip(context: BuildContext, arch: str, deps: Path) -> StripReport | None:
    options = context.settings.options
    if not options.strip:
        return None
    context.log(f"[{arch}] stripping native libraries ({options.strip})")
    return strip_binaries(
        deps,
        architecture=arch,
        mode=options.strip,
        include_vendored_libs=options.strip_vendored_libs,
        tool=options.strip_tool,
    )


def _strip_findings(report: StripReport) -> list[Finding]:
    return [Finding("FP103", Level.WARNING, f"strip failed: {f}") for f in report.failures]


def _compile(
    context: BuildContext, arch: str, stages: Stages, plan: Sequence[ArtifactSpec]
) -> list[str]:
    """Compile each stage with ``co_filename`` pointing where its archive is extracted."""
    python = context.settings.python
    context.log(f"[{arch}] compiling bytecode (unchecked-hash) with Python {python}")
    failures: list[str] = []
    for spec in plan:
        for stage, prefix in spec.parts:
            failures += compile_tree(
                context.python_path, stages[stage], runtime_path=_runtime_path(spec, prefix)
            ).failures
    return failures


def _runtime_path(spec: ArtifactSpec, prefix: str) -> str | None:
    """Where a stage lives at runtime, recorded as co_filename: absolute when the platform
    documents its extraction root, relative to the archive root otherwise (Scaleway)."""
    if spec.runtime_root is None:
        return prefix or None
    return spec.runtime_root + prefix


def _compile_findings(failures: Sequence[str], python: str) -> list[Finding]:
    if not failures:
        return []
    return [
        Finding(
            "FP104",
            Level.INFO,
            f"{len(failures)} file(s) are not valid Python {python} and stay "
            f"as sources (templates, Python 2 files...): {list(failures[:3])}",
        )
    ]


def _overlap_findings(modules: dict[str, list[str]], plan: Sequence[ArtifactSpec]) -> list[Finding]:
    """Code and dependencies providing the same top-level module."""
    overlap = sorted(set(modules["code"]) & set(modules["deps"]))
    if not overlap:
        return []
    same_zip = any({stage for stage, _ in spec.parts} == {"code", "deps"} for spec in plan)
    if same_zip:
        return [
            Finding("FP105", Level.ERROR, f"project code and dependencies both provide {overlap}")
        ]
    return [
        Finding(
            "FP105",
            Level.WARNING,
            f"project code and dependencies both provide {overlap} "
            "(the function code shadows the layer at runtime)",
        )
    ]


def _write_artifact(
    context: BuildContext, arch: str, stages: Stages, spec: ArtifactSpec
) -> ArtifactResult:
    options, target = context.settings.options, context.settings.target
    destination = context.out_dir / f"{context.base_name}-{target.name}-{spec.kind}-{arch}.zip"
    try:
        info = write_zip(
            destination,
            [(stages[stage], prefix) for stage, prefix in spec.parts],
            epoch=options.epoch(),
            compression_level=options.compression_level,
        )
    except ValueError as exc:
        raise FaaspackError(str(exc)) from exc
    context.log(f"[{arch}] wrote {destination}")
    return ArtifactResult(spec.kind, info)


def _import_findings(
    target: Target, options: Options, code_dir: Path, known: set[str] | frozenset[str]
) -> list[Finding]:
    """FP401 for imports nothing provides, FP402 for those only the runtime provides."""
    if options.import_check == "ignore":
        return []
    level = Level.ERROR if options.import_check == "error" else Level.WARNING
    findings = []
    for module, places in sorted(scan_imports([code_dir]).items()):
        if module in known or module in options.ignore_imports:
            continue
        where = ", ".join(places[:_LOCATIONS_SHOWN])
        if len(places) > _LOCATIONS_SHOWN:
            where += " ..."
        if module not in target.runtime_provided:
            findings.append(
                Finding(
                    "FP401",
                    level,
                    f"'{module}' is imported ({where}) but provided neither by the bundle nor by "
                    "the standard library; add it to your dependencies or to ignore-imports",
                )
            )
        elif (hint := target.runtime_provided[module]) is not None:
            findings.append(
                Finding(
                    "FP402",
                    Level.WARNING,
                    f"'{module}' is imported ({where}) but not bundled: {hint}",
                )
            )
    return findings


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
    """Whether an exported requirements file lists at least one package."""
    for line in requirements.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "-")):
            return True
    return False


def _stdlib_names(python: Path) -> frozenset[str]:
    script = "import json, sys; print(json.dumps(sorted(sys.stdlib_module_names)))"
    try:
        proc = subprocess.run(
            [str(python), "-c", script], capture_output=True, text=True, check=True
        )
        return frozenset(json.loads(proc.stdout))
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise FaaspackError(f"{python} cannot list its standard library modules: {exc}") from exc


def _copy_code(project: Project, paths: Sequence[str], destination: Path) -> None:
    copied: dict[str, str] = {}  # name in the archive -> code path
    for item in paths:
        source = (project.root / item).resolve()
        if not source.exists():
            raise FaaspackError(f"code path not found: {item}")
        if source.name in copied:
            raise FaaspackError(
                f"code paths {copied[source.name]!r} and {item!r} would both be copied "
                f"as {source.name!r}"
            )
        copied[source.name] = item
        target = destination / source.name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, target)


def _contributors(stages: Stages) -> list[tuple[str, int]]:
    """Top-level entries by size, a distribution's dist-info counted with its package."""
    sizes: dict[str, int] = {}
    for root in stages.values():
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
