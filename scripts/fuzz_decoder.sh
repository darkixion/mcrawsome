#!/usr/bin/env bash
# Build both decoder harnesses and seed them from the local fixtures. All generated data stays outside Git.
set -euo pipefail

task_root=$(cd "$(dirname "$0")/.." && pwd)
fuzz_root=${MCRAWSOME_FUZZ_DIR:-$(mktemp -d /tmp/mcrawsome-decoder-fuzz.XXXXXX)}
fuzz_root=$(realpath -m "$fuzz_root")
seconds=${1:-600}
decoder=${2:-both}
if [[ ! "$seconds" =~ ^[1-9][0-9]*$ || ! "$decoder" =~ ^(both|6|7)$ ]]; then
    echo "usage: $0 [seconds] [both|6|7]" >&2
    exit 2
fi
case "$fuzz_root/" in
    "$task_root/"*) echo "MCRAWSOME_FUZZ_DIR must be outside the repository" >&2; exit 2 ;;
esac
mkdir -p "$fuzz_root"/{corpus6,corpus7,artifacts6,artifacts7}
echo "Fuzz files: $fuzz_root"
cd "$task_root"

flags=(-std=c++17 -O1 -g -fno-omit-frame-pointer -fsanitize=fuzzer,address,undefined
       -fno-sanitize-recover=all -I native/motioncam-decoder/lib/include -I native/motioncam-decoder/thirdparty)
clang++ "${flags[@]}" native/fuzz/decoder.cpp native/motioncam-decoder/lib/RawData.cpp -o "$fuzz_root/decoder7"
clang++ "${flags[@]}" -DMCRAWSOME_LEGACY native/fuzz/decoder.cpp native/motioncam-decoder/lib/RawData_Legacy.cpp \
    -o "$fuzz_root/decoder6"

.venv/bin/python - "$fuzz_root" <<'PY'
import struct
import sys
from pathlib import Path

from mcrawsome.io.clip import Clip
from mcrawsome.io.container import read_frame_record, read_index

root = Path(sys.argv[1])
for name in ("clip_12bit.mcraw", "clip_10bit_rotated.mcraw"):
    path = Path("tests/fixtures/data") / name
    with path.open("rb") as f, Clip(path) as clip:
        index = read_index(f)
        for i, ref in enumerate(index.frames):
            payload, _ = read_frame_record(f, ref.offset)
            meta = clip.frame_metadata(i)
            seed = struct.pack("<II", meta.width, meta.height) + payload
            (root / "corpus7" / f"{path.stem}_{i}").write_bytes(seed)
        # The fixtures contain type 7 only. Encode their pixels as type 6 blocks for legacy seeds.
        tile = clip.raw_frame(0).data[:4, :64]
        blocks = []
        for row in tile:
            for start in range(0, 64, 32):
                for parity in (0, 1):
                    values = [int(v) for v in row[start + parity : start + 32 : 2]]
                    blocks.append(b"\xf0\x00" + struct.pack(">16H", *values))
        (root / "corpus6" / path.stem).write_bytes(struct.pack("<II", 64, 4) + b"".join(blocks) + b"\x00")

# Small valid seeds exercise every packed block format without decoding an entire 4K frame.
lengths7 = [0, 8, 16, 24, 32, 40, 48, 64, 64, 80, 80, 128, 128, 128, 128, 128, 128]
lengths6 = [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 32, 32, 32, 32, 32]
for depth in range(17):
    table = struct.pack("<I", 64) + bytes([0, depth])
    references = struct.pack("<I", 64) + b"\x00\x00"
    image = b"\x00" * (4 * lengths7[depth])
    offset = 16 + len(image)
    payload = struct.pack("<4I", 64, 4, offset, offset + len(table)) + image + table + references
    (root / "corpus7" / f"depth_{depth}").write_bytes(struct.pack("<II", 64, 4) + payload)
for depth, length in enumerate(lengths6):
    block = bytes([depth << 4, 0]) + b"\x00" * length
    payload = block * 16 + b"\x00"
    (root / "corpus6" / f"depth_{depth}").write_bytes(struct.pack("<II", 64, 4) + payload)
PY

run_decoder() {
    local kind=$1
    ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1 \
        "$fuzz_root/decoder$kind" "$fuzz_root/corpus$kind" -max_total_time="$seconds" \
        -timeout=30 -rss_limit_mb=2048 -max_len=8388608 -print_final_stats=1 \
        -artifact_prefix="$fuzz_root/artifacts$kind/" 2>&1 | tee "$fuzz_root/decoder$kind.log"
}
if [[ "$decoder" == both ]]; then
    run_decoder 6 &
    legacy_pid=$!
    run_decoder 7 &
    current_pid=$!
    result=0
    wait "$legacy_pid" || result=1
    wait "$current_pid" || result=1
    exit "$result"
fi
run_decoder "$decoder"
