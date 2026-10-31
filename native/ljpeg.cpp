// Lossless JPEG encoder: ITU T.81 Annex H (lossless mode), Annex K.2 (optimal Huffman tables),
// Annex C (code assignment). Written for DNG compression 7 tiles; no restart intervals. The decoder
// (Annex H, F.2.2.3 Huffman decoding) reads what DNG writers produce, restarts included.
#include "ljpeg.hpp"

#include <algorithm>
#include <array>
#include <stdexcept>
#include <string>

namespace mcrawsome {
namespace {

constexpr int kSymbols = 17;  // difference categories 0..16

int Category(int diff) {
    if (diff == 0) return 0;
    if (diff == 32768) return 16;  // the one difference with no extra bits
    int magnitude = diff < 0 ? -diff : diff;
    int bits = 0;
    while (magnitude) {
        ++bits;
        magnitude >>= 1;
    }
    return bits;
}

// The difference modulo 2^16, in -32767..32768 as T.81 H.1.2.1 defines it.
int Difference(int value, int prediction) {
    int diff = (value - prediction) & 0xFFFF;
    return diff > 32768 ? diff - 65536 : diff;
}

struct Table {
    std::array<uint8_t, 16> counts{};  // codes of each length 1..16
    std::vector<uint8_t> symbols;      // in order of increasing code length
    std::array<uint16_t, kSymbols> code{};
    std::array<uint8_t, kSymbols> size{};
};

// T.81 Annex K.2: code lengths from frequencies, limited to 16 bits, with the all-ones code reserved.
Table BuildTable(const std::array<long, kSymbols>& counts) {
    std::array<long, kSymbols + 1> freq{};
    std::copy(counts.begin(), counts.end(), freq.begin());
    freq[kSymbols] = 1;  // reserved symbol, so no real code is all ones
    std::array<int, kSymbols + 1> codesize{};
    std::array<int, kSymbols + 1> others;
    others.fill(-1);
    while (true) {
        int v1 = -1, v2 = -1;
        for (int i = 0; i <= kSymbols; ++i) {  // least frequency; ties go to the larger index
            if (freq[i] > 0 && (v1 < 0 || freq[i] <= freq[v1])) v1 = i;
        }
        for (int i = 0; i <= kSymbols; ++i) {
            if (i != v1 && freq[i] > 0 && (v2 < 0 || freq[i] <= freq[v2])) v2 = i;
        }
        if (v2 < 0) break;
        freq[v1] += freq[v2];
        freq[v2] = 0;
        ++codesize[v1];
        while (others[v1] >= 0) {
            v1 = others[v1];
            ++codesize[v1];
        }
        others[v1] = v2;
        ++codesize[v2];
        while (others[v2] >= 0) {
            v2 = others[v2];
            ++codesize[v2];
        }
    }
    std::array<int, 33> bits{};
    for (int i = 0; i <= kSymbols; ++i) {
        if (codesize[i]) ++bits[codesize[i]];
    }
    for (int i = 32; i > 16; --i) {  // Figure K.3: limit lengths to 16 bits
        while (bits[i] > 0) {
            int j = i - 2;
            while (bits[j] == 0) --j;
            bits[i] -= 2;
            ++bits[i - 1];
            bits[j + 1] += 2;
            --bits[j];
        }
    }
    int longest = 16;
    while (bits[longest] == 0) --longest;
    --bits[longest];  // drop the reserved symbol's code
    Table table;
    for (int i = 1; i <= 16; ++i) table.counts[i - 1] = static_cast<uint8_t>(bits[i]);
    for (int length = 1; length <= 32; ++length) {  // Figure K.4: symbols sorted by code length
        for (int i = 0; i < kSymbols; ++i) {
            if (codesize[i] == length) table.symbols.push_back(static_cast<uint8_t>(i));
        }
    }
    // Annex C: canonical codes in symbol order of the table.
    uint16_t code = 0;
    size_t k = 0;
    for (int length = 1; length <= 16; ++length) {
        for (int n = 0; n < table.counts[length - 1]; ++n, ++k) {
            const uint8_t symbol = table.symbols[k];
            table.code[symbol] = code++;
            table.size[symbol] = static_cast<uint8_t>(length);
        }
        code <<= 1;
    }
    table.symbols.resize(k);
    return table;
}

class BitWriter {
   public:
    explicit BitWriter(std::vector<uint8_t>& out) : out_(out) {}
    void Put(uint32_t value, int count) {
        for (int i = count - 1; i >= 0; --i) {
            current_ = static_cast<uint8_t>((current_ << 1) | ((value >> i) & 1));
            if (++filled_ == 8) Flush();
        }
    }
    void Finish() {  // pad the last byte with 1 bits (T.81 F.1.2.3)
        while (filled_ != 0) Put(1, 1);
    }

   private:
    void Flush() {
        out_.push_back(current_);
        if (current_ == 0xFF) out_.push_back(0x00);  // byte stuffing
        current_ = 0;
        filled_ = 0;
    }
    std::vector<uint8_t>& out_;
    uint8_t current_ = 0;
    int filled_ = 0;
};

void Marker(std::vector<uint8_t>& out, uint8_t code) {
    out.push_back(0xFF);
    out.push_back(code);
}

void Word(std::vector<uint8_t>& out, int value) {
    out.push_back(static_cast<uint8_t>(value >> 8));
    out.push_back(static_cast<uint8_t>(value & 0xFF));
}

}  // namespace

std::vector<uint8_t> EncodeLosslessJpeg(const uint16_t* tile, int width, int height, int bits) {
    if (width <= 0 || height <= 0 || width % 2 != 0) throw std::invalid_argument("tile width must be even");
    if (bits < 2 || bits > 16) throw std::invalid_argument("precision must be 2 to 16 bits");
    const int columns = width / 2;  // samples per component per row
    std::vector<int> diffs(static_cast<size_t>(width) * height);
    std::array<long, kSymbols> counts{};
    const int initial = 1 << (bits - 1);
    const int top = (1 << bits) - 1;
    for (int y = 0; y < height; ++y) {
        const uint16_t* row = tile + static_cast<size_t>(y) * width;
        const uint16_t* above = row - width;
        for (int x = 0; x < width; ++x) {
            if (row[x] > top) throw std::invalid_argument("a sample is above the declared precision");
            int prediction;
            if (x < 2) {  // first sample of each component on the row
                prediction = y == 0 ? initial : above[x];
            } else {
                prediction = row[x - 2];  // predictor 1: the previous sample of the same component
            }
            const int diff = Difference(row[x], prediction);
            diffs[static_cast<size_t>(y) * width + x] = diff;
            ++counts[Category(diff)];
        }
    }
    const Table table = BuildTable(counts);

    std::vector<uint8_t> out;
    out.reserve(static_cast<size_t>(width) * height * 2);
    Marker(out, 0xD8);  // SOI
    Marker(out, 0xC4);  // DHT
    Word(out, 2 + 1 + 16 + static_cast<int>(table.symbols.size()));
    out.push_back(0x00);  // class 0 (DC/lossless), table 0
    out.insert(out.end(), table.counts.begin(), table.counts.end());
    out.insert(out.end(), table.symbols.begin(), table.symbols.end());
    Marker(out, 0xC3);  // SOF3: lossless, Huffman
    Word(out, 8 + 3 * 2);
    out.push_back(static_cast<uint8_t>(bits));
    Word(out, height);
    Word(out, columns);
    out.push_back(2);  // components
    for (int c = 0; c < 2; ++c) {
        out.push_back(static_cast<uint8_t>(c + 1));
        out.push_back(0x11);  // no subsampling
        out.push_back(0);
    }
    Marker(out, 0xDA);  // SOS
    Word(out, 6 + 2 * 2);
    out.push_back(2);
    for (int c = 0; c < 2; ++c) {
        out.push_back(static_cast<uint8_t>(c + 1));
        out.push_back(0x00);  // Huffman table 0
    }
    out.push_back(1);  // predictor 1
    out.push_back(0);  // Se
    out.push_back(0);  // Ah, Al (no point transform)
    BitWriter writer(out);
    for (const int diff : diffs) {
        const int category = Category(diff);
        writer.Put(table.code[category], table.size[category]);
        if (category != 0 && category != 16) {
            const int extra = diff < 0 ? diff - 1 : diff;  // negative: one's complement in `category` bits
            writer.Put(static_cast<uint32_t>(extra) & ((1u << category) - 1), category);
        }
    }
    writer.Finish();
    Marker(out, 0xD9);  // EOI
    return out;
}

namespace {

[[noreturn]] void Bad(const std::string& why) { throw std::runtime_error("not a valid lossless JPEG stream: " + why); }

struct DecodeTable {
    bool defined = false;
    std::array<int, 17> maxcode{};  // largest code of each length, -1 when none (F.2.2.3)
    std::array<int, 17> valptr{};
    std::array<int, 17> mincode{};
    std::vector<uint8_t> symbols;
};

DecodeTable MakeDecodeTable(const uint8_t* counts, const uint8_t* symbols, size_t total) {
    DecodeTable table;
    table.defined = true;
    table.symbols.assign(symbols, symbols + total);
    int code = 0;
    int k = 0;
    for (int length = 1; length <= 16; ++length) {
        const int n = counts[length - 1];
        if (n == 0) {
            table.maxcode[length] = -1;
        } else {
            table.valptr[length] = k;
            table.mincode[length] = code;
            code += n;
            k += n;
            table.maxcode[length] = code - 1;
        }
        if (code > (1 << length)) Bad("a Huffman table has too many codes");
        code <<= 1;
    }
    return table;
}

class BitReader {
   public:
    BitReader(const uint8_t* data, size_t size, size_t pos) : data_(data), size_(size), pos_(pos) {}
    int Bit() {
        if (count_ == 0) Fill();
        --count_;
        return (current_ >> count_) & 1;
    }
    int Bits(int n) {
        int value = 0;
        for (int i = 0; i < n; ++i) value = (value << 1) | Bit();
        return value;
    }
    // At a restart: drop the partial byte and expect the RSTn marker.
    void Restart(int expected) {
        count_ = 0;
        if (marker_ < 0) {
            if (pos_ + 1 >= size_ || data_[pos_] != 0xFF) Bad("a restart marker is missing");
            marker_ = data_[pos_ + 1];
            pos_ += 2;
        }
        if (marker_ != 0xD0 + expected) Bad("restart markers are out of order");
        marker_ = -1;
    }
    void Finish() {
        count_ = 0;
        // Fill bytes may precede EOI, but another scan or missing marker is not a complete image.
        while (pos_ + 1 < size_ && data_[pos_] == 0xFF && data_[pos_ + 1] == 0xFF) ++pos_;
        if (pos_ + 1 >= size_ || data_[pos_] != 0xFF || data_[pos_ + 1] != 0xD9) {
            Bad("the end-of-image marker is missing");
        }
    }

   private:
    void Fill() {
        if (marker_ >= 0) Bad("the entropy-coded data ends early");
        if (pos_ >= size_) Bad("the stream is truncated");
        uint8_t byte = data_[pos_++];
        if (byte == 0xFF) {
            if (pos_ >= size_) Bad("the stream is truncated");
            const uint8_t next = data_[pos_++];
            if (next != 0x00) {  // a marker: no more data before it
                marker_ = next;
                Bad("the entropy-coded data ends early");
            }
        }
        current_ = byte;
        count_ = 8;
    }
    const uint8_t* data_;
    size_t size_;
    size_t pos_;
    int current_ = 0;
    int count_ = 0;
    int marker_ = -1;
};

int DecodeSymbol(BitReader& bits, const DecodeTable& table) {
    int code = bits.Bit();
    for (int length = 1; length <= 16; ++length) {
        if (table.maxcode[length] >= 0 && code <= table.maxcode[length] && code >= table.mincode[length]) {
            const size_t index = static_cast<size_t>(table.valptr[length] + code - table.mincode[length]);
            if (index >= table.symbols.size()) Bad("a Huffman code has no symbol");
            return table.symbols[index];
        }
        if (length < 16) code = (code << 1) | bits.Bit();
    }
    Bad("an invalid Huffman code");
}

int ReadDifference(BitReader& bits, const DecodeTable& table) {
    const int category = DecodeSymbol(bits, table);
    if (category == 0) return 0;
    if (category == 16) return 32768;
    if (category > 16) Bad("a difference category above 16");
    const int value = bits.Bits(category);
    return value < (1 << (category - 1)) ? value - (1 << category) + 1 : value;
}

int Predict(int predictor, int ra, int rb, int rc) {
    switch (predictor) {
        case 1: return ra;
        case 2: return rb;
        case 3: return rc;
        case 4: return ra + rb - rc;
        case 5: return ra + ((rb - rc) >> 1);
        case 6: return rb + ((ra - rc) >> 1);
        default: return (ra + rb) >> 1;
    }
}

int ReadWord(const uint8_t* data, size_t size, size_t pos) {
    if (pos + 2 > size) Bad("the stream is truncated");
    return (data[pos] << 8) | data[pos + 1];
}

}  // namespace

DecodedJpeg DecodeLosslessJpeg(const uint8_t* data, size_t size) {
    if (size < 4 || data[0] != 0xFF || data[1] != 0xD8) Bad("no start-of-image marker");
    std::array<DecodeTable, 4> tables;
    DecodedJpeg out;
    int precision = 0;
    int restart = 0;
    std::array<int, 4> ids{};
    size_t pos = 2;
    while (true) {
        if (pos + 4 > size) Bad("the stream ends before the scan");
        if (data[pos] != 0xFF) Bad("a marker was expected");
        const uint8_t marker = data[pos + 1];
        if (marker == 0xFF) {  // fill byte
            ++pos;
            continue;
        }
        const int length = ReadWord(data, size, pos + 2);
        if (length < 2 || pos + 2 + static_cast<size_t>(length) > size) Bad("a segment is truncated");
        const uint8_t* body = data + pos + 4;
        const size_t body_size = static_cast<size_t>(length) - 2;
        const size_t next = pos + 2 + static_cast<size_t>(length);
        if (marker == 0xC4) {  // DHT: one or more tables
            size_t at = 0;
            while (at < body_size) {
                if (at + 17 > body_size) Bad("a Huffman table is truncated");
                const int id = body[at] & 0x0F;
                if (id > 3) Bad("a Huffman table number above 3");
                size_t total = 0;
                for (int i = 1; i <= 16; ++i) total += body[at + i];
                if (total > 256 || at + 17 + total > body_size) Bad("a Huffman table is truncated");
                tables[id] = MakeDecodeTable(body + at + 1, body + at + 17, total);
                at += 17 + total;
            }
        } else if (marker == 0xC3) {  // SOF3
            if (body_size < 6) Bad("the frame header is truncated");
            precision = body[0];
            out.height = (body[1] << 8) | body[2];
            out.width = (body[3] << 8) | body[4];
            out.components = body[5];
            if (precision < 2 || precision > 16) Bad("a precision outside 2 to 16 bits");
            if (out.width == 0 || out.height == 0) Bad("the frame has no width or height");
            if (out.components < 1 || out.components > 4) Bad("1 to 4 components are supported");
            if (body_size < 6 + 3 * static_cast<size_t>(out.components)) Bad("the frame header is truncated");
            for (int c = 0; c < out.components; ++c) {
                ids[c] = body[6 + 3 * c];
                if (body[7 + 3 * c] != 0x11) Bad("subsampled components are not supported");
            }
        } else if (marker == 0xDD) {  // DRI
            if (body_size < 2) Bad("the restart interval is truncated");
            restart = (body[0] << 8) | body[1];
        } else if (marker == 0xDA) {  // SOS
            if (out.components == 0) Bad("the scan comes before the frame header");
            if (body_size < 1 || body[0] != out.components || body_size < 4 + 2 * static_cast<size_t>(body[0])) {
                Bad("the scan must hold every component");
            }
            std::array<int, 4> order{};  // frame component of each scan position
            std::array<int, 4> table_of{};
            for (int s = 0; s < out.components; ++s) {
                const int id = body[1 + 2 * s];
                int found = -1;
                for (int c = 0; c < out.components; ++c) {
                    if (ids[c] == id) found = c;
                }
                if (found < 0) Bad("the scan names an unknown component");
                order[s] = found;
                table_of[s] = body[2 + 2 * s] >> 4;
                if (table_of[s] > 3) Bad("a Huffman table number above 3");
                if (!tables[table_of[s]].defined) Bad("the scan uses an undefined Huffman table");
            }
            const int predictor = body[1 + 2 * out.components];
            const int transform = body[3 + 2 * out.components] & 0x0F;
            if (predictor < 1 || predictor > 7) Bad("a predictor outside 1 to 7");
            if (transform >= precision) Bad("the point transform is too large");
            const size_t row = static_cast<size_t>(out.width) * out.components;
            const size_t samples = row * static_cast<size_t>(out.height);
            if (samples > (size - next) * 8 + 64) Bad("the stream is too short for the frame");
            if (restart != 0 && restart % out.width != 0) Bad("restart intervals must be whole rows");
            const int rows_per_restart = restart == 0 ? out.height : restart / out.width;
            out.samples.assign(samples, 0);
            std::vector<int> line(row), above(row);
            const int initial = 1 << (precision - transform - 1);
            BitReader bits(data, size, next);
            for (int y = 0; y < out.height; ++y) {
                const bool first = y % rows_per_restart == 0;
                if (first && y > 0) bits.Restart((y / rows_per_restart - 1) % 8);
                for (int x = 0; x < out.width; ++x) {
                    for (int s = 0; s < out.components; ++s) {
                        const size_t at = static_cast<size_t>(x) * out.components + order[s];
                        int prediction;
                        if (first) {
                            prediction = x == 0 ? initial : line[at - out.components];
                        } else if (x == 0) {
                            prediction = above[at];
                        } else {
                            prediction = Predict(predictor, line[at - out.components], above[at],
                                                 above[at - out.components]);
                        }
                        line[at] = (prediction + ReadDifference(bits, tables[table_of[s]])) & 0xFFFF;
                    }
                }
                uint16_t* dst = out.samples.data() + static_cast<size_t>(y) * row;
                for (size_t i = 0; i < row; ++i) dst[i] = static_cast<uint16_t>((line[i] << transform) & 0xFFFF);
                std::swap(line, above);
            }
            bits.Finish();
            return out;
        } else if (marker == 0xD9) {
            Bad("the stream ends before the scan");
        } else if ((marker >= 0xC0 && marker <= 0xCF) && marker != 0xC4 && marker != 0xC8 && marker != 0xCC) {
            Bad("only lossless (SOF3) frames are supported");
        }
        pos = next;
    }
}

}  // namespace mcrawsome
