# Contributing

Use Python 3.11 and the existing pyproject dependencies. Installation resolves the declared
version ranges in pyproject.toml. Do not add dependencies without a concrete need.

```sh
uv venv --python 3.11 --seed
. .venv/bin/activate
uv pip install --require-hashes -r requirements-public-checks.lock
uv pip install --no-deps -e .
python -m pip check
ruff check .
python -m compileall -q pokegent addons tools tests
python tests/games/gen3/test_imports.py
python -m pytest -q
python tools/check_rom_data.py
```

All these checks run without a ROM, native emulator, credentials or game services.
The packaging tests may download build dependencies through pip build isolation, so use a
package index or a warm cache. Use synthetic memory and data in tests. Keep ROMs, saves,
extracted tables, screenshots of game content, credentials and machine-specific files
outside the repository.

`uv venv --seed` installs `pip`, which the wheel-building tests call. If you use another
environment manager, make sure `python -m pip --version` works inside the activated environment.

## Locked public-checks environment

`requirements-public-checks.lock` is generated, not hand-edited. It pins the base, `dev` and
`stream` dependencies with hashes for CPython 3.11, generated with uv 0.13.8:

```sh
uv pip compile pyproject.toml --extra dev --extra stream --python-version 3.11 \
  --universal --generate-hashes --no-emit-package pokegent -o requirements-public-checks.lock
```

The lock covers the `public-checks` job on Linux CPython 3.11. It is not a native, other-Python
or cross-platform claim; `native-helper` keeps its own commands. CI installs it with
`--require-hashes`, then installs the project with `--no-deps -e .` and runs `pip check`, so
runtime ranges and metadata stay consistent.

`requirements-public-startup.lock` is the matching lock for `public-startup-latency`. It pins
the base and `dev` dependencies only (no `stream` or `mgba` extras) against the public-checks
lock as a constraint, so shared versions stay aligned. It includes `ruff` through `dev` even
though the startup job only runs `pytest`; it is not a minimal runtime-only lock:

```sh
uv pip compile pyproject.toml --extra dev --python-version 3.11 \
  --universal --generate-hashes --no-emit-package pokegent \
  --constraint requirements-public-checks.lock -o requirements-public-startup.lock
```

To update a dependency deliberately, work from the checkout and keep the regenerated lock in a
fresh scratch directory so the repository file only changes when you accept the diff:

```sh
scratch=$(mktemp -d)
cp requirements-public-checks.lock "$scratch/"
uv pip compile pyproject.toml --extra dev --extra stream --python-version 3.11 \
  --universal --generate-hashes --no-emit-package pokegent \
  --upgrade-package pytest -o "$scratch/requirements-public-checks.lock"
diff -u requirements-public-checks.lock "$scratch/requirements-public-checks.lock"
```

The lock header records the scratch output path, so expect that one line to differ; any other
hunk is a dependency change. If the package is already at the newest allowed version, the diff
has no dependency change; report that instead of inventing a version change. For a real update,
install the scratch lock in a fresh Python 3.11 environment, then install the editable checkout
and check it:

```sh
uv venv --python 3.11 --seed "$scratch/venv"
"$scratch/venv/bin/python" -m pip install --require-hashes \
  -r "$scratch/requirements-public-checks.lock"
"$scratch/venv/bin/python" -m pip install --no-deps -e .
"$scratch/venv/bin/python" -m pip check
"$scratch/venv/bin/python" -c 'import pokegent'
```

Commit the regenerated lock with the reason for the update, and do not add unrelated upgrade
churn.

## Reproducible wheel

Build from a clean checkout with a fixed source epoch. `SOURCE_DATE_EPOCH` normalizes the ZIP
timestamps, and the pinned backend in `pyproject.toml` keeps member output stable:

```sh
epoch=$(git log -1 --format=%ct)
out=$(mktemp -d)
SOURCE_DATE_EPOCH=$epoch python -m pip wheel --no-deps -w "$out" .
ls "$out"  # exactly one pokegent-*.whl
sha256sum "$out"/pokegent-*.whl
```

Record the commit, tree, epoch, wheel hash, CPython version and backend version. Rebuild in a
fresh checkout at the same commit and epoch, then compare the hash. This covers the same
interpreter and zlib (CPython 3.11.18 here). For other environments, compare unpacked member
hashes instead of expecting byte-identical ZIP files. The backend is pinned exactly; `wheel`
stays ranged because the exercised copy is setuptools' vendored one, not a proven standalone
distribution.

New to the project? Read the [README quick start](README.md#quick-start-install-and-run-the-rom-free-checks),
run the checks, and comment on an [open issue](https://github.com/aimd-labs/pokegent/issues)
to confirm scope before a larger change. Focused fixes with a clear reproduction are welcome
directly as pull requests.

## Review and merge policy

Open small, focused PRs targeting `main`. Describe the concrete behavior change, relevant tests,
and exact verification commands and outcomes. Include regression tests for behavioral fixes.
A maintainer must review and approve every PR before it is merged. Maintainers own merging.
The stream's agent bot may open and update PRs; it must never merge PRs.
Changes to runtime legality or irreversible actions require special care and evidence from current
RAM; unverified game state must never be treated as a legal action.

Fork PRs use the same ROM-free checks with read-only permissions and no secrets. Do not add
`pull_request_target` workflows or privileged jobs that execute contributor code.

See [contracts](docs/contracts.md) and [local data requirements](docs/local-data.md).
