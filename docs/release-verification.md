# Release verification

These steps let a reader check a release without trusting the maintainers.
## 1. Regenerate and diff the public-checks lock
`requirements-public-checks.lock` is generated with uv and records its own command
in the header. Regenerate it in a scratch directory so the repository file changes
only when you accept the diff:

```sh
scratch=$(mktemp -d)
cp requirements-public-checks.lock "$scratch/"
uv pip compile pyproject.toml --extra dev --extra stream --python-version 3.11 --universal --generate-hashes --no-emit-package pokegent -o "$scratch/requirements-public-checks.lock"
diff -u requirements-public-checks.lock "$scratch/requirements-public-checks.lock"
```

Only the header line differs (it records the scratch output path); any other hunk is a real dependency change, and a lock that matches elsewhere shows the committed pins still reproduce.
The diff is pin-set evidence; package security stays outside its scope.
## 2. Verify the wheel manifest
`tools/wheel_manifest.py` records SHA-256 and size for a wheel and each member plus
the `dist_info` fields; use it to build a reference, then check a wheel copy:

```sh
python -m tools.wheel_manifest /path/to/pokegent.whl --source-commit REVIEWED_COMMIT > /path/to/wheel-manifest.json
python -m tools.verify_wheel_manifest /path/to/pokegent.whl /path/to/wheel-manifest.json --source-commit REVIEWED_COMMIT
```
It prints `{"matches": true, ...}` and exits 0 when wheel bytes, members,
`dist_info` fields and source commit match (exit 1 on a difference, exit 2 on an
unreadable or invalid wheel, manifest or commit). Byte-for-byte ZIP equality holds
only for one interpreter and zlib; compare member hashes in other environments.
This proves the copy has the recorded bytes; `source_commit_verified` stays false,
so it is not build, provenance or attestation evidence.
## 3. Check required checks at one head
`tools/check_required_checks.py` reads a complete check-runs export for one exact
head: combine every endpoint page into one object with a `check_runs` list and a
`total_count` equal to its length, then run it with the head and each name:

```sh
python -m tools.check_required_checks /path/to/check-runs.json --head HEAD_SHA --required CI_NAME --required SECOND_CI_NAME
```

The tool reads only the file and never calls GitHub. It reports `ready: true` and
exits 0 when each required name has one completed run at `HEAD_SHA` with a
`success` conclusion; anything else is not ready (exit 1); malformed input or
arguments exit 2. It is check evidence for one head, not review, mergeability or
admin policy, and it is not merge authorization.
