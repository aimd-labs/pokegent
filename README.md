# Pokegent

**Status: alpha. Full emulator runs need local data generated from your own supported ROM and a native mGBA installation.**

Pokegent is an LLM agent that plays Pokémon FireRed through mGBA. It reads current RAM,
presents typed legal actions, executes one action at a time, and checks the result against RAM.
The optional System One provider supports `agent`, `hybrid` (default), and `system1` modes.

## Requirements

- Python 3.11 and a Python environment manager such as uv.
- mGBA Python bindings and the native `libmgba` library for emulator runs. mGBA is external;
  it is not bundled in this repository. See the [bounded native setup](docs/native-install.md)
  for the tested Ubuntu 24.04 amd64 / Python 3.11 path.
- Bring your own legally obtained supported FireRed ROM, kept outside the checkout.
  No ROM, save, extracted game tables, map index, sprites, or gameplay recordings are included.

## Setup and checks

```sh
uv venv --python 3.11
. .venv/bin/activate
uv pip install -e '.[dev,stream]' pytest
ruff check .
python -m compileall -q pokegent addons tools tests
python tests/games/gen3/test_imports.py
python -m pytest -q
python tools/check_rom_data.py
```

These checks need no ROM or emulator. Generate the local data and private ROM cache with
[`pokegent rom-data setup --rom "$FIRERED_ROM_PATH" --out-dir "$POKEGENT_LOCAL_DATA_DIR"`](docs/local-data.md#explicit-local-setup).
The [clean-start recipe](docs/clean-start.md) covers native prerequisites, a fresh game and
agent choices. Local acceptance reached the first field and three RAM-verified movements on
the documented Ubuntu 24.04 host; setup success alone is not that gameplay proof.
Set `POKEGENT_LOCAL_DATA_DIR` before startup. Optional features stay disabled if their files are
absent; see [local data requirements](docs/local-data.md) for filenames and schemas.
The main `ubuntu-latest` CI job uses Ubuntu 26.04, where the six native-helper tests skip.
The separate `native-helper` job runs all six on Ubuntu 24.04 and rejects skips or an empty suite.
These synthetic checks use neither native binaries nor a ROM.

For later emulator setup, install `.[firered,dashboard]` (`firered` and `rom` are aliases for
`mgba`) and configure `FIRERED_ROM_PATH` with an absolute path outside the checkout. The extra
installs the Python binding only; the separate native `libmgba` and the [bounded native
setup](docs/native-install.md) preflight still apply on the tested Ubuntu 24.04 amd64, CPython
3.11, `mgba==0.10.2` boundary. `POKEGENT_LOCAL_DATA_DIR` supplies the local game-data files;
`POKEGENT_MAP_GROUPS` optionally overrides the map index.
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

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, and the required maintainer review.
Report vulnerabilities privately through GitHub Security Advisories; see [SECURITY.md](SECURITY.md).
The [Code of Conduct](CODE_OF_CONDUCT.md) applies to project spaces.

## License and trademarks

Code is licensed under [MIT](LICENSE), retaining the AIMD Labs and Nous Research copyright notices.
See [third-party notices](THIRD_PARTY_NOTICES.md) for provenance and external dependencies.
The MIT license does not grant rights to any game data or graphics.

Pokémon and FireRed are trademarks of their respective owners. This project is independent and
is not affiliated with, endorsed by, or sponsored by Nintendo, Game Freak, or The Pokémon Company.
