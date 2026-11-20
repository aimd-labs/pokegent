# Native mGBA setup

Tested boundary: Ubuntu 24.04 amd64, CPython 3.11, `mgba==0.10.2` and Ubuntu
`libmgba0.10t64=0.10.2+dfsg-1.1build3`. ROM-free checks proved imports and empty-core
initialization. Later [clean-start acceptance](clean-start.md) reached the first field and
three RAM-verified movements using these pinned public archives offline in a fresh
Python environment.
That gameplay attempt did not invoke the helper's download command below.
Other operating systems, architectures and minimal Ubuntu images are unverified.

The [PyPI binding](https://pypi.org/project/mgba/0.10.2/) is an unofficial
MPL-2.0 package. Its metadata says the upstream Python bindings are deprecated;
it does not claim upstream support. The Linux wheel requires a separate native
library and contains a RUNPATH pointing at the packager's build directory.
The explicit private load path below takes precedence; inspect actual loaded
paths when diagnosing imports.

Use a fresh private directory outside the checkout. The native helper downloads
four exact Ubuntu packages, verifies their SHA256, and extracts them without root.
It exposes only mGBA, libzip, Lua and inih in its `lib` directory. All remaining
libraries must already be available on the host; a missing host library stops
setup. It neither installs host packages nor downloads a dependency closure.
The hashes were checked against Ubuntu's signed noble package indexes.

Host prerequisites come from the standard Ubuntu packages `libedit2`, `libgl1`,
`libavcodec60`, `libavfilter9`, `libavformat60`, `libavutil58`, `libswscale7`,
`libswresample4`, `zlib1g`, `libpng16-16t64`, `libsqlite3-0`, `libelf1t64`,
`libbz2-1.0`, `libssl3t64`, `libstdc++6` and `libc6`, plus their runtime
dependencies. The standard system-package alternative is Ubuntu's public
[libmgba0.10t64 package](https://packages.ubuntu.com/noble/libmgba0.10t64).
Provisioning host packages is outside this helper. The tested host already had
these prerequisites; this is neither a self-contained runtime nor a fresh-OS proof.

```sh
task_prefix=/absolute/private/pokegent-native
mkdir -m 700 "$task_prefix"
export TMPDIR="$task_prefix/tmp" PIP_CACHE_DIR="$task_prefix/pip-cache"
mkdir -m 700 "$TMPDIR" "$PIP_CACHE_DIR"
python3.11 -m venv "$task_prefix/venv"
. "$task_prefix/venv/bin/activate"
python -m pip --version
bash tools/setup_mgba_env.sh "$task_prefix/native"
export LD_LIBRARY_PATH="$task_prefix/native/lib"

# Exact CPython 3.11 / Linux amd64 binding artifact; dependencies stay in the venv.
curl --fail --max-time 60 --output "$task_prefix/mgba.whl" \
  https://files.pythonhosted.org/packages/ef/35/e36389901864add5e02c23c548358a54c852e15a5b116d25157d1449c1c5/mgba-0.10.2-cp311-cp311-manylinux1_x86_64.whl
printf '%s  %s\n' \
  bdfd9157e09041ebcbcde59a8bc3c82de6e70394552449b8f03e4e392adcadbd \
  "$task_prefix/mgba.whl" | sha256sum --check
mv "$task_prefix/mgba.whl" \
  "$task_prefix/mgba-0.10.2-cp311-cp311-manylinux1_x86_64.whl"
python -m pip install "$task_prefix/mgba-0.10.2-cp311-cp311-manylinux1_x86_64.whl"
python -m pip install -e .
python -m pip check
```

Keep the native export scoped to this shell; do not add extracted system library
directories to a global profile. Preserve failed/partial prefixes and choose a new
directory for another attempt. The helper refuses existing native prefixes,
including normalized aliases, and requires the prefix's parent directory to exist.

Before using a ROM, check imports in a fresh process (no emulator frames run):

```sh
timeout 20s python - <<'PY'
import mgba.core, mgba.gba, mgba.image, mgba.vfs
from mgba._pylib import ffi, lib
print(mgba.core.__file__)
print(ffi.string(lib.projectVersion).decode())
print("\n".join(line for line in open('/proc/self/maps') if 'libmgba.' in line))
PY
```

Expect the binding module under your venv and `libmgba.so.0.10.2` under your
private native prefix. The ROM-free proof used `mCoreCreate(mPLATFORM_GBA)`, successful
`core.init(core)`, and exactly one `core.deinit(core)` through CFFI ownership,
without loading a ROM, resetting/running frames or sending input. Import success
alone does not establish ROM/save isolation or successful gameplay.
