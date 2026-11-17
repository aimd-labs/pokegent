# Persistent owner operations

`/opt/pokegent-owner-bootstrap/pokegent-owner-start --config /etc/pokegent-owner/owner.json` starts one owner agent
inside Bubblewrap on Linux. It does not start, stop or signal the game. Run it as a dedicated,
unprivileged Unix user. Provision directories and permissions before launch. The launcher never
creates host directories, copies private inputs, changes a service, or falls back to an unsandboxed
command. Install Bubblewrap separately; user namespaces must be available.

## Protected, pinned bootstrap

Production must run an operator-installed copy of `ops/hermes/pokegent-owner-start`, pinned to a
reviewed commit, outside the checkout, profile, live state and every other owner-writable path.
As the operator, extract that exact revision to a protected staging directory, record its commit
and SHA-256 digest, and install it under an operator-owned directory. For example (replace the
revision and staging path):

```sh
git show REVIEWED_COMMIT:ops/hermes/pokegent-owner-start > /path/to/operator-staging/pokegent-owner-start
sha256sum /path/to/operator-staging/pokegent-owner-start
sudo install -d -o root -g root -m 0755 /opt/pokegent-owner-bootstrap
sudo install -o root -g root -m 0755 /path/to/operator-staging/pokegent-owner-start /opt/pokegent-owner-bootstrap/pokegent-owner-start
```

Use the installed absolute path in the operator-controlled session/service startup definition.
Never restart by executing the checkout's copy, copying from an unreviewed owner branch, or using
an owner-writable wrapper. All parent directories, including staging and startup configuration,
must be protected from the owner: a read-only file in a writable parent can be replaced. The launcher
rejects a production bootstrap beneath writable bindings, symlinked paths, owner-owned components
(even mode 0555), and paths writable through group permissions or ACLs. Only the operator updates
the pinned bootstrap after review. The copy is standalone and imports only the system standard library.

`--synthetic-test` explicitly permits an unprotected bootstrap for disposable test fixtures and
recording executables. It must never appear in a production startup definition; it disables trust
checks and does not provide the production launch guarantee.

## One game code home

Use one standalone public clone at the reviewed release and one environment containing its installed
package. Install the console entry with that environment's installer, for example
`/opt/pokegent-env/bin/python -m pip install /srv/pokegent`. Inside the sandbox, `pokegent` resolves
to `/opt/pokegent-env/bin/pokegent`. There is no alternate module wrapper or import-path injection.
An editable install may point only at this same public clone. Check `.pth` files, interpreter
symlinks, console shebangs and native-library dependencies before rollout: every external dependency
must resolve in an explicit read-only runtime mount or the system `/usr` mount. Never mount a second
game checkout or a private custom-code repository. Use an environment without credentials or
unrelated editable installs. The owner agent runtime is separate tooling, not a gameplay source.

Copy `ops/hermes/owner.example.json` to a private location outside Git and replace all example paths.
Copy `ops/hermes/AGENTS.owner.md` into the profile's supported instruction location (for example,
`<profile>/AGENTS.md` with `cwd` set to `profile`); keep the checkout's development AGENTS.md intact.
The example is a schema illustration, not a runnable deployment. Paths are mounted at the same
absolute locations so interpreter shebangs, virtual environments and profile references still work.
All mount sources must already exist, be canonical (no symlinks), and be mutually disjoint. Put the
environment outside the checkout and live data outside the profile. Linked Git worktrees are rejected
because they require Git metadata from another checkout. Custom mounts cannot overlay system paths.

| Field | Meaning |
|---|---|
| `repo` | The sole public checkout; writable by default for PR branches. `repo_writable: false` makes it read-only. |
| `environment` | Read-only installed Pokegent environment; must contain `bin/pokegent`. |
| `agent_runtime` | Read-only owner-agent runtime. Its root `.env` and `.git/config`, when present, are masked. Audit other files for credentials before mounting. |
| `agent_command` | Literal argv array, with executable inside `agent_runtime`. Resolve the agent's actual runtime command during provisioning if its wrapper depends on an install manager. No shell expansion; no credentials in argv. |
| `home` | Empty sandbox-only HOME. The host home, SSH keys and general credentials are never mounted implicitly. |
| `profile` | Writable dedicated owner profile, including its private model configuration and session database. |
| `live_home` | Writable game state, objective and review data; not code. Keep the existing single-writer policy. |
| `cwd` | Existing directory inside an owner root; defaults to `repo`. The example uses `profile` to load its owner instructions. |
| `profile_name` | Optional agent profile selector; omitted by default. |
| `owner_uid` | Optional numeric uid enforcement; otherwise the current non-root uid. |
| `resume` | `auto` (default), `always`, or `never`. `auto` appends `resume_args` when `resume_file` exists. |
| `resume_file` / `resume_args` | Defaults: `<profile>/state.db` / `["--continue"]`. File must stay inside profile. Use the agent runtime's latest-session flag; no session identifier is stored in public config. |
| `mounts` | Explicit `{ "path": "/absolute/narrow/path", "role": "data" }` entries. No globbing or discovery of neighboring installs. |
| `environment_variables` | Only `POKEGENT_ROM_PATH`, `POKEGENT_LOCAL_DATA_DIR`, `POKEGENT_MAP_GROUPS`, `LD_LIBRARY_PATH`, `TERM`. These values must be non-secret. |
| `bwrap` | Absolute launcher executable, default `/usr/bin/bwrap`; substitute a recording fake for synthetic tests only. |

Extra mount roles are `data` (read-only ROM, generated tables, YAML config), `runtime` (read-only
interpreters, native libraries or owner tool dependencies), `tool` (one read-only executable file),
`credential` (one read-only credential file), and `state` (writable dedicated tool state/lease
directory). Bind only the exact required install-state directories; keep executable generations
read-only. Roles document the operator's trust boundary: the launcher cannot prove that a directory
contains data rather than executable code. Audit the full manifest and contents before deployment.
Do not mount a parent home, credential store, host root, old launcher directory or general workspace.
Network remains shared for loopback telemetry and external APIs. PID, IPC, UTS and user namespaces
are isolated; capabilities are dropped; `/tmp`, `/proc` and `/dev` are sandbox-local. Only selected
system resolver, identity, certificate and loader files under `/etc` are exposed read-only.

All inherited descriptors above standard input/output/error are closed before Bubblewrap executes.
The inherited environment is cleared, including authentication, proxy, debug, `PYTHONPATH` and
`PYTHONHOME` variables. Model keys belong only in the dedicated private profile. Never put secrets
in `environment_variables`, `agent_command`, resume arguments, shell tracing or launch logs.
The privileged operator owns the manifest and the launch path; the owner must not be allowed to
edit either. A sandbox cannot protect a signing key against code already running as its owner:
scope the App installation to this single repository and enforce review/default-branch protection
server-side. An in-process PR-only command policy is an additional guard, not a security boundary
against a holder of the signing key.

Copy `ops/hermes/AGENTS.owner.md` into the dedicated profile as `AGENTS.md` (or the agent's configured
instruction file). Set `cwd` to that directory, and verify that the agent loads it and the public
checkout's instructions. Keep deployment-specific notes and session state private. The owner may
edit a PR branch; approved deployment must not execute unreviewed owner edits. A pinned installed
wheel keeps the running game separate from source edits without another source checkout.

## Repository-scoped GitHub App commands

`tools/github_app_gh.py` uses Python's standard library, `/usr/bin/openssl` and a separately installed
`gh` executable. It imports no private broker. Create a GitHub App installation restricted to one
repository with Contents, Issues and Pull requests write permission (Metadata read is implicit).
Require maintainer review through repository rules; the bot must have no ruleset bypass.

Private helper configuration (not committed):

```json
{
  "app_id": "APP_CLIENT_ID",
  "installation_id": 123,
  "repository": "example-org/example-game",
  "private_key": "/var/lib/pokegent-secrets/app.pem",
  "gh": "/opt/owner-tools/gh"
}
```

Make the key owner-readable only (mode 0600 or 0400). Mount the key individually read-only and keep
the helper config in the dedicated profile. The helper requests exactly the configured repository
and the three permissions above, validates the returned repository/permissions, and fails closed.
It sends an RS256 JWT to GitHub over verified HTTPS; proxy inheritance and redirects are disabled.
See GitHub's [installation token contract](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
and [JWT contract](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app).

The private helper configuration has these fields (all values below are illustrative):

```json
{
  "app_id": "example-client-id",
  "installation_id": 1234,
  "repository": "example/game",
  "private_key": "/var/lib/pokegent-secrets/app.pem",
  "gh": "/opt/owner-tools/gh"
}
```

```sh
python3 -I /srv/pokegent/tools/github_app_gh.py --config /path/to/private-app.json -- pr list
python3 -I /srv/pokegent/tools/github_app_gh.py --config /path/to/private-app.json -- pr create --title 'Fix objective review' --body-file /path/to/private-pr-body.txt
```

The helper permits only the listed PR/issue operations, never merge, authentication, arbitrary API
calls, extensions, editors, templates, jq filters or another repository selector. It never outputs
a JWT or installation token, writes them to disk or passes them in argv. The child receives
`GH_TOKEN` in a fresh environment
with empty temporary gh configuration; output is captured and exact credentials are redacted before
forwarding. Output appears only after `gh` exits. Do not use environment dumps or debug tracing.
This helper does not provision Git push credentials; configure a reviewed external Git credential
provider scoped to the same installation if authenticated push is required. Do not place tokens in
remote URLs or command-line arguments. Same-uid process inspection remains possible, so use the
dedicated Unix account and keep unrelated processes out of it.

## Review and rollout evidence

Run the ROM-free checks in CONTRIBUTING.md, including `tests/test_owner_launcher.py` and
`tests/test_github_app_gh.py`. The recording launcher test asserts the complete binding set, installed
CLI PATH, cleared inherited environment, current directory, and latest-session resume behavior.
Authentication tests use synthetic keys/responses and a fake child; they contact no services.

After maintainer approval, an operator should review the exact manifest, verify the installed
`pokegent --help` and `pokegent status --help` inside a disposable sandbox, check package origins and
agent instruction loading, and verify latest-session resumption before live cutover. Archive old
unsandboxed launchers and remove their active PATH entries during that separately approved rollout.
Do not reset a profile, migrate a live save, restart a stream or run a real credential probe as part
of these synthetic checks. Unknown legality and the one-emulator/one-writer invariant remain in force.
