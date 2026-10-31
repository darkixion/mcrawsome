#pragma OPENCL FP_CONTRACT OFF

__kernel void reduce(__global const ushort* raw, __global const float* black,
                     __global const float* gain, __global float* rgb, __global uchar* masks,
                     int stride, int source_width, int source_height, int width, int height,
                     int step, int rotate, int red, int green0, int green1, int blue, float white) {
    int x = get_global_id(0), y = get_global_id(1);
    int offsets[4] = {red / 2 * stride + red % 2, green0 / 2 * stride + green0 % 2,
                      green1 / 2 * stride + green1 % 2, blue / 2 * stride + blue % 2};
    int pixel = rotate ? height * width - 1 - (y * width + x) : y * width + x;
    PreviewReduce(raw, black, gain, offsets, stride, step, x * source_width / width,
                  y * source_height / height, white, rgb + 3 * pixel, masks + 3 * pixel);
}
__kernel void transform(__global const float* rgb, __global const float* matrix, __global float* ap1,
                        int rotate, float gain, float strength) {
    int w = get_global_size(0), h = get_global_size(1);
    int p = get_global_id(1) * w + get_global_id(0);
    float v[3] = {rgb[3*p], rgb[3*p+1], rgb[3*p+2]};
    // Match the recovery blend's rounding even when there are no clipped sites.
    if (strength < 1.0f)
        for (int c = 0; c < 3; ++c) v[c] = (1.0f-strength) * v[c] + strength * v[c];
    for (int c = 0; c < 3; ++c)
        ap1[3*(rotate ? w*h-1-p : p)+c] = gain * (v[0] * matrix[3*c] + v[1] * matrix[3*c+1] + v[2] * matrix[3*c+2]);
}
__kernel void finish(__global const float* linear, __global const float* table,
                     __global uchar* output, int size, float gain) {
    int p = get_global_id(1) * get_global_size(0) + get_global_id(0);
    PreviewLook(linear + 3*p, output + 3*p, table, size, 1, 1, gain);
}

// Pack the three predicates once, before scanning neighbouring pixels.
__kernel void highlight_flags(__global const uchar* masks, __global const uchar* sites,
                               __global uchar* flags, int full_size) {
    int p = get_global_id(1) * get_global_size(0) + get_global_id(0);
    uchar any = masks[3*p] | masks[3*p+1] | masks[3*p+2];
    uchar all = masks[3*p] & masks[3*p+1] & masks[3*p+2];
    flags[p] = any | (all << 1) | ((full_size ? sites[p] : any) << 2);
}

// Separable dilation finds the same 17-pixel reference ring as the CPU preview.
int highlight_active(__global const uchar* tiles, int stride, int x, int y) {
    return !stride || tiles[(y/16)*stride+x/16];
}
__kernel void highlight_horizontal(__global const uchar* flags, __global uchar* ring,
                                    __global uchar* white, __global const uchar* tiles,
                                    int full_size, int tile_stride) {
    int x = get_global_id(0), y = get_global_id(1), w = get_global_size(0);
    if (!highlight_active(tiles,tile_stride,x,y)) {
        ring[y*w+x] = 0; white[y*w+x] = 0; return;
    }
    uchar any = 0, full = 0;
    #pragma unroll
    for (int dx = -8; dx <= 8; ++dx) {
        if (x + dx < 0 || x + dx >= w) continue;
        uchar value = flags[y*w + x + dx];
        any |= value;
        if (abs(dx) <= (full_size ? 2 : 1)) full |= value;
    }
    ring[y*w+x] = any & 1;
    white[y*w+x] = full >> 1;
}

__kernel void highlight_references(__global const float* rgb, __global const uchar* masks,
                                    __global const uchar* ring, __global const uchar* white,
                                    __global float* seed, __global float* off_seed, __global float* values,
                                    volatile __global uint* count, __global const uchar* tiles,
                                    int full_size, int tile_stride) {
    int x = get_global_id(0), y = get_global_id(1);
    int w = get_global_size(0), h = get_global_size(1), p = y*w+x;
    uchar near = 0, full = 0;
    int active = highlight_active(tiles,tile_stride,x,y);
    if (active) {
        for (int dy = -8; dy <= 8; ++dy) {
            if (y + dy < 0 || y + dy >= h) continue;
            near |= ring[(y+dy)*w+x];
            if (abs(dy) <= (full_size ? 2 : 1)) full |= white[(y+dy)*w+x];
        }
    }
    seed[p] = (full & 1) ? 1.0f : 0.0f;
    off_seed[p] = (full & 2) ? 1.0f : 0.0f;
    int reference = near && !(masks[3*p] || masks[3*p+1] || masks[3*p+2]);
    __local uint group_count, group_start;
    if (get_local_id(0) == 0 && get_local_id(1) == 0) group_count = 0;
    barrier(CLK_LOCAL_MEM_FENCE);
    uint offset = reference ? atomic_inc(&group_count) : 0;
    barrier(CLK_LOCAL_MEM_FENCE);
    if (get_local_id(0) == 0 && get_local_id(1) == 0)
        group_start = group_count ? atomic_add(count, group_count) : 0;
    barrier(CLK_LOCAL_MEM_FENCE);
    if (!reference) return;
    uint at = group_start + offset;
    for (int c = 0; c < 3; ++c) {
        float opposed = 0.5f * (rgb[3*p+(c+1)%3] + rgb[3*p+(c+2)%3]);
        values[3*at+c] = opposed > 1e-6f ? rgb[3*p+c] / opposed : -1.0f;
    }
}

// Each channel's median of the valid (non-negative) reference ratios, exactly as numpy's: a radix select
// over their bits, which order as non-negative floats do, in passes of 11, 11 and 9 bits, then the middle
// value or the mean of the two middle values. The state per channel is the selected bits so far, the rank
// still wanted among the values that share them, the count of valid values and the count in the last bin
// chosen; then each channel's least key above its lower middle value.
#define MEDIAN_BINS 2048

uint median_key(float v) { return v == 0.0f ? 0u : as_uint(v); }  // -0.0 orders as 0.0

__kernel void median_clear(__global uint* bins, __global uint* state, __global uint* count) {
    int i = get_global_id(0);
    if (i < 3 * MEDIAN_BINS) bins[i] = 0;
    if (i < 15) state[i] = i < 12 ? 0 : 0xffffffffu;
    if (i == 0) count[0] = 0;
}

__kernel __attribute__((reqd_work_group_size(256, 1, 1)))
void median_histogram(__global const float* values, __global const uint* count,
                      __global const uint* state, __global uint* bins, int shift, int width) {
    __local uint group_bins[3 * MEDIAN_BINS];
    for (int i = get_local_id(0); i < 3 * MEDIAN_BINS; i += 256) group_bins[i] = 0;
    barrier(CLK_LOCAL_MEM_FENCE);
    uint n = count[0], mask = (1u << width) - 1;
    uint selected[3] = {state[0], state[4], state[8]};
    for (uint i = get_global_id(0); i < n; i += get_global_size(0)) {
        for (int c = 0; c < 3; ++c) {
            float v = values[3*i+c];
            uint key = median_key(v);
            if (v >= 0.0f && key >> (shift + width) == selected[c])
                atomic_inc(&group_bins[c * MEDIAN_BINS + ((key >> shift) & mask)]);
        }
    }
    barrier(CLK_LOCAL_MEM_FENCE);
    for (int i = get_local_id(0); i < 3 * MEDIAN_BINS; i += 256)
        if (group_bins[i]) atomic_add(&bins[i], group_bins[i]);
}

// One work item per channel: the bin holding the wanted rank narrows the selection; the bins are cleared.
__kernel void median_select(__global uint* bins, __global uint* state, int shift, int width, int first) {
    int c = get_global_id(0);
    __global uint* histogram = bins + c * MEDIAN_BINS;
    __global uint* s = state + 4 * c;
    uint size = 1u << width;
    if (first) {
        uint valid = 0;
        for (uint b = 0; b < size; ++b) valid += histogram[b];
        s[2] = valid;
        s[1] = valid ? (valid - 1) / 2 : 0;
    }
    if (s[2]) {
        uint rank = s[1], below = 0, b = 0;
        while (b + 1 < size && below + histogram[b] <= rank) below += histogram[b++];
        s[0] = (s[0] << width) | b;
        s[1] = rank - below;
        s[3] = histogram[b];
    }
    for (uint b = 0; b < size; ++b) histogram[b] = 0;
}

__kernel __attribute__((reqd_work_group_size(256, 1, 1)))
void median_successor(__global const float* values, __global const uint* count, __global uint* state) {
    __local uint least[3];
    if (get_local_id(0) < 3) least[get_local_id(0)] = 0xffffffffu;
    barrier(CLK_LOCAL_MEM_FENCE);
    uint n = count[0];
    uint middle[3] = {state[0], state[4], state[8]};
    uint mine[3] = {0xffffffffu, 0xffffffffu, 0xffffffffu};
    for (uint i = get_global_id(0); i < n; i += get_global_size(0)) {
        for (int c = 0; c < 3; ++c) {
            float v = values[3*i+c];
            uint key = median_key(v);
            if (v >= 0.0f && key > middle[c] && key < mine[c]) mine[c] = key;
        }
    }
    for (int c = 0; c < 3; ++c)
        if (mine[c] != 0xffffffffu) atomic_min(&least[c], mine[c]);
    barrier(CLK_LOCAL_MEM_FENCE);
    if (get_local_id(0) < 3 && least[get_local_id(0)] != 0xffffffffu)
        atomic_min(&state[12 + get_local_id(0)], least[get_local_id(0)]);
}

__kernel void median_ratios(__global const uint* state, __global float* ratios) {
    int c = get_global_id(0);
    __global const uint* s = state + 4 * c;
    if (!s[2]) { ratios[c] = 1.0f; return; }
    float lower = as_float(s[0]);
    if (s[2] & 1) { ratios[c] = lower; return; }
    // The upper middle value repeats the lower one while values equal to it remain.
    float upper = s[1] + 1 < s[3] ? lower : as_float(state[12 + c]);
    ratios[c] = (lower + upper) * 0.5f;
}

int highlight_border(int p, int n) {
    if (n <= 1) return 0;
    while (p < 0 || p >= n) p = p < 0 ? -p : 2*n-p-2;
    return p;
}

__kernel void highlight_blur(__global const float* seed, __global float* blur,
                             __global const float* weights, __global const uchar* tiles,
                             int radius, int tile_stride) {
    int x = get_global_id(0), y = get_global_id(1), w = get_global_size(0);
    if (!highlight_active(tiles,tile_stride,x,y)) { blur[y*w+x] = 0; return; }
    float v = 0;
    for (int dx = -radius; dx <= radius; ++dx) v += weights[dx+radius] * seed[y*w+highlight_border(x+dx,w)];
    blur[y*w+x] = v;
}

__kernel void highlight_transform(__global const float* rgb, __global const uchar* masks,
                                   __global const float* ratios, __global const float* blur,
                                   __global const float* off_blur,
                                   __global const float* weights, __global const float* matrix,
                                   __global float* ap1, __global const uchar* tiles,
                                   float strength, int radius, int rotate, float gain, int tile_stride) {
    int x = get_global_id(0), y = get_global_id(1);
    int w = get_global_size(0), h = get_global_size(1), p = y*w+x;
    float v[3] = {rgb[3*p], rgb[3*p+1], rgb[3*p+2]};
    if (highlight_active(tiles,tile_stride,x,y)) {
        if (!(masks[3*p] && masks[3*p+1] && masks[3*p+2])) {
            for (int c = 0; c < 3; ++c)
                if (masks[3*p+c]) v[c] = max(v[c], ratios[c] * 0.5f * (rgb[3*p+(c+1)%3] + rgb[3*p+(c+2)%3]));
        }
        float blend = 0;
        for (int dy = -radius; dy <= radius; ++dy) blend += weights[dy+radius] * blur[highlight_border(y+dy,h)*w+x];
        float white = max(1.0f, max(v[0], max(v[1], v[2])));
        for (int c = 0; c < 3; ++c) v[c] = v[c] * (1-blend) + white * blend;
        if (strength < 1.0f) {
            float off = 0;
            for (int dy = -radius; dy <= radius; ++dy)
                off += weights[dy+radius] * off_blur[highlight_border(y+dy,h)*w+x];
            float level = max(1.0f, max(rgb[3*p], max(rgb[3*p+1], rgb[3*p+2])));
            for (int c = 0; c < 3; ++c)
                v[c] = (1.0f-strength) * (rgb[3*p+c] * (1.0f-off) + level * off) + strength * v[c];
        }
    }
    for (int c = 0; c < 3; ++c)
        ap1[3*(rotate ? w*h-1-p : p)+c] = gain * (v[0] * matrix[3*c] + v[1] * matrix[3*c+1] + v[2] * matrix[3*c+2]);
}


int full_reflect(int x, int n) {
    while (x < 0 || x >= n) x = x < 0 ? -x-1 : 2*n-x-1;
    return x;
}
float full_sample(__global const float* mosaic, int x, int y, int w, int h, int dx, int dy) {
    if (x >= 0 && x < w && y >= 0 && y < h) return mosaic[y*w+x];
    x = highlight_border(full_reflect(x+dx, w+2*dx)-dx, w);
    y = highlight_border(full_reflect(y+dy, h+2*dy)-dy, h);
    return mosaic[y*w+x];
}
__kernel void normalise(__global const ushort* raw, __global const float* black,
                         __global const float* gain, __global float* mosaic,
                         __global uchar* sites, float white) {
    int p = get_global_id(1)*get_global_size(0)+get_global_id(0);
    mosaic[p] = ((float)raw[p]-black[p])*gain[p];
    sites[p] = raw[p] >= white-2.0f;
}
__kernel void malvar(__global const float* mosaic, __global const uchar* sites,
                      __global float* rgb, __global uchar* masks, __global uchar* flags, int red) {
    int x=get_global_id(0), y=get_global_id(1), w=get_global_size(0), h=get_global_size(1);
    int dx=red%2, dy=red/2, p=y*w+x;
    int row=(y+dy)%2, col=(x+dx)%2;
    int colour=row==col ? (row ? 2 : 0) : 1;
    // The non-zero taps of the four filters in develop/demosaic.py, in row order.
    #define SAMPLE(kx,ky) full_sample(mosaic,x+(kx),y+(ky),w,h,dx,dy)
    float n2=SAMPLE(0,-2), nw=SAMPLE(-1,-1), n=SAMPLE(0,-1), ne=SAMPLE(1,-1);
    float w2=SAMPLE(-2,0), west=SAMPLE(-1,0), centre=mosaic[p], east=SAMPLE(1,0), e2=SAMPLE(2,0);
    float sw=SAMPLE(-1,1), south=SAMPLE(0,1), se=SAMPLE(1,1), s2=SAMPLE(0,2);
    #undef SAMPLE
    float va, vb;
    if (colour==1) {
        float across = n2*0.0625f - nw*0.125f - ne*0.125f - w2*0.125f + west*0.5f +
                       centre*0.625f + east*0.5f - e2*0.125f - sw*0.125f - se*0.125f + s2*0.0625f;
        float down = -n2*0.125f - nw*0.125f + n*0.5f - ne*0.125f + w2*0.0625f +
                     centre*0.625f + e2*0.0625f - sw*0.125f + south*0.5f - se*0.125f - s2*0.125f;
        va = row ? down : across;
        vb = row ? across : down;
    } else {
        va = -n2*0.125f + n*0.25f - w2*0.125f + west*0.25f + centre*0.5f +
             east*0.25f - e2*0.125f + south*0.25f - s2*0.125f;
        vb = -n2*0.1875f + nw*0.25f + ne*0.25f - w2*0.1875f + centre*0.75f -
             e2*0.1875f + sw*0.25f + se*0.25f - s2*0.1875f;
    }
    float values[3];
    if (colour==1) { values[0]=va; values[1]=mosaic[p]; values[2]=vb; }
    else { values[colour]=mosaic[p]; values[1]=va; values[2-colour]=vb; }
    uchar over[3] = {0, 0, 0};
    #pragma unroll
    for (int my=-1; my<=1; ++my) {
        #pragma unroll
        for (int mx=-1; mx<=1; ++mx) {
            int sx=x+mx, sy=y+my;
            if (sx<0 || sy<0 || sx>=w || sy>=h) continue;
            int r=(sy+dy)%2, k=(sx+dx)%2;
            int channel=r==k ? (r ? 2 : 0) : 1;
            over[channel] |= sites[sy*w+sx];
        }
    }
    for (int c=0; c<3; ++c) {
        rgb[3*p+c]=max(0.0f,values[c]);
        masks[3*p+c]=over[c];
    }
    flags[p] = (over[0] | over[1] | over[2]) | ((over[0] & over[1] & over[2]) << 1) | (sites[p] << 2);
}
