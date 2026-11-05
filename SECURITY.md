# Security policy

## Supported versions

Security fixes go to the latest released minor version only.

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/HectorBst/faaspack/security/advisories/new),
not in a public issue. Include the faaspack and uv versions, the command you ran and, if
possible, a minimal project that reproduces the problem.

faaspack is maintained on a best-effort basis: expect an acknowledgement within a week.

## Scope

faaspack runs on a build machine, with the privileges of the user who calls it. By design it:

- runs uv, which downloads and installs the packages listed in `uv.lock` and runs the
  project's build backend (`uv build --wheel`);
- runs the target Python interpreter on the installed files (`compileall`);
- with `faaspack verify`, runs the official runtime images in Docker, without network access,
  and imports the bundled dependencies (and the project code with `--import-code`).

Treat a `pyproject.toml`, a `uv.lock` or a manifest from an untrusted source like code you are
about to run. A vulnerability is a way for faaspack to do more than that, for example writing
outside its output and temporary directories or executing something the project did not ask
for.
