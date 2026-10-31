#include <motioncam/RawData.hpp>

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <stdexcept>
#include <vector>

namespace {
uint32_t read_u32(const uint8_t* data) {
    return uint32_t(data[0]) | (uint32_t(data[1]) << 8) | (uint32_t(data[2]) << 16) | (uint32_t(data[3]) << 24);
}
}

extern "C" int LLVMFuzzerTestOneInput(const uint8_t* data, size_t size) {
    if (size < 8) {
        return 0;
    }
    const uint32_t width = read_u32(data);
    const uint32_t height = read_u32(data + 4);
    // Bound the harness's allocations while keeping the full fixture dimensions in the corpus.
    if (width == 0 || height == 0 || width > 8192 || height > 4096 || width * height > 16 * 1024 * 1024) {
        return 0;
    }
#ifndef MCRAWSOME_LEGACY
    if (size >= 24 && read_u32(data + 8) > 8192) {
        return 0;
    }
#endif
    const size_t pixels = static_cast<size_t>(width) * height;
    std::vector<uint16_t> output(pixels + 16, 0xa55a);
    // Exercise unaligned payloads too: the Python binding provides no alignment contract.
    std::vector<uint8_t> payload(size - 8 + 1);
    std::copy(data + 8, data + size, payload.begin() + 1);
    try {
#ifdef MCRAWSOME_LEGACY
        const size_t written = motioncam::raw::DecodeLegacy(output.data(), width, height, payload.data() + 1, size - 8);
#else
        const size_t written = motioncam::raw::Decode(output.data(), width, height, payload.data() + 1, size - 8);
#endif
        if (written != pixels) {
            std::abort();
        }
    } catch (const std::invalid_argument&) {
        // Malformed input is expected; other failures remain visible.
    }
    if (!std::all_of(output.begin() + pixels, output.end(), [](uint16_t value) { return value == 0xa55a; })) {
        std::abort();
    }
    return 0;
}
