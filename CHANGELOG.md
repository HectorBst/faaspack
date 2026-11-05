# Changelog

All notable changes to faaspack are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/) (before 1.0, a minor version may break
compatibility).

## [Unreleased]

## [0.2.0] - 2026-11-05

First release on PyPI.

### Added

- FP101 also reports native modules built for macOS (Mach-O) or Windows (PE).
- `faaspack optimize --python` accepts a command found on `PATH`, such as `python3`.
- `faaspack verify --arch` fails when the manifest has no build for that architecture.
- Ctrl+C exits with code 130 instead of a traceback.
- Release workflow publishing to PyPI with trusted publishing.

### Changed

- The documentation is in English; design decisions are recorded in `docs/design.md`.
- The import analysis ignores imports under `contextlib.suppress(ImportError)` and in either
  branch of a test on `sys.version_info`, `sys.platform` or `os.name`.
- Scaleway bytecode records source paths relative to the archive root
  (`package/<module>/...` for dependencies).
- FP104 lists files relative to the archive root, and FP202 reports sizes in MiB.
- `faaspack.__version__` is read from the installed metadata.
- The comparison with other tools (README, `docs/design.md`) is updated as of 2026-11-05: the
  Serverless Framework now ships `checked-hash` bytecode for pip-installed dependencies.

### Fixed

- `faaspack verify` on a missing or invalid manifest, duplicate `code` paths, an interpreter
  that cannot run and a broken target plugin print an error and exit with 2 instead of a
  traceback.
- `keep` patterns protect files inside removed directories (`tests/`, `bin/`).
- On macOS, `strip` no longer picks the system `strip`, which cannot read ELF files.
- A build directory whose name ends in `.libs` no longer disables `strip`.

### Removed

- The undocumented `keep_work_dir` parameter of `faaspack.pipeline.bundle()`.

## 0.1.0 - 2026-09-22

First version, not published on PyPI: `bundle`, `verify`, `optimize` and `targets` commands,
`lambda` (function, layer and split layouts, x86_64 and arm64) and `scaleway-functions`
targets.

[Unreleased]: https://github.com/HectorBst/faaspack/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/HectorBst/faaspack/releases/tag/v0.2.0
