# faaspack

Build optimized Python deployment packages for **AWS Lambda** (function `.zip`, layer, or code
in the function and dependencies in a layer) and **Scaleway Serverless Functions**, from a uv
project. `uv.lock` stays the single source of truth: faaspack exports the lock, installs the
wheels of the target platform with `uv pip install --target`, then prunes, compiles and zips.

```bash
uvx faaspack bundle lambda --layout split --arch arm64 --arch x86_64
uvx faaspack bundle scaleway-functions
uvx faaspack verify dist/faaspack/my-api-1.0.0-lambda.manifest.json
uvx faaspack optimize /app/.venv/lib/python3.14/site-packages --python /app/.venv/bin/python
```

> Until the package is published on PyPI, replace `uvx faaspack` with
> `uvx --from git+https://github.com/HectorBst/faaspack faaspack`, or add faaspack to the `dev`
> group of your project and run `uv run faaspack`.

faaspack needs [uv](https://docs.astral.sh/uv/) 0.12.17 or newer on the build machine, and
Docker for `faaspack verify` only. faaspack itself has no runtime dependency.

## Why

| Problem | What faaspack does |
|---|---|
| Timestamp-based `.pyc` files are rejected once a zip tool rewrites modification times (to 1980 or 2098), and `/var/task` is read-only, so Lambda recompiles at every cold start (aws-lambda-builders#924, serverless#13889). `uv pip install --compile-bytecode` writes this kind of `.pyc`. | Compiles everything (dependencies and code) in `unchecked-hash` mode (PEP 552) with a Python of the target version. On the sample project, the Lambda emulator `INIT REPORT` drops from 501 ms to 289 ms (median of 5 runs, local machine, unthrottled CPU). |
| Wheels must match the runtime, not the build machine. | Lambda python3.12+: `manylinux_2_34` (Amazon Linux 2023, glibc 2.34), x86_64 or arm64. Scaleway: `musllinux` x86_64 (Alpine runtimes), where manylinux wheels do not import. |
| Useless files make archives heavier. | Conservative pruning (see below), optional `strip`, report of the largest contributors. |
| Archives change at every build. | Deterministic zips: sorted entries, fixed date, 644/755 permissions, relocated `co_filename`. Two builds of the same inputs on the same machine produce the same sha256. |
| Errors show up in production. | Checks the ELF architecture and libc of native modules, size quotas and unsatisfied imports, and imports everything in the official runtime image with `faaspack verify`. |

faaspack builds artifacts, it deploys nothing: the JSON manifest (paths, sizes, `sha256`, and
`sha256_base64` for the `source_code_hash` of OpenTofu or Terraform) is the input of your
deployment tool.

## Commands

### `faaspack bundle <target>`

Reads `pyproject.toml` and `uv.lock` in the current directory (or `--project`), writes the zips
and a manifest to `dist/faaspack/`.

| Option | Effect |
|---|---|
| `--layout function\|layer\|split` | Lambda only, see [Targets](#targets) |
| `--arch x86_64 --arch arm64` | one build per architecture (or `--arch x86_64,arm64`) |
| `--python 3.14` | minor version of the target runtime |
| `--strip debug\|unneeded` | strip native libraries (off by default) |
| `--no-compile` | no bytecode |
| `--verify` | run `faaspack verify` afterwards |
| `--strict` | exit with 1 on warnings too |
| `--output-dir`, `--uv`, `-v`, `-q` | output directory, uv binary, print uv commands, quiet mode |

Exit codes: 0 success, 1 blocking finding (an error, or a warning with `--strict`), 2 fatal
error (configuration, uv, unreadable manifest), 130 interrupted.

### `faaspack verify <manifest>`

Extracts the artifacts, mounts them read-only where the platform puts them, and imports every
top-level module of the dependencies with `python -v` in the runtime image:
`public.ecr.aws/lambda/python:<version>` for Lambda,
`rg.fr-par.scw.cloud/scwfunctionsruntimes-public/python-dep:<version>` for Scaleway (the
official build image, Alpine like the runtime, not the runtime itself). It reports failed
imports and modules compiled from source at import time (missing or rejected bytecode; the
goal is zero). `--import-code` also imports the project's own modules (which runs their
initialization code); `--import` and `--skip` adjust the list. arm64 on an x86_64 machine
needs QEMU emulation (`docker run --privileged --rm tonistiigi/binfmt --install arm64`).

### `faaspack optimize <directory>... --python <path, command or version>`

Prunes and compiles in place a tree already installed at its final location: the
`site-packages` of a Lambda container image, of Scaleway Serverless Containers or of Fargate.
`co_filename` keeps the real path and `bin/` is kept. Because `RECORD` files are removed, run
`optimize` on the final tree only (no `uv pip uninstall` afterwards), or pass
`--no-trim-dist-info`.

```dockerfile
RUN uv sync --frozen --no-dev --no-editable \
 && uvx faaspack optimize /app/.venv/lib/python3.14/site-packages --python /app/.venv/bin/python
```

### `faaspack targets`

Lists the installed targets.

## Configuration

In `pyproject.toml`, a common `[tool.faaspack]` table, then one table per target
(`[tool.faaspack.lambda]`, `[tool.faaspack.scaleway-functions]`). Command-line options win.

```toml
[tool.faaspack]
python = "3.14"

[tool.faaspack.lambda]
layout = "split"
architectures = ["arm64"]

[tool.faaspack.scaleway-functions]
ignore-imports = ["boto3"]
```

| Key | Default | Role |
|---|---|---|
| `python` | `3.14` | minor version of the runtime; bytecode is compiled by a Python of this version, downloaded by uv when needed |
| `layout` | `function` | Lambda: `function`, `layer`, `split` |
| `architectures` | `["x86_64"]` | Lambda: `x86_64`, `arm64` (the default follows AWS; arm64 is often faster and cheaper) |
| `package` | none | workspace member to package (`uv export --package`, `uv build --package`) |
| `code` | none | files or directories copied as is as the project code; without this key, faaspack builds the project wheel (`[build-system]` required) |
| `extras`, `groups` | `[]` | extras and dependency groups to include (no group by default, `dev` included) |
| `exclude-packages` | `[]` | packages removed from the export (`uv export --no-emit-package`) |
| `only-binary` | `true` | refuses sdists; `false` builds them on the host, which is only correct for pure Python |
| `compile` | `true` | `unchecked-hash` bytecode |
| `trim-dist-info` | `true` | removes `RECORD`, `INSTALLER`, `REQUESTED`, `direct_url.json`, `WHEEL`; keeps `METADATA`, `entry_points.txt`, `top_level.txt`, `licenses/` |
| `prune-tests` | `true` | removes `tests/` and `test/` directories; never `testing/` |
| `prune-scripts` | `true` | removes `bin/` (scripts whose shebang points to the build interpreter) |
| `prune-suffixes` | `.pyi .c .h .pyx .pxd .pxi` | removed file extensions |
| `keep` | `[]` | glob patterns (relative to the installed root) never removed, even inside a removed directory such as `tests/` or `bin/`, e.g. `"numpy/_core/include/*"` |
| `strip` | `false` | `debug` or `unneeded` |
| `strip-vendored-libs` | `false` | also strips the `*.libs/` directories repaired by auditwheel (known ELF corruption risk) |
| `strip-tool` | auto | `strip` on a Linux host of the same architecture, else `aarch64-linux-gnu-strip`/`x86_64-linux-gnu-strip` or `llvm-strip` (cross builds, macOS) |
| `import-check` | `warn` | unsatisfied imports of the code: `warn`, `error`, `ignore` |
| `ignore-imports` | `[]` | modules never reported |
| `output-dir` | `dist/faaspack` | directory of the zips and the manifest |
| `compression-level` | `9` | deflate level |
| `source-date-epoch` | `SOURCE_DATE_EPOCH` or 1980-01-01 | date written in the zip |
| `name` | project name | prefix of the produced files |

## Targets

### `lambda`

| Layout | Artifacts | Content |
|---|---|---|
| `function` | `<name>-<version>-lambda-function-<arch>.zip` | code and dependencies at the root |
| `layer` | `<name>-<version>-lambda-layer-<arch>.zip` | dependencies and code under `python/` |
| `split` | both | code only in the function, dependencies under `python/` in the layer |

The runtime puts `/var/task` before `/opt/python/lib/pythonX.Y/site-packages` and `/opt/python`,
**and adds the layer paths only when `PYTHONPATH` is not set on the function**
(`/var/runtime/bootstrap.py`). A layer with native code only works for the architecture it was
built for. Checked quotas: 50 MB zipped for a direct upload (warning: upload through Amazon
S3), 250 MB unzipped for the function and its layers together (error), and a hint from about
200 MB that a container image may start faster (a single public benchmark, measure it on your
case). python3.11 and older (Amazon Linux 2) use `manylinux2014`; this path is not tested.

### `scaleway-functions`

One artifact: code at the root, dependencies under `package/`, `musllinux` x86_64 wheels.
Checked limits: 100 MiB zip, 500 MiB code. Scaleway has no layers.

Not documented by Scaleway, therefore inferred: `package/` is added to `sys.path`
automatically (the official example imports its dependencies without changing it), the
architecture is amd64 (the `python-dep` image only exists for amd64), and the handling of
`.pth` files under `package/` and of `.pyc` files in the runtime standard library is unknown.

### Writing a target

A target is a subclass of `faaspack.targets.base.Target` registered in the `faaspack.targets`
entry point group of another package:

```toml
[project.entry-points."faaspack.targets"]
my-target = "my_package.target:MyTarget"
```

The subclass declares the platform as class attributes (`name`, `description`,
`architectures`, `default_architectures`, `layouts`, `default_layout`, `default_python`, `libc`
and `runtime_provided`) and implements `python_platform()` (the uv `--python-platform` value),
`plan()` (the archives of a layout), `check_sizes()` (quota findings), `verify_spec()` (the
container used by `faaspack verify`) and optionally `notes()`. The two built-in targets,
`faaspack/targets/aws_lambda.py` and `faaspack/targets/scaleway.py`, are the reference
implementations.

## boto3 and modules provided by the runtime

What gets bundled is what the lock contains: faaspack never drops boto3 on its own. The Lambda
runtime provides boto3, botocore, s3transfer, jmespath, dateutil, urllib3, six and simplejson,
in versions AWS changes without notice, and AWS recommends bundling the SDK. When the code
imports one of these modules without declaring it, faaspack reports **FP402**. To rely on the
runtime version on purpose, use `ignore-imports = ["boto3"]`; to remove a boto3 pulled in by
another dependency, use `exclude-packages = ["boto3", "botocore", "s3transfer"]`.
`snapshot_restore_py` (SnapStart hooks) and `awslambdaric` are provided by the runtime and are
never reported.

The import analysis is static and covers the project code. It ignores imports guarded by
`try/except ImportError`, `contextlib.suppress(ImportError)` or `if TYPE_CHECKING:`, imports in
either branch of a test on `sys.version_info`, `sys.platform` or `os.name` (such as `tomli`
before Python 3.11), and relative imports.

## Findings

| Code | Level | Meaning |
|---|---|---|
| FP101 | error | native module built for another architecture, or for macOS or Windows (Mach-O, PE) |
| FP102 | error | extension module built for another libc (glibc/musl) |
| FP103 | warning | `strip` failed on a file (left untouched) |
| FP104 | info | `.py` files that do not compile (templates, Python 2), kept as sources |
| FP105 | error or warning | code and dependencies provide the same module: error in a single archive, warning in `split` (the function code shadows the layer) |
| FP201 | warning | Lambda: zip above 50 MB, upload through S3 required |
| FP202 | error | Lambda: more than 250 MB unzipped |
| FP203 | info | Lambda: more than 200 MB, consider a container image |
| FP204 | info | Lambda: reminder of the quota shared by the function and its layers |
| FP211, FP212 | error | Scaleway: zip above 100 MiB, code above 500 MiB |
| FP301 | info | Lambda: reminder about `PYTHONPATH` and layer architecture |
| FP401 | per `import-check` | import provided neither by the bundle nor by the standard library |
| FP402 | warning | import provided by the Lambda runtime but not pinned |
| FP500 | info | `verify`: import summary |
| FP501 | error | `verify`: failed import |
| FP502 | warning | `verify`: modules compiled from source at startup |
| FP503 | warning | `verify`: image could not run (missing emulation, timeout) |

Codes are stable, so CI can grep or ignore them.

## Defaults and why

- **dist-info directories are kept.** Removing them breaks `import mcp` (MCP SDK 2.x,
  `PackageNotFoundError`), disables OpenTelemetry entry points and the availability checks of
  `transformers`, for about 4 % of the size.
- **`strip` is off.** No gain on wheels that are already stripped (pydantic-core), about 3 MiB
  on cryptography with `unneeded`, and a corruption risk on libraries repaired by patchelf.
- **No `-OO`, no `.pyc` without `.py`.** `-OO` needs `.opt-2.pyc` files for the whole standard
  library and slows startup down when they are missing; sourceless `.pyc` files bring no
  measurable gain and remove the source lines from tracebacks.
- **`--no-deps` at install time.** The installed set is exactly the export of the lock, with
  markers evaluated for the target platform.

The reasoning behind these choices is in [`docs/design.md`](docs/design.md).

## Known limitations

- A dependency published only as an sdist with native code fails (`only-binary = true`);
  building it in a container of the target is not automated yet.
- Private indexes: faaspack runs uv from the project root, so `[tool.uv]` settings and the
  `UV_INDEX_<NAME>_USERNAME`/`PASSWORD` variables apply; tested with an index configuration,
  not with a real authenticated index.
- uv workspaces: the `package` option exists but is not tested on a workspace with
  dependencies between members.
- `verify` is no substitute for a test on the platform: no CPU quota, no lazy code loading,
  and the build image stands in for the Scaleway runtime.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the development setup, the quality gates and the
pull request rules. Security issues: see [`SECURITY.md`](SECURITY.md).

## License

MIT, see [`LICENSE`](LICENSE).
