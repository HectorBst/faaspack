"""Command line interface."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from faaspack import __version__
from faaspack.config import Options, common_options, load_project
from faaspack.errors import FaaspackError, Finding, Level
from faaspack.optimize import optimize
from faaspack.pipeline import bundle
from faaspack.results import BundleResult
from faaspack.targets import available_targets
from faaspack.uvcli import Uv
from faaspack.verify import Docker, VerifyOptions, verify

EXIT_OK, EXIT_FINDINGS, EXIT_FATAL = 0, 1, 2


def _size(n: int) -> str:
    return f"{n / 2**20:.1f} MiB" if n >= 2**20 else f"{n / 1024:.0f} KiB"


def _archs(values: Sequence[str] | None) -> tuple[str, ...] | None:
    if not values:
        return None
    return tuple(a.strip() for v in values for a in v.split(",") if a.strip())


def _echo(message: str) -> None:
    print(message, flush=True)


def _silent(_: str) -> None:
    return None


def _report(findings: Sequence[Finding], *, strict: bool) -> int:
    for text in dict.fromkeys(str(f) for f in findings):  # same finding for every arch
        print(text)
    failing = {Level.ERROR} | ({Level.WARNING} if strict else set())
    return EXIT_FINDINGS if any(f.level in failing for f in findings) else EXIT_OK


def _print_bundle(result: BundleResult, top: int) -> None:
    for build in result.builds:
        print(
            f"\n{result.target} / {result.layout} / {build.architecture} "
            f"(Python {result.python}, {build.platform})"
        )
        for artifact in build.artifacts:
            a = artifact.archive
            print(
                f"  {artifact.kind:<9} {a.path}  zip {_size(a.size)}, "
                f"unzipped {_size(a.unzipped_size)}, {a.files} files, sha256 {a.sha256[:12]}"
            )
        extra = f", stripped {_size(build.strip.saved_bytes)}" if build.strip else ""
        print(
            f"  {len(build.distributions)} distributions, pruned "
            f"{_size(build.prune.removed_bytes)}{extra}"
        )
        if top:
            biggest = ", ".join(f"{n} {_size(s)}" for n, s in build.contributors[:top])
            print(f"  largest: {biggest}")
    print(f"\nmanifest: {result.manifest}")


def cmd_bundle(args: argparse.Namespace) -> int:
    overrides: dict[str, Any] = {
        "layout": args.layout,
        "architectures": _archs(args.arch),
        "python": args.python,
        "output_dir": args.output_dir,
        "strip": args.strip,
        "compile": False if args.no_compile else None,
    }
    uv = Uv(args.uv, echo=_echo if args.verbose else None)
    log = _silent if args.quiet else _echo
    result = bundle(Path(args.project), args.target, overrides, uv=uv, log=log)
    _print_bundle(result, 0 if args.quiet else 8)
    findings = result.all_findings()
    if args.verify:
        for report in verify(result.manifest, runtime=Docker(args.docker)):
            findings += report.findings
    print()
    return _report(findings, strict=args.strict)


def cmd_optimize(args: argparse.Namespace) -> int:
    options = Options()
    if args.from_config:
        options = common_options(load_project(Path()), set(available_targets()))
    overrides: dict[str, Any] = {
        "strip": args.strip,
        "compile": False if args.no_compile else None,
        "prune_tests": False if args.no_prune_tests else None,
        "trim_dist_info": False if args.no_trim_dist_info else None,
        "keep": tuple(args.keep) if args.keep else None,
        "strip_tool": args.strip_tool,
    }
    options = replace(options, **{k: v for k, v in overrides.items() if v is not None})
    result = optimize(
        [Path(d) for d in args.directories],
        python=args.python,
        options=options,
        architecture=args.arch,
    )
    stripped = f", stripped {_size(result.strip.saved_bytes)}" if result.strip else ""
    print(
        f"pruned {_size(result.prune.removed_bytes - result.prune.by_rule.get('pycache', 0))} "
        f"(old bytecode excluded){stripped}; {len(result.compile_failures)} file(s) left "
        "uncompiled"
    )
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    options = VerifyOptions(
        architectures=_archs(args.arch) or (),
        extra_imports=tuple(args.imports or ()),
        skip=tuple(args.skip or ()),
        import_code=args.import_code,
    )
    reports = verify(Path(args.manifest), options, runtime=Docker(args.docker))
    return _report([f for r in reports for f in r.findings], strict=args.strict)


def cmd_targets(_: argparse.Namespace) -> int:
    for name, cls in sorted(available_targets().items()):
        print(f"{name:<20} {cls.description}")
        print(
            f"{'':<20} layouts: {', '.join(cls.layouts)} (default {cls.default_layout}); "
            f"architectures: {', '.join(cls.architectures)}; python {cls.default_python}"
        )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="faaspack",
        description="Optimized Python packages for AWS Lambda and Scaleway Serverless Functions.",
    )
    parser.add_argument("--version", action="version", version=f"faaspack {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("bundle", help="build zip artifacts from a uv project (uses uv.lock)")
    b.add_argument("target", help="lambda or scaleway-functions (see `faaspack targets`)")
    b.add_argument("--project", default=".", help="project directory (default: .)")
    b.add_argument("--layout", help="lambda: function, layer or split")
    b.add_argument("--arch", action="append", help="x86_64, arm64 (repeat or comma-separate)")
    b.add_argument("--python", help="target Python minor version, e.g. 3.14")
    b.add_argument("--output-dir", help="default: dist/faaspack")
    b.add_argument("--strip", choices=["debug", "unneeded"], help="strip native libraries")
    b.add_argument("--no-compile", action="store_true", help="do not compile bytecode")
    b.add_argument("--verify", action="store_true", help="run `faaspack verify` afterwards")
    b.add_argument("--docker", default="docker", help="docker binary for --verify")
    b.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    b.add_argument("--uv", help="path to the uv binary")
    b.add_argument("-v", "--verbose", action="store_true", help="print uv commands")
    b.add_argument("-q", "--quiet", action="store_true")
    b.set_defaults(func=cmd_bundle)

    o = sub.add_parser(
        "optimize",
        help="prune and compile installed trees in place (e.g. a site-packages in a Dockerfile)",
    )
    o.add_argument("directories", nargs="+")
    o.add_argument(
        "--python",
        required=True,
        help="interpreter that will run the code (path), or a version for uv",
    )
    o.add_argument("--strip", choices=["debug", "unneeded"])
    o.add_argument("--strip-tool")
    o.add_argument("--arch", choices=["x86_64", "arm64"], help="default: host architecture")
    o.add_argument("--no-compile", action="store_true")
    o.add_argument("--no-prune-tests", action="store_true")
    o.add_argument("--no-trim-dist-info", action="store_true")
    o.add_argument("--keep", action="append", help="glob (relative to each directory) to keep")
    o.add_argument(
        "--from-config",
        action="store_true",
        help="read common options from ./pyproject.toml [tool.faaspack]",
    )
    o.set_defaults(func=cmd_optimize)

    v = sub.add_parser("verify", help="import the artifacts of a manifest in the runtime image")
    v.add_argument("manifest")
    v.add_argument("--arch", action="append")
    v.add_argument("--import", dest="imports", action="append", help="extra module to import")
    v.add_argument("--skip", action="append", help="module not to import")
    v.add_argument(
        "--import-code",
        action="store_true",
        help="also import the project's own top-level modules (runs init code)",
    )
    v.add_argument("--docker", default="docker")
    v.add_argument("--strict", action="store_true")
    v.set_defaults(func=cmd_verify)

    t = sub.add_parser("targets", help="list available targets")
    t.set_defaults(func=cmd_targets)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code: int = args.func(args)
    except FaaspackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FATAL
    return code
