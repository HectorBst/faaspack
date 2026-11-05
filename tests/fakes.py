"""Test doubles for the two external systems faaspack drives: uv and a container runtime."""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faaspack.binaries import ELF_MACHINES
from faaspack.uvcli import InstallPlatform, LockSelection

PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"
_CPU_TO_ARCH = {"x86_64": "x86_64", "aarch64": "arm64"}


def write_elf(path: Path, machine: int) -> Path:
    """Write the first bytes of a 64-bit little-endian ELF file for ``machine``."""
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4] = 2  # 64-bit
    header[5] = 1  # little endian
    header[18:20] = machine.to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(header))
    return path


def make_project(
    root: Path, *, handler: str = "", extra_toml: str = "", build_system: bool = True
) -> Path:
    """A uv project ``demo-app`` 1.0.0 with a ``demo_app`` package and an (unused) uv.lock."""
    root.mkdir(parents=True, exist_ok=True)
    system = '[build-system]\nrequires = ["uv_build"]\nbuild-backend = "uv_build"\n'
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo-app"\nversion = "1.0.0"\n\n'
        + (system if build_system else "")
        + extra_toml,
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    package = root / "src" / "demo_app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""Demo application."""\n', encoding="utf-8")
    (package / "handler.py").write_text(
        textwrap.dedent(handler)
        or "import json\n\n\ndef handler(event, context):\n    return json.dumps(event)\n",
        encoding="utf-8",
    )
    return root


@dataclass
class FakeUv:
    """Stands in for uv: "installs" small but realistic trees instead of real wheels.

    Each dependency is a package with a native module built for the requested platform
    (or for ``wrong_machine``), tests, stubs, a console script and a full dist-info.
    """

    dependencies: Sequence[str] = ("dep",)
    wrong_machine: int | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def export_requirements(self, project: Path, output: Path, selection: LockSelection) -> None:
        self.calls.append(("export", selection))
        lines = [f"{name}==1.0 \\\n    --hash=sha256:{'0' * 64}\n" for name in self.dependencies]
        output.write_text("# exported by a fake uv\n" + "".join(lines), encoding="utf-8")

    def build_wheel(self, project: Path, out_dir: Path, *, package: str | None) -> Path:
        self.calls.append(("build", package))
        out_dir.mkdir(parents=True)
        wheel = out_dir / "demo_app-1.0.0-py3-none-any.whl"
        wheel.write_bytes(b"")
        return wheel

    def install_requirements(
        self,
        project: Path,
        target_dir: Path,
        requirements: Path,
        *,
        platform: InstallPlatform,
        only_binary: bool,
    ) -> None:
        self.calls.append(("install-requirements", platform))
        cpu, _, rest = platform.platform.partition("-")
        libc = "musl" if "musl" in rest else "gnu"
        machine = self.wrong_machine or ELF_MACHINES[_CPU_TO_ARCH[cpu]]
        for name in self.dependencies:
            package = target_dir / name
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("import json\n", encoding="utf-8")
            (package / "__init__.pyi").write_text("", encoding="utf-8")
            (package / "tests").mkdir()
            (package / "tests" / "test_it.py").write_text("def test(): ...\n", encoding="utf-8")
            write_elf(package / f"_speedups.cpython-314-{cpu}-linux-{libc}.so", machine)
            policy = "musllinux_1_2" if libc == "musl" else rest
            _write_dist_info(target_dir, name, tag=f"cp314-cp314-{policy}_{cpu}")
            (target_dir / "bin").mkdir(exist_ok=True)
            (target_dir / "bin" / f"{name}-cli").write_text("#!/build/python\n", encoding="utf-8")
        (target_dir / ".lock").write_bytes(b"")

    def install_wheels(
        self, project: Path, target_dir: Path, wheels: Sequence[Path], *, platform: InstallPlatform
    ) -> None:
        self.calls.append(("install-wheels", platform))
        shutil.copytree(project / "src" / "demo_app", target_dir / "demo_app")
        _write_dist_info(
            target_dir, "demo_app", tag="py3-none-any", name="demo-app", version="1.0.0"
        )

    def find_python(self, version: str) -> Path:
        self.calls.append(("find-python", version))
        return Path(sys.executable)

    def called(self, operation: str) -> list[Any]:
        """Arguments of the calls to ``operation``, in order."""
        return [argument for name, argument in self.calls if name == operation]


def _write_dist_info(
    target_dir: Path, module: str, *, tag: str, name: str | None = None, version: str = "1.0"
) -> None:
    dist_info = target_dir / f"{module}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.4\nName: {name or module}\nVersion: {version}\n", encoding="utf-8"
    )
    (dist_info / "WHEEL").write_text(f"Wheel-Version: 1.0\nTag: {tag}\n", encoding="utf-8")
    for bookkeeping in ("RECORD", "INSTALLER"):
        (dist_info / bookkeeping).write_text("", encoding="utf-8")


@dataclass
class FakeRuntime:
    """Stands in for Docker: returns a canned process, or raises a timeout."""

    stdout: str = ""
    stderr: str = ""
    timeout: bool = False
    calls: list[list[str]] = field(default_factory=list)

    def run(self, args: Sequence[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd="docker run", timeout=timeout)
        return subprocess.CompletedProcess(["docker", "run", *args], 0, self.stdout, self.stderr)
