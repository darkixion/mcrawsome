// Shared by the native CPU preview and OpenCL. Keep the pixel maths here.
#ifdef __OPENCL_VERSION__
#define MC_GLOBAL __global
#define MC_U16 ushort
#define MC_U8 uchar
#define MC_U32 uint
#define MC_BITS as_uint
#define MC_FLOAT as_float
#define MC_MIN min
#define MC_MAX max
#else
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#define MC_GLOBAL
#define MC_U16 uint16_t
#define MC_U8 uint8_t
#define MC_U32 uint32_t
#define MC_MIN std::min
#define MC_MAX std::max
using std::floor;
using std::isfinite;
using std::isnan;
namespace mcrawsome {
inline uint32_t MC_BITS(float x) { uint32_t v; std::memcpy(&v, &x, 4); return v; }
inline float MC_FLOAT(uint32_t x) { float v; std::memcpy(&v, &x, 4); return v; }
#endif

inline float PreviewCode(float x) {
    if (x <= 0.0078125f) return 10.5402377416545f * x + 0.0729055341958355f;
    if (!isfinite(x)) return x;
    MC_U32 bits;
    bits = MC_BITS(x);
    const int exponent = (int)((bits >> 23) & 255) - 127;
    bits = (bits & 0x7fffff) | 0x3f800000;
    float mantissa;
    mantissa = MC_FLOAT(bits);
    const float t = (mantissa - 1) / (mantissa + 1), t2 = t * t;
    const float series = t * (1 + t2 * (1.0f/3 + t2 * (1.0f/5 + t2 * (1.0f/7 +
                         t2 * (1.0f/9 + t2 * (1.0f/11))))));
    return ((float)(exponent) + 2.8853900817779268f * series + 9.72f) / 17.52f;
}

inline MC_GLOBAL const float* PreviewAt(MC_GLOBAL const float* table, int size, int r, int g, int b) {
    return table + 3 * ((b * size + g) * size + r);
}
inline void PreviewLookup(const float* in, float* out,
                        MC_GLOBAL const float* table, int size, int linear, float gain) {
    const float top = (float)(size - 1);
    float p[3];
    int i0[3];
    float f[3];
    for (int c = 0; c < 3; ++c) {
        float v = in[c];
        if (linear) {
            const float x = v * gain;
            v = PreviewCode(x);
        }
        v = isnan(v) ? 0.0f : MC_MIN(MC_MAX(v, 0.0f), 1.0f);
        p[c] = v * top;
        i0[c] = MC_MIN((int)(floor(p[c])), size - 2);
        f[c] = p[c] - (float)(i0[c]);
    }
    const int r = i0[0], g = i0[1], b = i0[2];
    const float fr = f[0], fg = f[1], fb = f[2];
    MC_GLOBAL const float* c000 = PreviewAt(table, size, r, g, b);
    MC_GLOBAL const float* c111 = PreviewAt(table, size, r + 1, g + 1, b + 1);
    // The two corners between c000 and c111, and the weights, for each of the six tetrahedra.
    MC_GLOBAL const float *ca, *cb;
    float w1, w2, w3;  // weights along c000 -> ca -> cb -> c111
    if (fr > fg) {
        if (fg > fb) {
            ca = PreviewAt(table, size, r + 1, g, b), cb = PreviewAt(table, size, r + 1, g + 1, b), w1 = fr, w2 = fg, w3 = fb;
        } else if (fr > fb) {
            ca = PreviewAt(table, size, r + 1, g, b), cb = PreviewAt(table, size, r + 1, g, b + 1), w1 = fr, w2 = fb, w3 = fg;
        } else {
            ca = PreviewAt(table, size, r, g, b + 1), cb = PreviewAt(table, size, r + 1, g, b + 1), w1 = fb, w2 = fr, w3 = fg;
        }
    } else if (fb > fg) {
        ca = PreviewAt(table, size, r, g, b + 1), cb = PreviewAt(table, size, r, g + 1, b + 1), w1 = fb, w2 = fg, w3 = fr;
    } else if (fb > fr) {
        ca = PreviewAt(table, size, r, g + 1, b), cb = PreviewAt(table, size, r, g + 1, b + 1), w1 = fg, w2 = fb, w3 = fr;
    } else {
        ca = PreviewAt(table, size, r, g + 1, b), cb = PreviewAt(table, size, r + 1, g + 1, b), w1 = fg, w2 = fr, w3 = fb;
    }
    for (int c = 0; c < 3; ++c) {
        const float v = c000[c] + w1 * (ca[c] - c000[c]) + w2 * (cb[c] - ca[c]) + w3 * (c111[c] - cb[c]);
        const float clipped = MC_MIN(MC_MAX(v, 0.0f), 1.0f);
        out[c] = clipped;
    }
}
inline void PreviewLook(MC_GLOBAL const float* in, MC_GLOBAL MC_U8* out,
                        MC_GLOBAL const float* table, int size, int bgr, int linear, float gain) {
    float source[3] = {in[0], in[1], in[2]}, values[3];
    PreviewLookup(source, values, table, size, linear, gain);
    for (int c=0; c<3; ++c) out[bgr ? 2-c : c] = (MC_U8)(values[c]*255.0f+0.5f);
}
inline void PreviewReduce(MC_GLOBAL const MC_U16* raw, MC_GLOBAL const float* black,
                          MC_GLOBAL const float* gain, const int* offsets, int stride, int step,
                          int sx, int sy, float white, MC_GLOBAL float* rgb, MC_GLOBAL MC_U8* over) {
    float sum[3] = {0, 0, 0};
    MC_U8 clipped[3] = {0, 0, 0};
    for (int dy = 0; dy < step; dy += 2) {
        for (int dx = 0; dx < step; dx += 2) {
            int base = (sy * step + dy) * stride + sx * step + dx;
            float sites[4];
            for (int k = 0; k < 4; ++k) {
                int i = base + offsets[k];
                sites[k] = (raw[i] - black[i]) * gain[i];
                clipped[k == 0 ? 0 : k == 3 ? 2 : 1] |= raw[i] >= white - 2.0f;
            }
            sum[0] += sites[0];
            sum[1] += (sites[1] + sites[2]) * 0.5f;
            sum[2] += sites[3];
        }
    }
    for (int c = 0; c < 3; ++c) {
        rgb[c] = MC_MAX(0.0f, sum[c] * (4.0f / (step * step)));
        over[c] = clipped[c];
    }
}
#ifndef __OPENCL_VERSION__
}  // namespace mcrawsome
#endif
