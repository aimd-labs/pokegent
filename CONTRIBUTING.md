# Contributing

Use Python 3.11 and the existing pyproject dependencies. Installation resolves the declared
version ranges in pyproject.toml. Do not add dependencies without a concrete need.

```sh
uv venv --python 3.11 --seed
. .venv/bin/activate
uv pip install -e '.[dev,stream]' pytest
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
