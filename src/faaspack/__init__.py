"""faaspack: optimized Python packages for AWS Lambda and Scaleway Serverless Functions."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("faaspack")  # pyproject.toml is the single source of the version
except PackageNotFoundError:  # a source tree that is not installed
    __version__ = "0+unknown"
