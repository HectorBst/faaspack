# Contributing to faaspack

## Set up

You need [uv](https://docs.astral.sh/uv/) 0.12.17 or newer. Docker is optional: it is only
used by the tests marked `docker`.

```bash
uv sync
uvx pre-commit install
```

The pre-commit hooks run the same checks as CI on the files you commit: ruff (lint and
format), mypy in strict mode, cognitive complexity, gitleaks, zizmor (workflow audit) and a
`uv.lock` freshness check. Python tools run through `uv run --frozen`, so their versions come
from `uv.lock`.

## Quality gates

Every pull request must pass these gates, enforced by CI:

| Gate | Threshold | Local command |
|---|---|---|
| Lint and format | no finding | `uv run ruff check . && uv run ruff format --check .` |
| Types | mypy strict, no error | `uv run mypy` |
| Cognitive complexity | 15 per function at most | `uv run complexipy` |
| Global coverage (branches included) | 85 % of the whole suite | `uv run pytest --cov` |
| New code coverage | 75 % of the lines the pull request adds | `uv run diff-cover coverage.xml --compare-branch=origin/main` |
| Secrets | no finding | `uvx pre-commit run gitleaks --all-files` |
| Vulnerabilities | no known vulnerability | `uv audit --preview-features audit-command` |

Global coverage is measured on the combined unit, integration and Docker tests; the offline
unit tests alone also stay above the threshold (`uv run pytest -m "not integration" --cov`).

## Code map

| Module | Responsibility |
|---|---|
| `cli.py` | argument parsing, terminal output, exit codes |
| `config.py` | `[tool.faaspack]` parsing and validation into `Options` |
| `pipeline.py` | `faaspack bundle`: per architecture, install, prune, check, compile, zip |
| `optimize.py` | `faaspack optimize`: prune and compile a tree in place |
| `verify.py` | `faaspack verify`: import the artifacts in the runtime image |
| `uvcli.py` | what faaspack needs from uv (`UvCommands`) and its implementation (`Uv`) |
| `prune.py`, `bytecode.py`, `binaries.py`, `archive.py`, `imports.py` | one concern each: pruning rules, `unchecked-hash` compilation, ELF checks and strip, deterministic zips, static import analysis |
| `results.py`, `manifest.py` | build results and their JSON manifest |
| `targets/` | platform plugins (`Target` subclasses) |

uv and the container runtime are the two external systems. They sit behind small protocols
(`UvCommands`, `ContainerRuntime`), so the pipeline and `verify` are unit-tested offline with
the fakes of `tests/fakes.py`; the real implementations are covered by the integration and
Docker tests.

## Tests

```bash
uv run pytest -m "not integration"   # unit tests, offline, a few seconds
uv run pytest                        # + integration (uv, PyPI) and Docker when available
```

Tests marked `integration` build the sample project in `tests/fixtures/sample_app` with the real
uv; tests marked `docker` also run `faaspack verify` and invoke the function in the Lambda
runtime interface emulator. They are skipped when uv or Docker is missing
(`FAASPACK_SKIP_INTEGRATION=1` forces the skip).

Every bug fix comes with a test that fails without the fix.

## Pull requests

- One pull request per coherent change, with a [Conventional Commits](https://www.conventionalcommits.org/)
  title (`fix(prune): ...`, `feat: ...`, `chore(ci): ...`): it becomes the commit message.
- Pull requests are merged by squash or rebase only, so `main` keeps a linear history.
- A stacked pull request (based on another pull request's branch) must be rebased once its
  parent is merged, because squashing rewrites the parent's commits:

  ```bash
  git fetch origin
  git rebase --onto origin/main <parent-branch> <your-branch>
  git push --force-with-lease
  ```

## Code style

- Code, docstrings, comments and documentation are written in English.
- Names and structure should make the code self-explanatory. Comments explain *why*, when the
  reason is not obvious (a platform quirk, a measured trade-off), never *what*.
- Public functions and classes have a docstring; findings keep their stable `FPxxx` code,
  documented in the README.
