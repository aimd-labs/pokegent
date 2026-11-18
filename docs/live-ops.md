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

As the operator, copy `ops/hermes/owner.example.json` to a protected private location outside Git
(for example `/etc/pokegent-owner/owner.json`) and replace all example paths. The manifest must be
readable by the dedicated owner but owned by the operator; use an operator-owned directory mode
0755 and file mode 0644 containing only non-secret paths/settings, or a restricted read-only group
for private settings. Never store secrets in this manifest. Protect every parent directory from
owner writes/replacement, and keep it outside every writable binding. Production checks ownership
and effective write access (including groups/ACLs) on the file and every ancestor before reading it;
symlinks in the file or parent path are rejected. Mode 0444 alone is insufficient for owner-owned
files. A manifest in the writable profile is rejected. Provision this layout as the operator;
the launcher never repairs permissions or elevates privileges.
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
| `bwrap` | Absolute canonical executable, default `/usr/bin/bwrap`. Production requires operator ownership and protected parents outside writable bindings. Recording fakes require `--synthetic-test`. |

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
The privileged operator owns the manifest, pinned bootstrap and Bubblewrap executable; the owner
must not be allowed to edit or replace them or their parent paths. Rejection of an unsafe launch
configuration happens before any child executes. A sandbox cannot protect a signing key against code already running as its owner:
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

The helper validates an explicit option schema for each operation before signing or requesting a
token. It accepts the following subset of `gh`; new upstream options require review before being
added. Value options accept `--name VALUE`, `--name=VALUE` and their listed short alias, including
attached short values such as `-L10` or `-tTitle`. Boolean switches accept standalone names and
recognized short clusters (for example `-df`); equals-valued booleans are rejected. Unknown flags,
abbreviations, `--` terminators, extra positionals and URL/branch selectors are rejected. Select a
PR/issue with one positive number; PR operations may omit it to use the current branch. Issue
view/edit/comment require the number. No selector is accepted for create/list.

| Operation | Permitted value options | Permitted switches |
|---|---|---|
| PR create | `--title/-t`, `--body/-b`, `--body-file/-F`, `--assignee/-a`, `--label/-l`, `--milestone/-m`, `--base/-B`, `--head/-H`, `--reviewer/-r` | `--draft/-d`, `--fill/-f`, `--no-maintainer-edit` |
| Issue create | Same as PR create except base, head and reviewer | None |
| PR edit | `--title/-t`, `--body/-b`, `--body-file/-F`, `--milestone/-m`, `--add-assignee`, `--remove-assignee`, `--add-label`, `--remove-label`, `--base/-B`, `--add-reviewer`, `--remove-reviewer` | `--remove-milestone` |
| Issue edit | Same as PR edit except base and reviewer options | `--remove-milestone` |
| PR list | `--assignee/-a`, `--author/-A`, `--label/-l`, `--limit/-L`, `--search/-S`, `--state/-s`, `--app`, `--json`, `--base/-B`, `--head/-H` | `--draft/-d` |
| Issue list | Same as PR list except base/head; also `--mention`, `--milestone/-m` | None |
| PR/issue view | `--json` | `--comments/-c` |
| PR/issue comment | `--body/-b`, `--body-file/-F` | `--edit-last`, `--create-if-none` |
| PR diff | `--color`, `--exclude` | `--name-only`, `--patch` |
| PR checks | `--json`, `--interval/-i` | `--required`, `--watch`, `--fail-fast` |

Create requires a nonempty title and an explicit body/body-file, or PR `--fill`. Comment requires
a body/body-file; edit requires an explicit change. Body and body-file are mutually exclusive;
`--create-if-none` requires `--edit-last`. These checks avoid implicit editor/prompt paths. Limits
and intervals must be positive integers; state and color must be supported values. In list/view/checks,
`-t` is a forbidden formatter; in create/edit it is the permitted title option. Even benign attached
formatter forms and `--editor=false` fail before any JWT, token request or child process.

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

## Optional native owner status and progress heartbeat

`ops/hermes/owner_status.py` is a single-file, process-local OSC 7501 adapter for the inspected
classic Hermes TUI. It changes no installed Hermes/Core files or provider configuration. It
reports working at input admission/dispatch, blocked across overlapping approval/question/auth
callbacks, and error on failed or uncertain turns. It reports idle only after native idle
housekeeping, initial rendering and session validation, with no command, modal, local/gateway
queue, held input, continuation or living turn worker. A still-running interrupted worker prevents
idle. Neither chat return, first render nor process exit alone reports idle. Unchanged status is
not re-emitted, preserving Core's idle epoch. Source hashes pin the inspected lifecycle; an upgrade
requires review of those sources and synthetic tests before changing the pins. Unsupported or
missing interfaces fail closed. Voice mode conservatively remains non-idle.

For this adapter the existing launcher additionally accepts:

| Field | Contract |
|---|---|
| `status_adapter` | Absolute canonical, separately mounted, operator-protected copy of the reviewed `owner_status.py`, outside every writable mount. |
| `native_runtime_command` | JSON argv emitted by that installation's `hermes --print-runtime-command`, **without startup arguments**. Only the inspected `python -I -c` bootstrap and its exact `hermes_cli.main` entry are accepted. |
| `resume` | Must be `always`. |
| `resume_args` | Must be `["--resume", "EXACT_EXISTING_SESSION_ID"]`; latest/continue/fresh fallback is refused. |

The parent operator captures the runtime command during installation; construction of the public
sandbox command never executes the native launcher (which may perform startup repair). The public
launcher substitutes only that known entry with `runpy.run_path` of the reviewed absolute adapter.
All native bootstrap text, profile preprocessing, startup/resume argument tokens and the existing
cleared sandbox environment remain in place. No cwd import, PYTHONPATH injection, additional
executable root or plain-Python fallback is supported. Native main preprocesses the profile before
`cli` is imported; a different resulting profile or initialized session refuses idle. Session
identity remains pinned at every readiness check; a session rollover needs operator reconciliation,
not automatic latest-session selection. The process holds a private profile lock for its lifetime,
and native active-session exclusion remains enabled.

The native interpreter, source, dependency generations, dependency selection/lease metadata and
all native imports must already have an explicit readable sandbox closure using `agent_runtime`
and existing read-only `runtime` mounts. Code roots and all their contents/parents must be protected
from owner ownership/writes, with no symlinks; the adapter additionally checks activated imports
against these roots (plus `/usr`). Native dependency maintenance/leases may require a layout that
can separate writable bookkeeping from immutable code without overlapping mounts. **Do not weaken
the mount or protection rules to fit an existing home-directory installation.** If the installed
native bootstrap cannot run within this closure, report the missing protected installation or
layout prerequisite and stop before launch. The launcher intentionally refuses owner-owned
runtime trees even if their files are chmod 0555. Install the reviewed launcher, adapter and private
manifest with protected parents before the parent schedules a same-session boundary handoff.
No files in a live profile need to be changed by this feature's implementation.

`ops/hermes/owner_heartbeat.py` is an optional one-shot companion, separate from B2's durable
objective/run review journal. Run it with the existing installed Python environment containing
Pillow; it adds no dependencies. Example operator timer command (all paths/addresses are examples):

```sh
/opt/pokegent-env/bin/python /opt/pokegent-owner-bootstrap/owner_heartbeat.py \
  --state /var/lib/pokegent-notify/heartbeat --target t123 \
  --dashboard http://127.0.0.1:19770 --stream http://127.0.0.1:19770
```

A normal process-manager timer may invoke it every 30 seconds. The data directory must be absolute,
canonical, owned by its caller, mode 0700, and outside Git; lock/receipt files use 0600. Use one
private directory per configured target. No service/timer is installed by this repository.
The helper never restarts the owner or game, writes objectives, clears B2 state or changes legality.

The helper reads Core's supported Terminal schema (`state`, `status.state/app`, `status_ms`,
`created_ms`, `pid`, `foreground.pid`, `address`), requiring a live Hermes idle epoch strictly older
than 300 seconds. Unknown/malformed/blocked/busy/future evidence suppresses delivery. It requires
a bounded, verified 240x160 PNG and supported operator `/ops/telemetry` plus `/ops/health`: attached
emulator, running current run, matching run IDs, valid state updated within its positive TTL (at most
5 seconds), document age at most 5 seconds and no recorded capture errors. Cached PNG alone never
qualifies. Identical validated configured origins use the supported paired deployment: one
ViewerState and one FrameHub shared by operator and stream handlers (for example both origins
`http://127.0.0.1:19770`). This is an operator topology prerequisite, not a runtime identity token;
a reverse proxy or multiple workers without that shared hub need a separate pairing protocol.
Distinct origins retain the strict telemetry/PNG/health frame and PNG/health sequence equality.
The helper never discovers ports or substitutes another runtime.

For the paired topology the child takes one ordered telemetry D0, health H0, PNG P, health H1,
telemetry D1 sample, with a 50 ms observation gap between H0 and P. A monotonic 500 ms budget
includes all reads, the gap, parsing and PNG verification; there is no retry loop. Both documents
must independently satisfy freshness/readiness/valid-state/running checks, retain the same nonempty
run ID (also in their game fields), and have nondecreasing document/RAM timestamps and frame cursors.
Both health samples must be running, ready, attached, error-free, with typed positive sequence and
capture counts and nonnegative frame counters. Booleans are not counters. Capture-count growth must
match sequence growth; ambiguous sampled stats suppress delivery.

The partial ordering is D0.frame <= D1.frame, D0.frame <= P.frame,
H0.frame <= P.frame <= H1.frame and H0.seq <= P.seq <= H1.seq. Adjacent equal sequences require equal
frames; advancing sequences require advancing frames. Fresh telemetry may lag a newer PNG, so
P.frame <= D1.frame is deliberately not required. If all five frame cursors agree, a genuine still
image qualifies. Otherwise P.seq must exceed H0.seq and H1 must show capture-count growth, proving
in this shared hub that P was captured after the baseline. An old P followed by a newer H1 cannot
qualify. Pixels need not change and no emulator advancement is requested.

A 50 ms gap spans the default 30 Hz and maximum 60 Hz capture periods, but cannot guarantee a new
capture at the supported 1 Hz minimum. A fixed periodic timer may repeatedly hit a
nonqualifying phase and keep suppressing moving samples; this is a known timing follow-up.
This bounded sampler intentionally keeps the cache proof and does not claim liveness for every supported capture cadence. Slow deployments need
separately reviewed timing or a run-bound capture envelope before claiming that guarantee.
Missing/old/future RAM, run changes, detach/error, rollback, contradictory counters, bad PNG or an
exhausted budget suppress delivery. The returned expiry is the earliest original document/RAM
expiry across D0/D1, capped by the remaining 500 ms sample validity; neither timestamp gets a new
TTL. Expiry during the Core recheck still suppresses delivery.

The complete media check runs in a disposable child with an independent three-second parent timeout
covering DNS, open/headers, all five reads and PNG verification. On expiry the child is killed and
reaped before the transaction lock is released; no reminder is sent.
HTTP retains bounded reads, no redirects or ambient proxy;
Core subprocesses have five-second deadlines and use argv without shell expansion.

A nonblocking private file lock covers receipt read, eligibility, media probe, Core epoch/process
recheck, final `hc send --if idle`, and atomic fsynced receipt commit. The receipt names the configured
target, terminal creation, process IDs and **original** idle epoch. Busy/refused/failed/timed-out
sends do not create a success receipt. Success is validated against the installed Core SendResult
schema. A crash, lost acknowledgement or receipt-write failure after accepted delivery can lead
to another delivery: this is **at least once**, like B2, with a stable review ID in the static prompt.
B2 and heartbeat can coexist; both retain the final atomic idle guard. The heartbeat does not drain
or replace B2. The prompt asks the owner to inspect fresh status/objective/private notes and continue
already authorized legal progress without another GO.

Parent acceptance after independent review: verify protected closure first, preserve the exact
current profile/session, and hand off at a run boundary with one owner. Then verify real working,
blocked and idle transitions, refusal while busy, controlled idle delivery and private receipt,
fresh supported telemetry and game picture. Those live steps are operator work; synthetic tests
neither send owner messages nor prove a target installation ready.
