# Pokegent

**Status: alpha. Full emulator runs need externally supplied local game data; automatic generation from your own ROM is planned.**

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

These checks need no ROM or emulator. Full emulator runs and ROM cache building are not ready
from this checkout alone: a verified local map index is required, and its automatic generator
is planned. Set `POKEGENT_LOCAL_DATA_DIR` to a verified local data directory to enable
Fly routing and elevator, quiz and mansion actions. Those features stay disabled if their files
are absent. See [local data requirements](docs/local-data.md) for exact filenames and schemas.

For later emulator setup, install `.[mgba,dashboard]` and configure `FIRERED_ROM_PATH` with an
absolute path outside the checkout. `POKEGENT_LOCAL_DATA_DIR` supplies the local game-data files;
`POKEGENT_MAP_GROUPS` optionally overrides the map index.
Keep generated data outside the repository. Unknown ROM revisions fail closed.

The local dashboard listens on `http://127.0.0.1:19701`.
See [configuration](docs/configuration.md), the [objective format](docs/objective-file.md),
and [contracts](docs/contracts.md). Copy `pokegent.example.yaml` to ignored `pokegent.yaml`
for local configuration. Keep credentials in environment variables.

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
