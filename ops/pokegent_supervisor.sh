#!/usr/bin/env bash
# Run in the foreground under a process manager. Environment values are data, never sourced.
set -eu
CODE=${CODE:-$(cd -- "$(dirname -- "$0")/.." && pwd)}
cd -- "$CODE"
exec "${PY:-python3}" -m pokegent.supervisor "$@"
