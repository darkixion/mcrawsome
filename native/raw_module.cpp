// Python binding for MotionCam's frame decompression (raw::Decode / raw::DecodeLegacy).
#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>

#include <motioncam/RawData.hpp>

#include "ljpeg.hpp"
#include "look.hpp"
#include "preview.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace py = pybind11;

namespace {
constexpr int kCompressionLegacy = 6;
constexpr int kCompression = 7;

int preview_threads(size_t pixels) {
    return std::max(1u, std::min({8u, std::thread::hardware_concurrency(),
                                 static_cast<unsigned>(pixels / 65536 + 1)}));
}

uint32_t read_u32(const uint8_t* data, size_t offset) {
    const auto* p = data + offset;
    return uint32_t(p[0]) | (uint32_t(p[1]) << 8) | (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24);
}

py::array_t<uint16_t> decode(const py::bytes& data, int width, int height, int compression_type) {
    if (width <= 0 || height <= 0 || width % 2 != 0 || height % 2 != 0) {
        throw py::value_error("width and height must be positive and even");
    }
    if (compression_type != kCompression && compression_type != kCompressionLegacy) {
        throw py::value_error("unsupported compression type " + std::to_string(compression_type));
    }
    // The binding owns a reference to immutable Python bytes for this entire call,
    // including while the GIL is released. Borrow its storage instead of copying a frame.
    char* bytes = nullptr;
    Py_ssize_t length = 0;
    if (PyBytes_AsStringAndSize(data.ptr(), &bytes, &length) != 0) throw py::error_already_set();
    const auto* src = reinterpret_cast<const uint8_t*>(bytes);
    const auto size = static_cast<size_t>(length);
    if (size == 0) {
        throw py::value_error("empty frame data");
    }
    if (compression_type == kCompression) {
        if (size < 16) {
            throw py::value_error("frame data is truncated");
        }
        const uint32_t encoded_width = read_u32(src, 0);
        const uint32_t encoded_height = read_u32(src, 4);
        if (encoded_height != static_cast<uint32_t>(height)) {
            throw py::value_error("frame header says " + std::to_string(encoded_height) +
                                  " rows, the clip says " + std::to_string(height));
        }
        if (height % 4 != 0 || encoded_width % 64 != 0 || encoded_width < static_cast<uint32_t>(width) ||
            encoded_width > static_cast<uint32_t>(std::numeric_limits<int>::max())) {
            throw py::value_error("frame dimensions are invalid");
        }
    } else {
        // Every row has two headers per padded 32-pixel block, followed by the trailer marker.
        const size_t blocks = (static_cast<size_t>(width) + 31) / 32 * static_cast<size_t>(height);
        if (width > std::numeric_limits<int>::max() - 31 || blocks > (size - 1) / 4) {
            throw py::value_error("frame data is truncated");
        }
    }
    {
        py::gil_scoped_release release;
        if (compression_type == kCompression) {
            motioncam::raw::Validate(width, height, src, size);
        } else {
            motioncam::raw::ValidateLegacy(width, height, src, size);
        }
    }
    py::array_t<uint16_t> out({static_cast<py::ssize_t>(height), static_cast<py::ssize_t>(width)});
    uint16_t* dst = out.mutable_data();
    size_t written = 0;
    {
        py::gil_scoped_release release;
        written = compression_type == kCompression
                      ? motioncam::raw::Decode(dst, width, height, src, size)
                      : motioncam::raw::DecodeLegacy(dst, width, height, src, size);
    }
    if (written == 0) {
        throw py::value_error("failed to decompress frame");
    }
    return out;
}
py::bytes encode_ljpeg(const py::array_t<uint16_t, py::array::c_style | py::array::forcecast>& tile, int bits) {
    if (tile.ndim() != 2) {
        throw py::value_error("the tile must be a 2-D array");
    }
    const int height = static_cast<int>(tile.shape(0));
    const int width = static_cast<int>(tile.shape(1));
    std::vector<uint8_t> encoded;
    {
        py::gil_scoped_release release;
        encoded = mcrawsome::EncodeLosslessJpeg(tile.data(), width, height, bits);
    }
    return py::bytes(reinterpret_cast<const char*>(encoded.data()), encoded.size());
}
py::array_t<uint16_t> decode_ljpeg(const py::bytes& data) {
    const std::string buffer = data;
    mcrawsome::DecodedJpeg decoded;
    {
        py::gil_scoped_release release;
        try {
            decoded = mcrawsome::DecodeLosslessJpeg(reinterpret_cast<const uint8_t*>(buffer.data()), buffer.size());
        } catch (const std::runtime_error& exc) {
            py::gil_scoped_acquire acquire;
            throw py::value_error(exc.what());
        }
    }
    const auto columns = static_cast<py::ssize_t>(decoded.width) * decoded.components;
    py::array_t<uint16_t> out({static_cast<py::ssize_t>(decoded.height), columns});
    std::copy(decoded.samples.begin(), decoded.samples.end(), out.mutable_data());
    return out;
}
py::array_t<uint8_t> apply_table(const py::array_t<float, py::array::c_style | py::array::forcecast>& frame,
                                const py::array_t<float, py::array::c_style | py::array::forcecast>& table,
                                bool bgr, bool linear, float stops) {
    if (frame.ndim() != 3 || frame.shape(2) != 3) {
        throw py::value_error("the frame must be a (height, width, 3) array");
    }
    const auto entries = table.ndim() == 2 && table.shape(1) == 3 ? table.shape(0) : 0;
    int size = 2;
    while (static_cast<py::ssize_t>(size) * size * size < entries) {
        ++size;
    }
    if (entries == 0 || static_cast<py::ssize_t>(size) * size * size != entries) {
        throw py::value_error("the table must be (size**3, 3) with size at least 2");
    }
    py::array_t<uint8_t> out({frame.shape(0), frame.shape(1), static_cast<py::ssize_t>(3)});
    const size_t pixels = static_cast<size_t>(frame.shape(0)) * static_cast<size_t>(frame.shape(1));
    const int threads = linear ? preview_threads(pixels)
                               : static_cast<int>(std::min(4u, std::max(1u, std::thread::hardware_concurrency())));
    const float* in = frame.data();
    const float* lut = table.data();
    uint8_t* dst = out.mutable_data();
    {
        py::gil_scoped_release release;
        mcrawsome::ApplyLook(in, dst, pixels, lut, size, bgr, threads, linear, stops);
    }
    return out;
}
// The same inpaint-opposed ratios as develop/highlights.py, at the reduced preview size.
void recover_preview(float* rgb, const uint8_t* masks, size_t width, size_t height) {
    bool target[3] = {false, false, false};
    const size_t pixels = width * height;
    for (size_t p = 0; p < pixels; ++p) {
        const auto* m = masks + 3 * p;
        if (!(m[0] && m[1] && m[2]))
            for (int c = 0; c < 3; ++c) target[c] |= m[c];
    }
    if (!target[0] && !target[1] && !target[2]) return;
    const size_t stride = width + 1;
    std::vector<uint32_t> integral(stride * (height + 1), 0);
    for (size_t y = 0; y < height; ++y) {
        uint32_t row = 0;
        for (size_t x = 0; x < width; ++x) {
            const auto* m = masks + 3 * (y * width + x);
            row += bool(m[0] || m[1] || m[2]);
            integral[(y + 1) * stride + x + 1] = integral[y * stride + x + 1] + row;
        }
    }
    std::vector<size_t> ring;
    for (size_t y = 0; y < height; ++y) {
        const size_t y0 = y > 8 ? y - 8 : 0, y1 = std::min(height, y + 9);
        for (size_t x = 0; x < width; ++x) {
            const size_t p = y * width + x;
            const auto* m = masks + 3 * p;
            if (m[0] || m[1] || m[2]) continue;
            const size_t x0 = x > 8 ? x - 8 : 0, x1 = std::min(width, x + 9);
            if (integral[y1 * stride + x1] - integral[y0 * stride + x1] -
                integral[y1 * stride + x0] + integral[y0 * stride + x0]) ring.push_back(p);
        }
    }
    float ratio[3] = {1, 1, 1};
    for (int c = 0; c < 3; ++c) {
        if (!target[c]) continue;
        const int a = (c + 1) % 3, b = (c + 2) % 3;
        std::vector<float> values;
        values.reserve(ring.size());
        for (size_t p : ring) {
            const float opposed = 0.5f * (rgb[3*p+a] + rgb[3*p+b]);
            if (opposed > 1e-6f) values.push_back(rgb[3*p+c] / opposed);
        }
        if (!values.empty()) {
            const size_t middle = values.size() / 2;
            std::nth_element(values.begin(), values.begin() + middle, values.end());
            ratio[c] = values[middle];
            if (values.size() % 2 == 0)
                ratio[c] = 0.5f * (ratio[c] + *std::max_element(values.begin(), values.begin() + middle));
        }
    }
    for (size_t p = 0; p < pixels; ++p) {
        const auto* m = masks + 3*p;
        if (!(m[0] || m[1] || m[2]) || (m[0] && m[1] && m[2])) continue;
        const float original[3] = {rgb[3*p], rgb[3*p+1], rgb[3*p+2]};
        for (int c = 0; c < 3; ++c) if (m[c])
            rgb[3*p+c] = std::max(original[c], ratio[c] * 0.5f * (original[(c+1)%3] + original[(c+2)%3]));
    }
}

// Normalise Bayer quads, average green and recover clipped channels before the colour matrix.
py::tuple develop_preview(
    const py::array_t<uint16_t, py::array::c_style | py::array::forcecast>& raw,
    const py::array_t<float, py::array::c_style | py::array::forcecast>& black,
    const py::array_t<float, py::array::c_style | py::array::forcecast>& gain,
    const std::string& cfa, int step, bool rotate, int max_width, float white, bool recover) {
    if (raw.ndim() != 2 || black.ndim() != 2 || gain.ndim() != 2 ||
        raw.shape(0) != black.shape(0) || raw.shape(1) != black.shape(1) ||
        raw.shape(0) != gain.shape(0) || raw.shape(1) != gain.shape(1) ||
        (step != 2 && step != 4) || raw.shape(0) < step || raw.shape(1) < step ||
        cfa.size() != 4 || std::count(cfa.begin(), cfa.end(), 'r') != 1 ||
        std::count(cfa.begin(), cfa.end(), 'g') != 2 || std::count(cfa.begin(), cfa.end(), 'b') != 1) {
        throw py::value_error("invalid preview planes, Bayer arrangement or step");
    }
    if (max_width < 1) throw py::value_error("preview width must be positive");
    const size_t source_height = raw.shape(0) / step, source_width = raw.shape(1) / step;
    const size_t width = std::min(source_width, static_cast<size_t>(max_width));
    const size_t height = std::max<size_t>(1, std::lround(source_height * double(width) / source_width));
    const int stride = static_cast<int>(raw.shape(1));
    py::array_t<float> out({static_cast<py::ssize_t>(height), static_cast<py::ssize_t>(width),
                            static_cast<py::ssize_t>(3)});
    const auto* src = raw.data();
    const auto* blacks = black.data();
    const auto* gains = gain.data();
    py::array_t<uint8_t> masks({static_cast<py::ssize_t>(height), static_cast<py::ssize_t>(width),
                              static_cast<py::ssize_t>(3)});
    auto* clipped = masks.mutable_data();
    auto* dst = out.mutable_data();
    const int count = preview_threads(width * height);
    const int red = cfa.find('r'), blue = cfa.find('b');
    int greens[2], n = 0;
    for (int i = 0; i < 4; ++i) if (cfa[i] == 'g') greens[n++] = i;
    const int offsets[4] = {(red / 2) * stride + red % 2,
        (greens[0] / 2) * stride + greens[0] % 2,
        (greens[1] / 2) * stride + greens[1] % 2,
        (blue / 2) * stride + blue % 2};
    auto rows = [&](size_t first, size_t last) {
        for (size_t y = first; y < last; ++y) {
            for (size_t x = 0; x < width; ++x) {
                const size_t pixel = rotate ? height * width - 1 - (y * width + x) : y * width + x;
                mcrawsome::PreviewReduce(src, blacks, gains, offsets, stride, step,
                    x * source_width / width, y * source_height / height, white,
                    dst + 3 * pixel, clipped + 3 * pixel);
            }
        }
    };
    {
        py::gil_scoped_release release;
        std::vector<std::thread> workers;
        const size_t share = (height + count - 1) / count;
        for (int t = 1; t < count; ++t)
            workers.emplace_back(rows, std::min(height, t * share), std::min(height, (t + 1) * share));
        rows(0, std::min(height, share));
        for (auto& worker : workers) worker.join();
        if (recover) recover_preview(dst, clipped, width, height);
    }
    return py::make_tuple(out, masks);
}
void neutral_preview(py::array_t<float, py::array::c_style> rgb,
                     const py::array_t<float, py::array::c_style | py::array::forcecast>& mask) {
    if (rgb.ndim() != 3 || rgb.shape(2) != 3 || mask.ndim() != 2 ||
        rgb.shape(0) != mask.shape(0) || rgb.shape(1) != mask.shape(1))
        throw py::value_error("preview mask must match the RGB frame");
    auto* dst = rgb.mutable_data();
    const auto* blend = mask.data();
    const size_t pixels = mask.size();
    auto part = [&](size_t first, size_t last) {
        for (size_t p = first; p < last; ++p) {
            const float m = blend[p];
            if (m == 0) continue;
            const float white = std::max(1.0f, std::max(dst[3*p], std::max(dst[3*p+1], dst[3*p+2])));
            for (int c = 0; c < 3; ++c) dst[3*p+c] = dst[3*p+c] * (1-m) + white * m;
        }
    };
    {
        py::gil_scoped_release release;
        std::vector<std::thread> workers;
        const int count = preview_threads(pixels);
        const size_t share = (pixels + count - 1) / count;
        for (int t = 1; t < count; ++t) workers.emplace_back(part, std::min(pixels, t*share), std::min(pixels, (t+1)*share));
        part(0, std::min(pixels, share));
        for (auto& worker : workers) worker.join();
    }
}

py::array_t<float> encode_preview(
    const py::array_t<float, py::array::c_style | py::array::forcecast>& linear, float stops) {
    if (linear.ndim() != 3 || linear.shape(2) != 3) throw py::value_error("expected RGB frame");
    py::array_t<float> out({linear.shape(0), linear.shape(1), linear.shape(2)});
    const auto* src = linear.data();
    auto* dst = out.mutable_data();
    const size_t values = linear.size();
    const float gain = std::exp2(stops);
    auto part = [&](size_t first, size_t last) {
        for (size_t i = first; i < last; ++i) {
            const float x = src[i] * gain;
            dst[i] = mcrawsome::PreviewCode(x);
        }
    };
    {
        py::gil_scoped_release release;
        std::vector<std::thread> workers;
        const int count = preview_threads(values / 3);
        const size_t share = (values + count - 1) / count;
        for (int t = 1; t < count; ++t) workers.emplace_back(part, std::min(values, t * share), std::min(values, (t + 1) * share));
        part(0, std::min(values, share));
        for (auto& worker : workers) worker.join();
    }
    return out;
}
// Lens shading (and an exposure gain) baked into CFA values, as mcrawsome.output.dng.bake_shading computes
// them in float32: (v - black) * gain map * gain + black, rounded to nearest even, clipped to 0..white.
// Each step rounds as NumPy's float32 operations do (the target is built with -ffp-contract=off).
py::array_t<uint16_t> bake_cfa(const py::array_t<uint16_t, py::array::c_style>& raw,
                               const py::array_t<float, py::array::c_style | py::array::forcecast>& black,
                               const py::object& gains, float gain, float white) {
    if (raw.ndim() != 2 || raw.shape(0) % 2 || raw.shape(1) % 2)
        throw py::value_error("the CFA must be a 2D array with even sides");
    if (black.ndim() != 2 || black.shape(0) != 2 || black.shape(1) != 2)
        throw py::value_error("black levels must be a 2x2 array, one per Bayer site");
    const size_t height = raw.shape(0), width = raw.shape(1);
    py::array_t<float, py::array::c_style | py::array::forcecast> map;
    const bool has_map = !gains.is_none();  // a default-constructed array is empty, not null
    if (has_map) {
        map = py::array_t<float, py::array::c_style | py::array::forcecast>::ensure(gains);
        if (!map || map.ndim() != 2 || size_t(map.shape(0)) != height || size_t(map.shape(1)) != width)
            throw py::value_error("the gain map must match the CFA");
    }
    if (!(white >= 0.0f) || white > 65535.0f) throw py::value_error("white level out of range");
    py::array_t<uint16_t> out({height, width});
    const uint16_t* src = raw.data();
    const float* sites = black.data();
    const float* factors = has_map ? map.data() : nullptr;
    uint16_t* dst = out.mutable_data();
    {
        py::gil_scoped_release release;
        const int threads = preview_threads(height * width);
        auto work = [&](size_t first, size_t last) {
            for (size_t y = first; y < last; ++y) {
                const float row_black[2] = {sites[(y & 1) * 2], sites[(y & 1) * 2 + 1]};
                for (size_t x = 0; x < width; ++x) {
                    const size_t i = y * width + x;
                    const float b = row_black[x & 1];
                    float v = static_cast<float>(src[i]) - b;
                    if (factors) v = v * factors[i];
                    if (gain != 1.0f) v = v * gain;
                    v = std::nearbyint(v + b);
                    dst[i] = static_cast<uint16_t>(std::clamp(v, 0.0f, white));
                }
            }
        };
        std::vector<std::thread> pool;
        const size_t step = (height + threads - 1) / threads;
        for (int t = 1; t < threads; ++t)
            pool.emplace_back(work, std::min(height, t * step), std::min(height, (t + 1) * step));
        work(0, std::min(height, step));
        for (auto& thread : pool) thread.join();
    }
    return out;
}
// Green interpolation uses the same phase-edge replication as NumPy's padded sub-images, rather
// than clamping photosite coordinates (which would change the CFA phase at the border).
py::list remosaic_green(const py::list& phases, int green_mask) {
    using Array = py::array_t<float, py::array::c_style>;
    if (phases.size() != 16 || (green_mask != 6 && green_mask != 9))
        throw py::value_error("remosaic green needs sixteen phases and two diagonal green sites");
    std::vector<Array> source, result;
    for (const auto& phase : phases) source.push_back(py::cast<Array>(phase));
    if (source[0].ndim() != 2 || source[0].shape(0) == 0 || source[0].shape(1) == 0)
        throw py::value_error("remosaic phases must be nonempty 2D arrays");
    const py::ssize_t height = source[0].shape(0), width = source[0].shape(1);
    const float* values[16];
    float* output[16];
    py::list out;
    for (int p = 0; p < 16; ++p) {
        if (source[p].ndim() != 2 || source[p].shape(0) != height || source[p].shape(1) != width)
            throw py::value_error("remosaic phases must have matching shapes");
        values[p] = source[p].data();
        const int site = 2 * ((p / 4) >> 1) + ((p % 4) >> 1);
        if (green_mask & (1 << site)) result.push_back(source[p]);
        else result.emplace_back(std::vector<py::ssize_t>{height, width});
        output[p] = (green_mask & (1 << site)) ? nullptr : result.back().mutable_data();
        out.append(result.back());
    }
    {
        py::gil_scoped_release release;
        auto work = [&](py::ssize_t first, py::ssize_t last) {
            for (int p = 0; p < 16; ++p) {
                const int py = p / 4, px = p % 4;
                const int site = 2 * (py >> 1) + (px >> 1);
                if (green_mask & (1 << site)) continue;
                for (py::ssize_t y = first; y < last; ++y) {
                    struct Neighbour { const float* row; int shift; };
                    Neighbour near[2][5];
                    float before[2];
                    for (int vertical = 0; vertical < 2; ++vertical) {
                        const int offset = (vertical ? py : px) & 1;
                        const int steps[5] = {offset ? -2 : -1, offset ? -3 : -2,
                                              offset ? 1 : 2, offset ? 2 : 3, offset ? -1 : 1};
                        before[vertical] = offset ? float(1.0 / 3) : float(2.0 / 3);
                        for (int k = 0; k < 5; ++k) {
                            const int yy = py + (vertical ? steps[k] : 0) + 4;
                            const int xx = px + (vertical ? 0 : steps[k]) + 4;
                            const auto sy = std::clamp(y + yy / 4 - 1, py::ssize_t(0), height - 1);
                            near[vertical][k] = {values[(yy % 4) * 4 + xx % 4] + sy * width, xx / 4 - 1};
                        }
                    }
                    auto pixel = [&](py::ssize_t x, bool edge) {
                        const size_t i = y * width + x;
                        float total = 0.0f, weights = 0.0f;
                        for (int vertical = 0; vertical < 2; ++vertical) {
                            float samples[5];
                            for (int k = 0; k < 5; ++k) {
                                const auto& n = near[vertical][k];
                                const auto sx = edge ? std::clamp(x + n.shift, py::ssize_t(0), width - 1)
                                                     : x + n.shift;
                                samples[k] = n.row[sx];
                            }
                            const float value = samples[0] * before[vertical] +
                                                samples[2] * (1.0f - before[vertical]);
                            const float change = std::abs(samples[0] - samples[2]) +
                                0.5f * (std::abs(samples[1] - samples[0]) + std::abs(samples[3] - samples[2])) +
                                2.0f * std::abs(values[p][i] - samples[4]);
                            const float weight = 1.0f / (change + 1.0f);
                            total += value * weight;
                            weights += weight;
                        }
                        output[p][i] = total / weights;
                    };
                    pixel(0, true);
                    for (py::ssize_t x = 1; x < width - 1; ++x) pixel(x, false);
                    if (width > 1) pixel(width - 1, true);
                }
            }
        };
        const int count = preview_threads(height * width * 16);
        const py::ssize_t step = (height + count - 1) / count;
        std::vector<std::thread> pool;
        for (int t = 1; t < count; ++t)
            pool.emplace_back(work, std::min(height, t * step), std::min(height, (t + 1) * step));
        work(0, std::min(height, step));
        for (auto& thread : pool) thread.join();
    }
    return out;
}

template<bool SkipZero>
float remosaic_interpolate(const float* grid, const float* guide, float here, size_t offset,
                           const float* distances, const size_t* origins) {
    float total = 0.0f, weights = 0.0f;
    for (int corner = 0; corner < 4; ++corner) {
        if constexpr (SkipZero) {
            if (distances[corner] == 0.0f) continue;
        }
        const size_t j = origins[corner] + offset;
        const float near = guide[j];
        const float spread = 0.05f * (std::abs(near) + std::abs(here)) + 8.0f;
        const float delta = (near - here) / spread;
        const float weight = distances[corner] / (1.0f + delta * delta);
        total += grid[j] * weight;
        weights += weight;
    }
    return total / weights;
}

// Fuse the existing float32 Quad Bayer colour-difference interpolation. Keep the four-corner
// accumulation order and every arithmetic step: -ffp-contract=off prevents fused multiply-adds.
// The caller supplies edge-padded cell grids and one contiguous photosite phase, so no full-sized
// temporary arrays are needed for weights, spreads, products or accumulators.
py::array_t<float> remosaic_difference(const py::array_t<float, py::array::c_style>& grid,
                                      const py::array_t<float, py::array::c_style>& guide,
                                      const py::array_t<float, py::array::c_style>& here,
                                      int ty, int tx, double fy, double fx) {
    if (here.ndim() != 2 || here.shape(0) == 0 || here.shape(1) == 0 ||
        grid.ndim() != 2 || guide.ndim() != 2 ||
        grid.shape(0) != here.shape(0) + 2 || grid.shape(1) != here.shape(1) + 2 ||
        guide.shape(0) != grid.shape(0) || guide.shape(1) != grid.shape(1))
        throw py::value_error("remosaic grids must have a one-cell border around the photosite phase");
    if (ty < -1 || ty > 0 || tx < -1 || tx > 0 ||
        !(fy >= 0 && fy <= 1) || !(fx >= 0 && fx <= 1))
        throw py::value_error("remosaic corner offsets or fractions out of range");
    const size_t height = here.shape(0), width = here.shape(1), stride = grid.shape(1);
    const float* differences = grid.data();
    const float* greens = guide.data();
    const float* target = here.data();
    py::array_t<float> out({height, width});
    float* dst = out.mutable_data();
    const float distances[4] = {
        static_cast<float>((1 - fy) * (1 - fx)), static_cast<float>((1 - fy) * fx),
        static_cast<float>(fy * (1 - fx)), static_cast<float>(fy * fx)};
    const size_t origins[4] = {
        size_t(1 + ty) * stride + size_t(1 + tx), size_t(1 + ty) * stride + size_t(2 + tx),
        size_t(2 + ty) * stride + size_t(1 + tx), size_t(2 + ty) * stride + size_t(2 + tx)};
    {
        py::gil_scoped_release release;
        for (size_t y = 0; y < height; ++y) {
            for (size_t x = 0; x < width; ++x) {
                const size_t i = y * width + x;
                dst[i] = remosaic_interpolate<true>(differences, greens, target[i], y * stride + x,
                                                   distances, origins);
            }
        }
    }
    return out;
}

// Full-resolution remosaic keeps phase and green buffers, but interpolates colour differences
// straight into the final Bayer image. This avoids twelve intermediate difference images and the
// Python phase-conversion/refinement/rounding passes. Operation order is the original float32 one.
py::array_t<uint16_t> remosaic(const py::array_t<uint16_t, py::array::c_style>& raw,
                              const std::string& pattern,
                              const py::array_t<float, py::array::c_style | py::array::forcecast>& black) {
    if (pattern != "rggb" && pattern != "bggr" && pattern != "grbg" && pattern != "gbrg")
        throw py::value_error("unsupported Bayer pattern: " + pattern);
    if (raw.ndim() != 2 || raw.shape(0) < 8 || raw.shape(1) < 8 ||
        raw.shape(0) % 4 || raw.shape(1) % 4)
        throw py::value_error("a Quad Bayer frame needs whole 4x4 tiles, at least 8x8");
    if (black.ndim() != 1 || black.size() != 4)
        throw py::value_error("black levels must contain four sites in pattern order");
    const size_t height = raw.shape(0) / 4, width = raw.shape(1) / 4, pixels = height * width;
    const size_t stride = width + 2;
    const uint16_t* input = raw.data();
    const float* blacks = black.data();
    py::list phases;
    std::array<float*, 16> values;
    int green_mask = 0;
    for (int site = 0; site < 4; ++site)
        if (pattern[site] == 'g') green_mask |= 1 << site;
    for (int p = 0; p < 16; ++p) {
        py::array_t<float> phase({height, width});
        values[p] = phase.mutable_data();
        phases.append(phase);
    }
    // Each stage partitions tile rows: all writes cover disjoint full-width Bayer rows.
    auto parallel = [&](auto work) {
        const int count = preview_threads(pixels * 16);
        const size_t step = (height + count - 1) / count;
        std::vector<std::thread> pool;
        for (int t = 1; t < count; ++t)
            pool.emplace_back(work, std::min(height, t * step), std::min(height, (t + 1) * step));
        work(0, std::min(height, step));
        for (auto& thread : pool) thread.join();
    };
    {
        py::gil_scoped_release release;
        parallel([&](size_t first, size_t last) {
            for (size_t y = first; y < last; ++y)
                for (int py = 0; py < 4; ++py)
                    for (size_t x = 0; x < width; ++x)
                        for (int px = 0; px < 4; ++px)
                            values[py * 4 + px][y * width + x] =
                                float(input[(4 * y + py) * (4 * width) + 4 * x + px]) -
                                blacks[2 * (py >> 1) + (px >> 1)];
        });
    }
    py::list green_arrays = remosaic_green(phases, green_mask);
    std::array<const float*, 16> greens;
    for (int p = 0; p < 16; ++p)
        greens[p] = py::cast<py::array_t<float, py::array::c_style>>(green_arrays[p]).data();
    struct Colour {
        std::vector<float> grid, guide;
        std::array<std::array<float, 4>, 16> distances;
        std::array<std::array<size_t, 4>, 16> origins;
        std::array<int, 4> cells;
    };
    std::array<Colour, 2> colours;
    std::array<int, 4> colour_for_site;
    colour_for_site.fill(-1);
    for (int c = 0; c < 2; ++c) {
        const int site = static_cast<int>(pattern.find(c == 0 ? 'r' : 'b'));
        colour_for_site[site] = c;
        const int a = site / 2, b = site % 2;
        auto& colour = colours[c];
        colour.grid.resize((height + 2) * stride);
        colour.guide.resize((height + 2) * stride);
        colour.cells = {8 * a + 2 * b, 8 * a + 2 * b + 1, 8 * a + 2 * b + 4, 8 * a + 2 * b + 5};
        for (int p = 0; p < 16; ++p) {
            const double y = ((p / 4) - (2 * a + 0.5)) / 4;
            const double x = ((p % 4) - (2 * b + 0.5)) / 4;
            const int ty = int(std::floor(y)), tx = int(std::floor(x));
            const double fy = y - ty, fx = x - tx;
            colour.distances[p] = {float((1 - fy) * (1 - fx)), float((1 - fy) * fx),
                                   float(fy * (1 - fx)), float(fy * fx)};
            colour.origins[p] = {size_t(1 + ty) * stride + size_t(1 + tx),
                                 size_t(1 + ty) * stride + size_t(2 + tx),
                                 size_t(2 + ty) * stride + size_t(1 + tx),
                                 size_t(2 + ty) * stride + size_t(2 + tx)};
        }
    }
    py::array_t<uint16_t> out({height * 4, width * 4});
    uint16_t* output = out.mutable_data();
    {
        py::gil_scoped_release release;
        parallel([&](size_t first, size_t last) {
            for (auto& colour : colours)
                for (size_t y = first; y < last; ++y)
                    for (size_t x = 0; x < width; ++x) {
                        const size_t i = y * width + x, j = (y + 1) * stride + x + 1;
                        float difference = 0.0f, guide = 0.0f;
                        for (int cell : colour.cells) {
                            difference += values[cell][i] - greens[cell][i];
                            guide += greens[cell][i];
                        }
                        colour.grid[j] = difference * 0.25f;
                        colour.guide[j] = guide * 0.25f;
                    }
        });
        // Repeat the phase-grid edge exactly as np.pad(..., mode="edge") does.
        for (auto& colour : colours) {
            for (auto* grid : {colour.grid.data(), colour.guide.data()}) {
                for (size_t y = 1; y <= height; ++y) {
                    grid[y * stride] = grid[y * stride + 1];
                    grid[y * stride + width + 1] = grid[y * stride + width];
                }
                std::copy_n(grid + stride, stride, grid);
                std::copy_n(grid + height * stride, stride, grid + (height + 1) * stride);
            }
        }
        parallel([&](size_t first, size_t last) {
            for (size_t y = first; y < last; ++y)
                for (int p = 0; p < 16; ++p) {
                    const int py = p / 4, px = p % 4;
                    const int recorded = 2 * (py >> 1) + (px >> 1);
                    const int wanted = 2 * (py & 1) + (px & 1);
                    for (size_t x = 0; x < width; ++x) {
                        const size_t i = y * width + x;
                        auto difference = [&](int site) {
                            const auto& colour = colours[colour_for_site[site]];
                            return remosaic_interpolate<false>(colour.grid.data(), colour.guide.data(),
                                greens[p][i], y * stride + x, colour.distances[p].data(), colour.origins[p].data());
                        };
                        float value = values[p][i];
                        if (pattern[recorded] != pattern[wanted]) {
                            value = greens[p][i];
                            if (pattern[recorded] != 'g') value = values[p][i] - difference(recorded);
                            if (pattern[wanted] != 'g') value = value + difference(wanted);
                        }
                        const float rounded = std::nearbyint(value + blacks[wanted]);
                        output[(y * 4 + py) * (width * 4) + x * 4 + px] =
                            static_cast<uint16_t>(std::clamp(rounded, 0.0f, 65535.0f));
                    }
                }
        });
    }
    return out;
}

// Average each of the four Bayer sites independently. Supported factors have power-of-two sample
// counts, so quotient/remainder rounding gives exactly np.rint(uint32_sum / factor**2), including
// halfway-to-even cases, without float64 intermediates or repeated strided NumPy additions.
py::array_t<uint16_t> reduce_cfa(const py::array_t<uint16_t, py::array::c_style>& raw, int factor) {
    if (factor != 2 && factor != 4 && factor != 8)
        throw py::value_error("native CFA reduction supports factors 2, 4 and 8");
    const size_t block = 2 * factor;
    if (raw.ndim() != 2 || size_t(raw.shape(0)) < block || size_t(raw.shape(1)) < block)
        throw py::value_error("the CFA is too small for this reduction factor");
    const size_t height = raw.shape(0) / block, width = raw.shape(1) / block;
    const size_t source_width = raw.shape(1), output_width = 2 * width;
    const uint16_t* source = raw.data();
    py::array_t<uint16_t> out({2 * height, output_width});
    uint16_t* output = out.mutable_data();
    const unsigned shift = factor == 2 ? 2 : factor == 4 ? 4 : 6;
    const uint32_t samples = factor * factor;
    auto rounded = [&](uint32_t sum) {
        uint32_t value = sum >> shift, remainder = sum & (samples - 1);
        value += remainder > samples / 2 || (remainder == samples / 2 && (value & 1));
        return static_cast<uint16_t>(value);
    };
    {
        py::gil_scoped_release release;
        auto work = [&](size_t first, size_t last) {
            for (size_t y = first; y < last; ++y)
                for (size_t x = 0; x < width; ++x) {
                    uint32_t sums[4] = {};
                    for (int dy = 0; dy < factor; ++dy) {
                        const uint16_t* row = source + (y * block + 2 * dy) * source_width + x * block;
                        for (int dx = 0; dx < factor; ++dx) {
                            sums[0] += row[2 * dx];
                            sums[1] += row[2 * dx + 1];
                            sums[2] += row[source_width + 2 * dx];
                            sums[3] += row[source_width + 2 * dx + 1];
                        }
                    }
                    output[2 * y * output_width + 2 * x] = rounded(sums[0]);
                    output[2 * y * output_width + 2 * x + 1] = rounded(sums[1]);
                    output[(2 * y + 1) * output_width + 2 * x] = rounded(sums[2]);
                    output[(2 * y + 1) * output_width + 2 * x + 1] = rounded(sums[3]);
                }
        };
        const int count = preview_threads(size_t(raw.shape(0)) * source_width);
        const size_t step = (height + count - 1) / count;
        std::vector<std::thread> pool;
        for (int t = 1; t < count; ++t)
            pool.emplace_back(work, std::min(height, t * step), std::min(height, (t + 1) * step));
        work(0, std::min(height, step));
        for (auto& thread : pool) thread.join();
    }
    return out;
}
}  // namespace

PYBIND11_MODULE(_raw, m) {
    m.doc() = "MotionCam raw frame decompression, and a look table applied to preview frames";
    m.def("remosaic", &remosaic, py::arg("raw"), py::arg("pattern"), py::arg("black"));
    m.def("reduce_cfa", &reduce_cfa, py::arg("raw"), py::arg("factor"));
    m.def("remosaic_green", &remosaic_green, py::arg("phases"), py::arg("green_mask"));
    m.def("remosaic_difference", &remosaic_difference, py::arg("grid"), py::arg("guide"), py::arg("here"),
          py::arg("ty"), py::arg("tx"), py::arg("fy"), py::arg("fx"));
    m.def("neutral_preview", &neutral_preview, py::arg("rgb"), py::arg("mask"));
    m.def("recover_preview", [](py::array_t<float, py::array::c_style> rgb,
                               const py::array_t<uint8_t, py::array::c_style>& masks) {
        if (rgb.ndim() != 3 || rgb.shape(2) != 3 || masks.ndim() != 3 ||
            masks.shape(0) != rgb.shape(0) || masks.shape(1) != rgb.shape(1) || masks.shape(2) != 3)
            throw py::value_error("preview masks must match the RGB frame");
        auto* dst = rgb.mutable_data();
        const auto* clipped = masks.data();
        const size_t width = rgb.shape(1), height = rgb.shape(0);
        py::gil_scoped_release release;
        recover_preview(dst, clipped, width, height);
    }, py::arg("rgb"), py::arg("masks"));
    m.def("encode_preview", &encode_preview, py::arg("linear"), py::arg("stops"));
    m.def("develop_preview", &develop_preview, py::arg("raw"), py::arg("black"), py::arg("gain"),
          py::arg("cfa"), py::arg("step") = 2, py::arg("rotate") = false,
          py::arg("max_width") = 65536, py::arg("white") = 65535.0f, py::arg("recover") = false);
    m.def("bake_cfa", &bake_cfa, py::arg("raw"), py::arg("black"), py::arg("gains"), py::arg("gain") = 1.0f,
          py::arg("white") = 65535.0f,
          "Lens shading and an exposure gain baked into a (height, width) CFA, as bake_shading computes them.");
    m.def("decode", &decode, py::arg("data"), py::arg("width"), py::arg("height"), py::arg("compression_type"),
          "Decompress one MotionCam raw frame into a (height, width) uint16 array.");
    m.def("encode_ljpeg", &encode_ljpeg, py::arg("tile"), py::arg("bits"),
          "Encode a (height, width) CFA tile as lossless JPEG (two interleaved components, predictor 1).");
    m.def("decode_ljpeg", &decode_ljpeg, py::arg("data"),
          "Decode a lossless JPEG stream into a (height, width * components) uint16 array, components interleaved.");
    using FloatArray = py::array_t<float, py::array::c_style | py::array::forcecast>;
    m.def("apply_preview", [](const FloatArray& frame, const FloatArray& table, float stops) {
        return apply_table(frame, table, true, true, stops);
    }, py::arg("frame"), py::arg("table"), py::arg("stops"));
    m.def("apply_look", [](const FloatArray& frame, const FloatArray& table, bool bgr) {
        return apply_table(frame, table, bgr, false, 0.0f);
    }, py::arg("frame"), py::arg("table"), py::arg("bgr") = false,
          "A (height, width, 3) frame of 0 to 1 values through a (size**3, 3) table, tetrahedrally, as uint8.");
}
