from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(["docker", "info"], capture_output=True, check=False)
    return proc.returncode == 0


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    skip_integration = os.environ.get("FAASPACK_SKIP_INTEGRATION") == "1" or not (
        shutil.which("uv") or os.environ.get("UV")
    )
    docker = _docker_available()
    for item in items:
        if "integration" in item.keywords and skip_integration:
            item.add_marker(pytest.mark.skip(reason="uv or network unavailable"))
        if "docker" in item.keywords and not docker:
            item.add_marker(pytest.mark.skip(reason="Docker daemon unavailable"))


@pytest.fixture
def sample_app(tmp_path: Path) -> Path:
    """A copy of the sample uv project (with its uv.lock) in a temporary directory."""
    destination = tmp_path / "sample_app"
    shutil.copytree(
        FIXTURES / "sample_app",
        destination,
        ignore=shutil.ignore_patterns(".venv", "dist", "__pycache__"),
    )
    return destination
