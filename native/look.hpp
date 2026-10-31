#pragma once

#include <cstddef>
#include <cstdint>

namespace mcrawsome {

// `pixels` RGB float triples (0 to 1, clipped) through a (size**3, 3) table in .cube order (red fastest),
// interpolated tetrahedrally, written as 8-bit RGB (or BGR) to `out`, on up to `threads` threads.
void ApplyLook(const float* in, uint8_t* out, size_t pixels, const float* table, int size, bool bgr,
               int threads, bool linear = false, float stops = 0.0f);

}  // namespace mcrawsome
