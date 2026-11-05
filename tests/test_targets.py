from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from faaspack.errors import FaaspackError, Level
from faaspack.targets import available_targets, get_target
from faaspack.targets.aws_lambda import UNZIPPED_LIMIT, LambdaTarget
from faaspack.targets.base import BuiltArtifact
from faaspack.targets.scaleway import ScalewayFunctionsTarget


def test_registry_exposes_both_targets() -> None:
    assert set(available_targets()) >= {"lambda", "scaleway-functions"}
    with pytest.raises(FaaspackError, match="unknown target"):
        get_target("gcp")


def test_a_broken_plugin_is_reported_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    broken = EntryPoint(name="broken", value="no_such_module:Target", group="faaspack.targets")
    monkeypatch.setattr("faaspack.targets.entry_points", lambda group: [broken])
    with pytest.raises(
        FaaspackError, match=r"target 'broken' \(no_such_module:Target\) cannot be loaded"
    ):
        available_targets()


def test_a_plugin_must_be_a_target(monkeypatch: pytest.MonkeyPatch) -> None:
    wrong = EntryPoint(name="wrong", value="pathlib:Path", group="faaspack.targets")
    monkeypatch.setattr("faaspack.targets.entry_points", lambda group: [wrong])
    with pytest.raises(FaaspackError, match="is not a faaspack Target"):
        available_targets()


@pytest.mark.parametrize(
    ("python", "arch", "expected"),
    [
        ("3.14", "x86_64", "x86_64-manylinux_2_34"),
        ("3.14", "arm64", "aarch64-manylinux_2_34"),
        ("3.12", "arm64", "aarch64-manylinux_2_34"),
        ("3.11", "x86_64", "x86_64-manylinux2014"),
    ],
)
def test_lambda_platforms(python: str, arch: str, expected: str) -> None:
    assert LambdaTarget().python_platform(python=python, architecture=arch) == expected


def test_scaleway_is_musl_x86_only() -> None:
    target = ScalewayFunctionsTarget()
    assert target.python_platform(python="3.14", architecture="x86_64") == (
        "x86_64-unknown-linux-musl"
    )
    with pytest.raises(FaaspackError, match="unsupported architecture"):
        target.validate(python="3.14", layout="function", architectures=["arm64"])
    with pytest.raises(FaaspackError, match="unknown layout"):
        target.validate(python="3.14", layout="split", architectures=["x86_64"])


@pytest.mark.parametrize("layout", ["function", "layer", "split"])
def test_lambda_plans_place_each_stage_once(layout: str) -> None:
    plan = LambdaTarget().plan(layout)
    stages = [stage for spec in plan for stage, _ in spec.parts]
    assert sorted(stages) == ["code", "deps"]
    for spec in plan:
        prefix = {"function": "", "layer": "python/"}[spec.kind]
        assert all(p == prefix for _, p in spec.parts)


def test_python_version_format() -> None:
    with pytest.raises(FaaspackError, match="minor version"):
        LambdaTarget().validate(python="3.14.1", layout="function", architectures=["x86_64"])


def test_lambda_size_findings() -> None:
    target = LambdaTarget()
    small = target.check_sizes([BuiltArtifact("function", 10_000, 30_000)])
    assert small == []
    big_zip = target.check_sizes([BuiltArtifact("function", 60 * 2**20, 100 * 2**20)])
    assert [f.code for f in big_zip] == ["FP201"]
    hint = target.check_sizes([BuiltArtifact("function", 40 * 2**20, 220 * 2**20)])
    assert [(f.code, f.level) for f in hint] == [("FP203", Level.INFO)]
    split = target.check_sizes(
        [
            BuiltArtifact("function", 1, UNZIPPED_LIMIT // 2),
            BuiltArtifact("layer", 1, UNZIPPED_LIMIT // 2 + 1),
        ]
    )
    assert [f.code for f in split] == ["FP202", "FP204"]


def test_lambda_quota_message_uses_the_unit_of_the_limit() -> None:
    (finding,) = LambdaTarget().check_sizes([BuiltArtifact("function", 1, UNZIPPED_LIMIT + 1)])
    assert finding.message.startswith("unzipped size 250.0 MiB exceeds the 250 MiB Lambda quota")


def test_scaleway_size_findings() -> None:
    findings = ScalewayFunctionsTarget().check_sizes(
        [BuiltArtifact("function", 101 * 2**20, 501 * 2**20)]
    )
    assert [f.code for f in findings] == ["FP211", "FP212"]


def test_layer_notes_and_verify_specs() -> None:
    target = LambdaTarget()
    assert target.notes("function") == []
    assert [f.code for f in target.notes("split")] == ["FP301"]
    spec = target.verify_spec(python="3.14", architecture="arm64")
    assert spec.docker_platform == "linux/arm64"
    assert spec.sys_path[1] == "/opt/python/lib/python3.14/site-packages"
    assert target.runtime_provided["boto3"] is not None
    assert target.runtime_provided["snapshot_restore_py"] is None
