# Pokegent

**Status: alpha. The checks below need no ROM or emulator. Full emulator runs need local data generated from your own supported ROM and a native mGBA installation.**

Pokegent is an LLM agent that plays Pokémon FireRed through mGBA. It reads current RAM,
presents typed legal actions, executes one action at a time, and checks the result against RAM.
The optional System One provider supports `agent`, `hybrid` (default), and `system1` modes.

## Requirements

For the ROM-free setup and checks:

- Python 3.11 (`>=3.11,<3.12`), git, and an environment manager such as uv.
- No ROM, emulator, credentials or game services.

For local emulator runs, in addition:

- mGBA Python bindings and the native `libmgba` library. mGBA is external; it is not bundled
  in this repository. See the [bounded native setup](docs/native-install.md) for the tested
  Ubuntu 24.04 amd64 / Python 3.11 path.
- Your own legally obtained supported FireRed ROM, kept outside the checkout. No ROM, save,
  extracted game tables, map index, sprites or gameplay recordings are included.

## Quick start: install and run the ROM-free checks

```sh
git clone https://github.com/aimd-labs/pokegent.git
cd pokegent
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

`--seed` also installs `pip` in the virtualenv; the wheel-building tests invoke it.
All five checks should pass and need neither a ROM nor a native emulator. They use
synthetic memory and data. `check_rom_data.py` rejects committed game data.
Installation and the packaging tests may download Python packages through pip build
isolation, so use a package index or a warm cache. They need no credentials, game
services or ROM. See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution and review path.

`requirements-public-checks.lock` pins the ROM-free `public-checks` environment (base, `dev`
and `stream` extras) with hashes. `requirements-public-startup.lock` separately pins the
`public-startup-latency` environment (base and `dev`, so it includes `ruff` even though that
job only runs `pytest`; the job needs the runtime base and `pytest`, not a bare-minimum
runtime). Both were generated with uv 0.13.8 for CPython 3.11 from the commands in
[CONTRIBUTING.md](CONTRIBUTING.md#locked-public-checks-environment), and the startup lock
uses the public-checks lock as constraints so shared versions stay aligned. The `--no-deps`
edit install keeps the declared ranges subject to `pip check`. These are Linux CPython 3.11
locks and are not native or cross-platform claims; `native-helper` keeps its own commands.

## Tested combinations

| Scope | Documented as tested | Not verified |
| --- | --- | --- |
| ROM-free checks | CPython 3.11: `public-checks` and `public-startup-latency` on GitHub Actions `ubuntu-latest`; `native-helper` (six synthetic tests only) on `ubuntu-24.04`; run [45531183848](https://github.com/aimd-labs/pokegent/actions/runs/45531183848) at `77a1a2e8`; the `ubuntu-latest` image version is not recorded in public run metadata | macOS, Windows, other Python versions; the exact image behind `ubuntu-latest` |
| Native/emulator path | Ubuntu 24.04 amd64, CPython 3.11, `mgba==0.10.2`, `libmgba0.10t64=0.10.2+dfsg-1.1build3` | Other systems, architectures and minimal images |
| Game data | FireRed USA Rev 0; local setup reached the first field and three RAM-verified movements | Other ROM revisions, full-game coverage |

The [native setup](docs/native-install.md) and [clean-start](docs/clean-start.md) guides
carry the source pins and limits behind these rows. Setup success alone is not gameplay proof,
and synthetic checks do not establish the full compatibility matrix.

### Supported-version policy

Version ranges in `pyproject.toml` (Python `>=3.11,<3.12` and the dependency ranges)
declare what installation accepts; they are not claims that every accepted version was
tested. Only the combinations in the table are tested, and each row's cited run, commit or
document is its evidence. An editable checkout is identified by its actual source commit
and package origin, not by a package version string alone; evidence should name both.
Everything outside the table, including other operating systems, Python versions,
architectures and ROM revisions, is unverified or unsupported even where code paths exist
or `source-lock.md` lists upstream sources. ROM-free checks and native binding imports are
not gameplay proof, and no row claims complete campaign reliability.

## Optional: emulator play with your own ROM

The quick start does not install or exercise the emulator. For an emulator run:

1. Complete the [native mGBA setup](docs/native-install.md).
2. Generate the local data and private ROM cache with
   [`pokegent rom-data setup --rom "$FIRERED_ROM_PATH" --out-dir "$POKEGENT_LOCAL_DATA_DIR"`](docs/local-data.md#explicit-local-setup).
3. Export `POKEGENT_LOCAL_DATA_DIR` and an absolute `FIRERED_ROM_PATH` before startup.
4. Follow the [clean-start recipe](docs/clean-start.md) for a fresh game and agent choices.

Install `.[firered,dashboard]` for later emulator setup (`firered` and `rom` are aliases for
`mgba`). The extra installs the Python binding only; the separate native `libmgba` and the
[bounded native setup](docs/native-install.md) preflight still apply. `POKEGENT_LOCAL_DATA_DIR`
supplies the local game-data files; `POKEGENT_MAP_GROUPS` optionally overrides the map index.
Keep generated data outside the repository. Unknown ROM revisions fail closed.

The local dashboard listens on `http://127.0.0.1:19701`.
See [connect your own agent or model](docs/connect-your-agent.md),
[configuration](docs/configuration.md), the [objective format](docs/objective-file.md),
and [contracts](docs/contracts.md). Copy `pokegent.example.yaml` to ignored `pokegent.yaml`
for local configuration. Keep credentials in environment variables.
See [local run ledger commands](docs/run-ledger.md) for explicit ingestion, queries and splits.

## Built live on stream

An AI agent plays FireRed live on Twitch under the channel name **PokeJEV**.
Stream link coming soon. The stream's agent sends fixes to this repository as pull requests.
Maintainers review and merge accepted changes; the agent bot never merges its own PRs.

## Contributing and security

New contributors can run the checks above, then comment on an
[open issue](https://github.com/aimd-labs/pokegent/issues) to confirm scope before a larger
change. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, and the required maintainer review.
Report vulnerabilities privately through GitHub Security Advisories; see [SECURITY.md](SECURITY.md).
The [Code of Conduct](CODE_OF_CONDUCT.md) applies to project spaces.

## License and trademarks

Code is licensed under [MIT](LICENSE), retaining the AIMD Labs and Nous Research copyright notices.
See [third-party notices](THIRD_PARTY_NOTICES.md) for provenance and external dependencies.
The MIT license does not grant rights to any game data or graphics.

Pokémon and FireRed are trademarks of their respective owners. This project is independent and
is not affiliated with, endorsed by, or sponsored by Nintendo, Game Freak, or The Pokémon Company.
