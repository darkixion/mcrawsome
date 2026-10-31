#!/usr/bin/env bash
# A private FFmpeg for the AppImage, which only decodes: Direct Log videos (H.265, ProRes-compatible and
# AV1, with VAAPI or NVIDIA's NVDEC where the host has them), their audio and telemetry. FFmpeg's own
# components all stay enabled, so every command MCRAWsome runs works; the only external library is dav1d
# (software AV1). FFmpeg and dav1d are linked statically, so no system FFmpeg is loaded; the graphics
# loaders (libva, libdrm) and glibc come from the host, bundled or not by the AppImage builder. NVDEC needs
# only NVIDIA's headers (MIT): FFmpeg loads the driver's libcuda and libnvcuvid at run time, when present.
# Usage: build_ffmpeg.sh PREFIX (under ~/.cache/mcrawsome-ffmpeg). Never installs system packages.
set -euo pipefail
readarray -t pins <<'PINS'
ffmpeg|8.1.3|https://ffmpeg.org/releases/ffmpeg-8.1.3.tar.xz|7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3
dav1d|1.5.4|https://downloads.videolan.org/videolan/dav1d/1.5.4/dav1d-1.5.4.tar.xz|686616b7c69eb88d44459391ab25cac13b6647a3b288835c5784e71c1514a5c5
nv-codec-headers|13.1.15.0|https://github.com/FFmpeg/nv-codec-headers/releases/download/n13.1.15.0/nv-codec-headers-13.1.15.0.tar.gz|52532ceade3d5c1af62624986f13cf01b63c910576b08c0c278756c5e4b41ad0
PINS

if [[ $# != 1 || $(uname -s) != Linux ]]; then
    echo "Usage (Linux only): $0 PREFIX" >&2
    exit 2
fi
# Re-exec once so downloads, configuration and every compiler inherit nice 19.
if [[ ${MCRAWSOME_FFMPEG_NICE:-0} != 1 ]]; then
    exec nice -n 19 env MCRAWSOME_FFMPEG_NICE=1 bash "$0" "$@"
fi
root=$(cd "$(dirname "$0")" && pwd -P)
cache="$HOME/.cache/mcrawsome-ffmpeg"
mkdir -p "$cache" "$1"
prefix=$(cd "$1" && pwd -P)
case "$prefix/" in
    "$cache/"*) ;;
    *) echo "PREFIX must be under $cache" >&2; exit 2 ;;
esac
jobs=${MCRAWSOME_FFMPEG_JOBS:-4}
if [[ ! $jobs =~ ^[1-4]$ ]]; then
    echo "MCRAWSOME_FFMPEG_JOBS must be 1 to 4" >&2
    exit 2
fi
for pair in cc:build-essential make:build-essential meson:meson ninja:ninja-build nasm:nasm \
            pkg-config:pkg-config curl:curl tar:tar sha256sum:coreutils ldd:libc-bin; do
    if ! command -v "${pair%%:*}" >/dev/null; then
        echo "Missing prerequisite: apt install ${pair#*:}" >&2
        exit 1
    fi
done
for pair in libva:libva-dev libva-drm:libva-dev libdrm:libdrm-dev; do
    if ! pkg-config --exists "${pair%%:*}"; then
        echo "Missing prerequisite: apt install ${pair#*:}" >&2
        exit 1
    fi
done
mkdir -p "$cache/sources" "$cache/src" "$cache/build-decode" "$cache/tmp"
export TMPDIR="$cache/tmp"
declare -A sources
for row in "${pins[@]}"; do
    IFS='|' read -r name version url checksum <<< "$row"
    archive="$cache/sources/${url##*/}"
    if [[ ! -f $archive ]]; then
        curl --fail --location --retry 3 --proto '=https' "$url" -o "$archive.part"
        mv "$archive.part" "$archive"
    fi
    printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status
    # Extracted afresh: an earlier build's tree may carry a patch this build does not apply.
    src="$cache/src/$name-$version-decode"
    rm -rf "$src" "$src.partial"
    mkdir -p "$src.partial"
    tar -xf "$archive" --strip-components=1 -C "$src.partial"
    mv "$src.partial" "$src"
    sources[$name]=$src
done
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
rm -rf "$cache/build-decode/dav1d" "$cache/build-decode/ffmpeg"
meson setup "$cache/build-decode/dav1d" "${sources[dav1d]}" --prefix="$prefix" --libdir=lib \
    --buildtype=release --default-library=static --wrap-mode=nodownload \
    -Denable_tools=false -Denable_tests=false -Denable_examples=false
ninja -C "$cache/build-decode/dav1d" -j"$jobs"
ninja -C "$cache/build-decode/dav1d" install
make -C "${sources[nv-codec-headers]}" PREFIX="$prefix" install
readarray -t options < "$root/ffmpeg-configure.txt"
mkdir -p "$cache/build-decode/ffmpeg"
(
    cd "$cache/build-decode/ffmpeg"
    "${sources[ffmpeg]}/configure" --prefix="$prefix" "${options[@]}"
    make -j"$jobs"
    make install
)
# The notices and the record of what was built travel with the build, not the repository.
share="$prefix/share/mcrawsome"
mkdir -p "$share/LICENCES"
cp "${sources[ffmpeg]}/COPYING.LGPLv2.1" "$share/LICENCES/FFmpeg-LGPL-2.1.txt"
cp "${sources[ffmpeg]}/LICENSE.md" "$share/LICENCES/FFmpeg-NOTICE.md"
cp "${sources[dav1d]}/COPYING" "$share/LICENCES/dav1d-BSD-2-Clause.txt"
# Each NVIDIA header carries its own copyright and MIT notice.
: > "$share/LICENCES/nv-codec-headers-MIT.txt"
for header in "${sources[nv-codec-headers]}"/include/ffnvcodec/*.h; do
    printf '\n%s\n' "${header##*/}" >> "$share/LICENCES/nv-codec-headers-MIT.txt"
    sed -n '2,/^ \*\//{ /^ \*\//d; p; }' "$header" \
        | sed 's/^ \* \?//' >> "$share/LICENCES/nv-codec-headers-MIT.txt"
done
{
    printf '# Written by build_ffmpeg.sh from verified source releases.\n'
    printf 'linkage = "static FFmpeg and dav1d; the graphics loaders, NVIDIA driver libraries and glibc from the host"\n'
    for row in "${pins[@]}"; do
        IFS='|' read -r name version url checksum <<< "$row"
        printf '\n[[components]]\nname = "%s"\nversion = "%s"\nsource_url = "%s"\nsha256 = "%s"\n' \
            "$name" "$version" "$url" "$checksum"
    done
} > "$share/ffmpeg-manifest.toml"
"$prefix/bin/ffmpeg" -version
# Keep the linkage honest even when system codec development libraries are installed.
for program in ffmpeg ffprobe; do
    dependencies=$(ldd "$prefix/bin/$program")
    if [[ $dependencies =~ lib(avcodec|avformat|avutil|avfilter|avdevice|swscale|swresample|dav1d)\.so ]]; then
        echo "Unexpected shared FFmpeg or codec dependency in $program: $dependencies" >&2
        exit 1
    fi
done
