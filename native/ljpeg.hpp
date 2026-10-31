// Lossless JPEG (ITU T.81 process 14, SOF3) encoding of CFA tiles for DNG compression 7, and decoding.
#pragma once

#include <cstdint>
#include <vector>

namespace mcrawsome {

// Encode a (height, width) CFA tile, width even, as width/2 x height with two interleaved components
// (so each photosite is predicted from the last one of the same colour on its row), predictor 1, at
// `bits` precision (2 to 16), with an optimal Huffman table for this tile.
std::vector<uint8_t> EncodeLosslessJpeg(const uint16_t* tile, int width, int height, int bits);

struct DecodedJpeg {
    int width = 0;       // samples per component per row
    int height = 0;
    int components = 0;
    std::vector<uint16_t> samples;  // height rows of width * components samples, components interleaved
};

// Decode a lossless JPEG stream (one scan, 1 to 4 components without subsampling, predictors 1 to 7,
// point transform, restart intervals of whole rows). Throws std::runtime_error on malformed data; every
// read is bounds-checked.
DecodedJpeg DecodeLosslessJpeg(const uint8_t* data, size_t size);

}  // namespace mcrawsome
