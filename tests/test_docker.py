"""Smoke tests in the official runtime images (needs Docker; arm64 needs QEMU binfmt)."""

from __future__ import annotations

import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

from faaspack.pipeline import bundle
from faaspack.verify import verify

pytestmark = [pytest.mark.integration, pytest.mark.docker]


def test_verify_lambda_and_scaleway(sample_app: Path) -> None:
    for target in ("lambda", "scaleway-functions"):
        result = bundle(
            sample_app, target, {"output_dir": f"out-{target}", "architectures": ("x86_64",)}
        )
        (report,) = verify(result.manifest)
        assert report.failures == {}
        assert report.imported > 5
        assert report.from_source == []


def test_split_artifacts_run_in_the_lambda_emulator(sample_app: Path, tmp_path: Path) -> None:
    result = bundle(
        sample_app, "lambda", {"output_dir": "out", "layout": "split", "architectures": ("x86_64",)}
    )
    function, layer = result.builds[0].artifacts
    task, opt = tmp_path / "task", tmp_path / "opt"
    zipfile.ZipFile(function.archive.path).extractall(task)
    zipfile.ZipFile(layer.archive.path).extractall(opt)
    name = f"faaspack-test-{time.time_ns()}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--platform",
            "linux/amd64",
            "-p",
            "127.0.0.1:19123:8080",
            "-v",
            f"{task}:/var/task:ro",
            "-v",
            f"{opt}:/opt:ro",
            "public.ecr.aws/lambda/python:3.14",
            "sample_app.handler.handler",
        ],
        check=True,
        capture_output=True,
    )
    try:
        body = b""
        for _ in range(40):
            try:
                request = urllib.request.Request(
                    "http://127.0.0.1:19123/2015-03-31/functions/function/invocations",
                    data=b'{"name": "faaspack"}',
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read()
                break
            except OSError:
                time.sleep(0.5)
        assert b"faaspack" in body
        assert b'\\"statusCode\\"' in body or b'"statusCode": 200' in body
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
