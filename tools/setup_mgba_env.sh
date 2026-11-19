#!/usr/bin/env bash
# Extract pinned Ubuntu 24.04 amd64 libraries without changing host packages.
set -euo pipefail
trap 'printf "error: setup_mgba_env.sh failed\n" >&2' ERR

if [[ $# -ne 1 ]]; then
    printf 'usage: %s <libmgba-root>\n' "$0" >&2
    exit 2
fi

source /etc/os-release
if [[ $ID != ubuntu || $VERSION_ID != 24.04 || $(uname -m) != x86_64 ]]; then
    printf 'error: supported platform is Ubuntu 24.04 amd64 only\n' >&2
    exit 1
fi
for command in curl sha256sum dpkg-deb ldconfig; do
    command -v "$command" >/dev/null || {
        printf 'error: required command missing: %s\n' "$command" >&2
        exit 1
    }
done

# Reuse host dependencies; never download a recursive closure or replace libc.
host_libraries=$(ldconfig -p)
for library in libedit.so.2 libGL.so.1 libavcodec.so.60 libavfilter.so.9 \
    libavformat.so.60 libavutil.so.58 libswscale.so.7 libswresample.so.4 \
    libz.so.1 libpng16.so.16 libsqlite3.so.0 libelf.so.1 libm.so.6 libc.so.6 \
    libbz2.so.1.0 libssl.so.3 libcrypto.so.3 libstdc++.so.6; do
    if ! awk -v name="$library" '$1 == name { found=1 } END { exit !found }' \
        <<< "$host_libraries"; then
        printf 'error: missing host library %s; no host packages were changed\n' "$library" >&2
        exit 1
    fi
done

runtime_root=$1
if [[ $runtime_root != /* || $runtime_root == *:* || $runtime_root == *$'\n'* \
    || -e $runtime_root || -L $runtime_root ]]; then
    printf 'error: choose a new absolute private directory outside the checkout\n' >&2
    exit 2
fi
checkout_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
runtime_root=$(realpath -m -- "$runtime_root")
if [[ $runtime_root == "$checkout_root" || $runtime_root == "$checkout_root/"* ]]; then
    printf 'error: native packages must stay outside the checkout\n' >&2
    exit 2
fi
umask 077
mkdir -p "$runtime_root/debs" "$runtime_root/root" "$runtime_root/lib"
debs_dir="$runtime_root/debs"
extract_root="$runtime_root/root"
lib_dir="$runtime_root/lib"

# Hashes verified against Ubuntu noble signed InRelease -> Packages indexes.
while read -r digest package_path; do
    deb="$debs_dir/${package_path##*/}"
    curl --fail --silent --show-error --max-time 60 --max-filesize 2097152 --output "$deb" \
        "https://archive.ubuntu.com/ubuntu/$package_path"
    printf '%s  %s\n' "$digest" "$deb" | sha256sum --check --status
    dpkg-deb -x "$deb" "$extract_root"
done <<'PACKAGES'
3c2088fec5a3219082f7d77dd95911ef2cae1aca838f60a560a261c0bed7d23b pool/universe/m/mgba/libmgba0.10t64_0.10.2+dfsg-1.1build3_amd64.deb
d7484efd8d9968ecab9e488fde59980fc633e0eb2bbf7f1ea8e33bf71c9ff1fb pool/universe/libz/libzip/libzip4t64_1.7.3-1.1ubuntu2_amd64.deb
6a520259e5b06e86191a25af6d448387cdd2b3d291a0aa956ce272e84d071260 pool/main/l/lua5.4/liblua5.4-0_5.4.6-3build2_amd64.deb
7afede2f1279e0c22ffec5d682de3a1f72bc3bea337a1bb94fe7006a840d1a9f pool/main/libi/libinih/libinih1_55-1ubuntu2_amd64.deb
PACKAGES

# Export only these libraries; extracted base libraries never enter the load path.
for library in libmgba.so.0.10 libzip.so.4 liblua5.4.so.0 libinih.so.1; do
    source_library="$extract_root/usr/lib/x86_64-linux-gnu/$library"
    if [[ ! -f $source_library ]]; then
        printf 'error: pinned package lacks %s\n' "$library" >&2
        exit 1
    fi
    ln -s "$source_library" "$lib_dir/$library"
done

printf 'export LD_LIBRARY_PATH=%q\n' "$lib_dir"
