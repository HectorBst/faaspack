"""The uv adapter, against a fake ``uv`` executable that records its command lines."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from faaspack.errors import FaaspackError
from faaspack.uvcli import InstallPlatform, LockSelection, Uv, locate_uv
from tests.fakes import PYTHON

# Records argv in $FAKE_UV_LOG, then behaves like the few uv commands faaspack runs.
FAKE_UV = f"""\
#!{sys.executable}
import json, os, pathlib, sys

args = sys.argv[1:]
with open(os.environ["FAKE_UV_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if os.environ.get("FAKE_UV_FAIL") == " ".join(args[:2]):
    print("\\n".join(f"line {{i}}" for i in range(30)), file=sys.stderr)
    sys.exit(2)
if args[:2] == ["build", "--wheel"]:
    out = pathlib.Path(args[args.index("--out-dir") + 1])
    out.mkdir(parents=True, exist_ok=True)
    for i in range(int(os.environ.get("FAKE_UV_WHEELS", "1"))):
        (out / f"app_{{i}}-1.0-py3-none-any.whl").write_bytes(b"")
if args[:2] == ["python", "install"]:
    pathlib.Path(os.environ["FAKE_UV_LOG"] + ".installed").touch()
if args[:2] == ["python", "find"]:
    if os.environ.get("FAKE_UV_MISSING_PYTHON") and not os.path.exists(
        os.environ["FAKE_UV_LOG"] + ".installed"
    ):
        sys.exit(1)
    print(sys.executable)
"""


@pytest.fixture
def fake_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = tmp_path / "uv"
    binary.write_text(textwrap.dedent(FAKE_UV), encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_UV_LOG", str(tmp_path / "calls.jsonl"))
    return binary


def recorded(fake_uv: Path) -> list[list[str]]:
    log = fake_uv.parent / "calls.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_export_selects_the_lock_subset(fake_uv: Path, tmp_path: Path) -> None:
    selection = LockSelection(package="api", extras=("s3",), groups=("prod",), exclude=("boto3",))
    Uv(str(fake_uv)).export_requirements(tmp_path, tmp_path / "req.txt", selection)
    (args,) = recorded(fake_uv)
    assert args[:2] == ["export", "--frozen"]
    assert {"--no-default-groups", "--no-editable", "--no-emit-project"} <= set(args)
    tail = args[args.index(str(tmp_path / "req.txt")) + 1 :]
    assert tail == [
        "--package",
        "api",
        "--extra",
        "s3",
        "--group",
        "prod",
        "--no-emit-package",
        "boto3",
    ]


def test_install_commands_target_the_platform(fake_uv: Path, tmp_path: Path) -> None:
    uv = Uv(str(fake_uv))
    platform = InstallPlatform("3.14", "aarch64-manylinux_2_34")
    uv.install_requirements(
        tmp_path, tmp_path / "deps", tmp_path / "r.txt", platform=platform, only_binary=True
    )
    uv.install_requirements(
        tmp_path, tmp_path / "deps", tmp_path / "r.txt", platform=platform, only_binary=False
    )
    uv.install_wheels(tmp_path, tmp_path / "code", [tmp_path / "app.whl"], platform=platform)
    binary, source, wheel = recorded(fake_uv)
    for args in (binary, source, wheel):
        assert args[:4] == ["pip", "install", "--target", args[3]]
        assert args[args.index("--python-platform") + 1] == "aarch64-manylinux_2_34"
        assert {"--no-deps", "--no-compile-bytecode", "--no-installer-metadata"} <= set(args)
    assert binary[-4:] == ["--only-binary", ":all:", "--requirement", str(tmp_path / "r.txt")]
    assert "--only-binary" not in source
    assert wheel[-1] == str(tmp_path / "app.whl")


def test_build_wheel_returns_the_single_wheel(
    fake_uv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uv = Uv(str(fake_uv))
    wheel = uv.build_wheel(tmp_path, tmp_path / "w1", package="api")
    assert wheel.name == "app_0-1.0-py3-none-any.whl"
    assert recorded(fake_uv)[0][-2:] == ["--package", "api"]
    monkeypatch.setenv("FAKE_UV_WHEELS", "2")
    with pytest.raises(FaaspackError, match="expected one wheel"):
        uv.build_wheel(tmp_path, tmp_path / "w2", package=None)


def test_failures_show_the_end_of_the_output(
    fake_uv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_UV_FAIL", "build --wheel")
    with pytest.raises(FaaspackError, match=r"exit 2") as error:
        Uv(str(fake_uv)).build_wheel(tmp_path, tmp_path / "w", package=None)
    message = str(error.value)
    assert "line 29" in message
    assert "line 14" not in message  # only the last 15 lines


def test_find_python_installs_a_missing_version(
    fake_uv: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_UV_MISSING_PYTHON", "1")
    assert Uv(str(fake_uv)).find_python(PYTHON) == Path(sys.executable)
    assert [args[:2] for args in recorded(fake_uv)] == [
        ["python", "find"],
        ["python", "install"],
        ["python", "find"],
    ]


def test_find_python_checks_the_version(fake_uv: Path) -> None:
    with pytest.raises(FaaspackError, match=r"for Python 3\.99"):
        Uv(str(fake_uv)).find_python("3.99")


def test_commands_are_echoed_when_verbose(fake_uv: Path, tmp_path: Path) -> None:
    lines: list[str] = []
    Uv(str(fake_uv), echo=lines.append).run(["pip", "list", "--format", "two words"])
    assert lines == ["+ uv pip list --format 'two words'"]


def test_locate_uv(tmp_path: Path, fake_uv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert locate_uv(str(fake_uv)) == str(fake_uv)
    monkeypatch.setenv("UV", str(fake_uv))
    assert locate_uv() == str(fake_uv)
    monkeypatch.delenv("UV")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert locate_uv("missing") == str(fake_uv)
    monkeypatch.setenv("PATH", os.devnull)
    with pytest.raises(FaaspackError, match="uv not found"):
        locate_uv()
