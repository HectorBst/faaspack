# Design decisions

This document records why faaspack is built the way it is, so that contributors can change it
without replaying the original investigation. Facts are dated: serverless platforms and uv
change quickly, and anything below can become outdated.

## Constraints of the platforms (checked 2026-09-21)

AWS Lambda and Scaleway Functions accept an ordinary zip: there is no proprietary format. The
constraints are the file layout and binary compatibility.

- **AWS Lambda, python3.12 and newer** run on Amazon Linux 2023 with glibc 2.34 (`ldd --version`
  in `public.ecr.aws/lambda/python:3.14`), on x86_64 and arm64. The handler and its
  dependencies sit at the root of the function zip; layer content goes under `python/`. Quotas:
  50 MB zipped for a direct upload, 250 MB unzipped for the function and its layers together, 5
  layers per function ([quotas](https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html)).
- **Layer loading**: `/var/runtime/bootstrap.py` puts `/var/task`, then
  `/opt/python/lib/python3.X/site-packages`, then `/opt/python` at the front of `sys.path`, runs
  `site.addsitedir()` on them (so `.pth` files work), and adds the layer paths only when
  `PYTHONPATH` is not set on the function.
- **Scaleway Functions** run on Alpine, so musl
  ([packaging guide](https://www.scaleway.com/en/docs/serverless-functions/how-to/package-function-dependencies-in-zip/)).
  Dependencies go in a `package/` directory at the root of the zip. Limits: 100 MiB zip, 500 MiB
  code ([limitations](https://www.scaleway.com/en/docs/serverless-functions/reference-content/functions-limitations/)).
  There are no layers. A manylinux wheel does not import there: `pydantic_core` fails with
  `ModuleNotFoundError` in the `python-dep:3.14` build image.
- **Bytecode and uv**: `uv pip install --compile-bytecode` writes timestamp-based `.pyc` files
  (113 out of 113 in our test); with `SOURCE_DATE_EPOCH`, uv switches to `checked-hash`. Neither
  produces `unchecked-hash`. The [uv guide for Lambda](https://docs.astral.sh/uv/guides/integration/aws-lambda/)
  disables compilation.

## A command-line tool, not a hatchling plugin

The question was whether hatchling plugins could produce Lambda archives from the uv tooling.
The PEP 517 hooks of hatchling 1.32.4 only call `SdistBuilder` or `WheelBuilder` and ignore
`config_settings` ([build.py](https://github.com/pypa/hatch/blob/master/backend/src/hatchling/build.py)),
so a custom hatchling target is only reachable through `hatch build -t <target>`, never through
`uv build`, `pip` or `python -m build`.

A workaround works: a build hook disabled by default and enabled with
`HATCH_BUILD_HOOK_ENABLE_<NAME>=1` runs under `uv build`. It was rejected because:

- it would force hatchling on the project, while `uv init --package` generates `uv_build`,
  which has no plugin system;
- `uv build` builds the wheel from the sdist in a temporary directory, which only works if
  `uv.lock` is part of the sdist;
- the archive would be a side effect the frontend knows nothing about, and every call would
  also produce a useless wheel;
- a build backend builds the project's own distribution; assembling it with third-party
  dependencies for a target platform is an installer's job (`uv pip install --target
  --python-platform`).

faaspack is therefore a standalone package run with `uvx faaspack`. It reads
`[tool.faaspack]`, exports `uv.lock` with `uv export --frozen`, installs for the target with
`uv pip install --target`, and builds the project wheel with `uv build --wheel`, so it works
with any PEP 517 backend. A project without `[build-system]` lists its code with the `code`
key. A thin hatchling adapter on top of faaspack (for `hatch build -t lambda` only) remains
possible.

## No existing tool covered the need (2026-09-21)

No tool combined `uv.lock`, the three Lambda layouts, arm64, valid bytecode and Scaleway:

- AWS SAM's `python_uv` builder was a preview, silent on `uv.lock` and bytecode; the rejected
  `.pyc` issue ([aws-lambda-builders#924](https://github.com/aws/aws-lambda-builders/issues/924))
  was open.
- AWS CDK's `PythonFunction` handles `uv.lock`, `PythonLayerVersion` did not
  ([aws-cdk#35153](https://github.com/aws/aws-cdk/issues/35153)).
- Pants has function and layer targets and an architecture field, but does not read `uv.lock`.
- Serverless Framework v4's `slim` option deletes `.pyc` files and `*.dist-info`, the opposite
  of what helps cold starts ([serverless#13889](https://github.com/serverless/serverless/issues/13889)).
- uv's own bundle ([astral-sh/uv#9350](https://github.com/astral-sh/uv/issues/9350)) has been
  open since 2024-11-22 with the `needs-design` label.

If uv ships a native bundle, or if SAM and the Serverless Framework fix their bytecode,
faaspack's own value shrinks to its optimizations and to Scaleway. faaspack therefore stays a
thin layer over uv.

## One package, targets as plugins

Lambda and Scaleway share about 80 % of the code (export, installation, pruning, compilation,
strip, zip, verification). Two packages would duplicate it or need a third, shared package;
three packages are too many versions to keep compatible for a small project. faaspack is one
package whose targets are registered in the `faaspack.targets` entry point group; a target can
move to its own package later if it evolves at a different pace.

## Code structure: ports for external systems only

faaspack drives two external systems that are slow, need the network and are absent from many
machines: uv and a container runtime. Each sits behind a small protocol (`UvCommands`,
`ContainerRuntime`) with one real implementation (`Uv`, `Docker`), so the whole bundle and
verify logic runs in unit tests with fakes, and the real implementations are exercised by
integration tests.

A full hexagonal layout (domain, application and adapter packages) was considered and not
adopted: the domain is small (targets, layouts, findings, pruning rules), most steps are thin
wrappers over the file system and the standard library, and an extra layer of indirection would
make the roughly 1,500 lines harder to navigate without making them easier to test. The file
system is used directly, in temporary directories, because it is fast and real.

## Bytecode: `unchecked-hash`

Timestamp-based `.pyc` files are rejected once the zip rewrites modification times, and
`/var/task` is read-only, so the interpreter recompiles at every cold start. faaspack compiles
dependencies and code with `compileall --invalidation-mode unchecked-hash` (PEP 552) using a
Python of the target minor version (bytecode does not depend on the CPU architecture), and
rewrites `co_filename` to the runtime path (`/var/task/`, `/opt/python/`, or a relative path
when the platform does not document it), which also makes the `.pyc` bytes independent of the
build directory. On the sample project, the Lambda emulator initialization went from 501 ms to
289 ms (median of 5 runs after 1 warm-up, local machine, unthrottled CPU).

Rejected: `-OO` (it needs `.opt-2.pyc` files for the whole standard library), `.pyc` without
`.py` (no measured gain, tracebacks without lines). `-X no_debug_ranges` is unmeasured.

## Conservative pruning

- **dist-info directories are kept**; only installer bookkeeping (`RECORD`, `INSTALLER`,
  `REQUESTED`, `direct_url.json`, `WHEEL`) is removed. Deleting the whole directories breaks
  `import mcp` (MCP SDK 2.x raises `PackageNotFoundError`) to save 1.7 MB out of 40 MB, and
  disables OpenTelemetry auto-instrumentation and the availability checks of `transformers`.
- **Tests**: `tests/` and `test/` directories are removed, `testing/` never is, because
  `numpy.testing` and `pandas.testing` are public APIs. The gain is small on an API stack (about
  0.7 MB out of 41 MB) and large on data stacks (38 MB of tests in pandas,
  [pandas#60606](https://github.com/pandas-dev/pandas/issues/60606)).
- **Scripts** (`bin/`) are removed: their shebang points to the build machine interpreter.
- **Strip** is off by default. Libraries vendored by auditwheel under `*.libs/` are skipped
  even when strip is on, because stripping files rewritten by patchelf is a known source of
  corrupted ELF files ([scipy#12975](https://github.com/scipy/scipy/issues/12975),
  [patchelf#507](https://github.com/NixOS/patchelf/issues/507)).

Measured on an API stack (fastapi, mcp, opentelemetry-sdk, jsonschema, aws-lambda-powertools,
httpx, pydantic-settings, sse-starlette, uvicorn) for `x86_64-manylinux_2_34`:

| Measure | Value |
|---|---|
| Installed size | 40.9 MB |
| `*.dist-info` directories | 1.7 MB (39 distributions) |
| After the default pruning | 39.8 MB (about −2.5 %) |
| `.so` files | 20.4 MB |
| `.so` after `strip --strip-debug` | 20.2 MB (about −1 %) |
| `.so` after `strip --strip-unneeded` | 17.3 MB (about −15 %), imports still work |

## boto3: the lock decides

The Lambda runtime provides boto3 and its dependencies, in versions AWS changes without
notice, and AWS recommends bundling the SDK. faaspack bundles what the lock contains and never
drops boto3 on its own. FP402 fires when the project code imports a runtime-provided module
without declaring it, not when boto3 is merely absent, because many functions do not use the
SDK. `ignore-imports` relies on the runtime copy on purpose; `exclude-packages` removes a boto3
pulled in transitively. On Scaleway, which does not provide boto3, the same import raises
FP401.

## Architecture default

The Lambda default is x86_64, like the `Architectures` parameter of the
[CreateFunction API](https://docs.aws.amazon.com/lambda/latest/api/API_CreateFunction.html);
the README recommends arm64.

## Assumptions that still need a real deployment

- Scaleway adds `package/` to `sys.path`: inferred from the official example
  ([scaleway/serverless-examples](https://github.com/scaleway/serverless-examples),
  `functions/python-dependencies`), not documented.
- Scaleway's CPU architecture is not documented; x86_64 is inferred from the `python-dep`
  image, which only exists for amd64.
- The handling of `.pth` files under `package/` and the presence of `.pyc` files in the
  Scaleway runtime standard library are unknown.
- AWS does not document the glibc version of its runtimes; 2.34 comes from the image.
- The cold start effect of putting dependencies in a layer rather than in the function has not
  been measured.
- SnapStart makes the bytecode question mostly moot, since imports run before the snapshot.

## Verification of 0.1.0 (2026-09-21 and 2026-09-22)

On `tests/fixtures/sample_app` (pydantic, httpx, and a deliberate undeclared boto3 import):

- the three Lambda layouts in x86_64 and arm64: `faaspack verify` imports 11 modules out of 11
  in `public.ecr.aws/lambda/python:3.14` (arm64 under QEMU), with 0 module compiled from source;
- Scaleway: musl wheels, 11 modules out of 11 imported in `python-dep:3.14`;
- `split` archives invoked in the Lambda runtime interface emulator with the real
  `bootstrap.py`: correct response, layer loaded from `/opt/python`;
- reproducibility: two successive builds give the same sha256;
- cross strip for arm64 on cryptography: 3.1 MiB saved (28.3 to 25.2 MiB unzipped), imports
  still work.

No function has been deployed on a real AWS or Scaleway account yet.
