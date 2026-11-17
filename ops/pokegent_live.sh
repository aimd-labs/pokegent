#!/usr/bin/env bash
# Observe only: never signal, launch, or change the game writer.
set -eu
CODE=${CODE:-$(cd -- "$(dirname -- "$0")/.." && pwd)}
cd -- "$CODE"
exec "${PY:-python3}" -m pokegent.review monitor "$@"
