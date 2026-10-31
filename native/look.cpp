// A look's 3D table applied to a preview frame: tetrahedral interpolation as looks/cube.py's apply_cube
// (and FFmpeg's lut3d=interp=tetrahedral), straight to 8-bit, on several threads.
#include "look.hpp"
#include "preview.hpp"

#include <algorithm>
#include <cmath>
#include <thread>
#include <vector>

namespace mcrawsome {
namespace {

void apply_rows(const float* in, uint8_t* out, const float* table, int size, size_t first, size_t last,
                bool bgr, bool linear, float gain) {
    for (size_t i = first; i < last; ++i)
        mcrawsome::PreviewLook(in + 3 * i, out + 3 * i, table, size, bgr, linear, gain);
}

}  // namespace

void ApplyLook(const float* in, uint8_t* out, size_t pixels, const float* table, int size, bool bgr,
               int threads, bool linear, float stops) {
    const float gain = std::exp2(stops);
    const int count = std::max(1, std::min<int>(threads, static_cast<int>(pixels / 65536) + 1));
    std::vector<std::thread> workers;
    const size_t share = (pixels + count - 1) / count;
    for (int t = 1; t < count; ++t) {
        const size_t first = std::min(pixels, t * share), last = std::min(pixels, (t + 1) * share);
        workers.emplace_back(apply_rows, in, out, table, size, first, last, bgr, linear, gain);
    }
    apply_rows(in, out, table, size, 0, std::min(pixels, share), bgr, linear, gain);
    for (auto& worker : workers) {
        worker.join();
    }
}

}  // namespace mcrawsome
