#!/usr/bin/env bash
# Download and extract the native mGBA runtime without root.
set -euo pipefail
trap 'printf "error: setup_mgba_env.sh failed\n" >&2' ERR

if [[ $# -ne 1 ]]; then
    printf 'usage: %s <libmgba-root>\n' "$0" >&2
    exit 2
fi

runtime_root=$1
mkdir -p "$runtime_root"
runtime_root=$(cd "$runtime_root" && pwd)
command -v apt-cache apt-get dpkg >/dev/null || {
    printf 'error: setup_mgba_env.sh requires apt-cache, apt-get, and dpkg\n' >&2
    exit 1
}
debs_dir="$runtime_root/debs"
extract_root="$runtime_root/root"
lib_dir="$extract_root/usr/lib/x86_64-linux-gnu"

mkdir -p "$debs_dir" "$extract_root"
cd "$debs_dir"

mapfile -t packages < <(apt-cache depends --recurse --no-recommends --no-suggests \
    --no-conflicts --no-breaks --no-replaces --no-enhances libmgba0.10t64 \
    | grep '^[a-z0-9]' | sort -u)
apt-get download "${packages[@]}" >&2

shopt -s nullglob
debs=("$debs_dir"/*.deb)
if ((${#debs[@]} == 0)); then
    printf 'error: apt-get download produced no .deb files\n' >&2
    exit 1
fi
for deb in "${debs[@]}"; do
    dpkg -x "$deb" "$extract_root"
done

printf 'export LD_LIBRARY_PATH="%s:%s/samba:%s/blas:%s/lapack:%s/atlas"\n' \
    "$lib_dir" "$lib_dir" "$lib_dir" "$lib_dir" "$lib_dir"
