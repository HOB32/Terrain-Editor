"""Pure-Python port of the DECODE half of lzham_alpha's lzhamdecomp/lzham_symbol_codec.{h,cpp}.

Source: https://github.com/richgel999/lzham_alpha (master), files
    lzhamdecomp/lzham_symbol_codec.h
    lzhamdecomp/lzham_symbol_codec.cpp
Only the decoder is ported; the encoder half (put_bits / arith_propagate_carry /
assemble_output_buf / the output_symbol recording machinery) is not ported; lzcomp.py
writes streams its own way (see its docstring and FORMAT.md section 5).

Stdlib only, Python 3.14.  C names are kept in comments where they help.
Python ints do not wrap, so every place the C relied on 32-bit (or bit_buf_t)
truncation masks explicitly.

Bit buffer width
----------------
The C picks the width at compile time:

    #if LZHAM_CPU_HAS_64BIT_REGISTERS
       #define LZHAM_SYMBOL_CODEC_USE_64_BIT_BUFFER 1
    #endif
    ... typedef uint64 bit_buf_t; enum { cBitBufSize = 64 };   // else uint32 / 32

We default to 64, matching an x64 build.  The width does NOT change the decoded
output: bits are always consumed from the TOP of the buffer (big-endian bit
order), so the bit sequence is identical either way.  It only changes how
eagerly whole bytes are pulled out of the input, i.e. how far `bytes_consumed`
runs ahead of the logically-consumed position.  The decompressor copes with that
by handing buffered bytes back one at a time via
decode_remove_byte_from_bit_buf() before it ever reads raw bytes at
`bytes_consumed` (that is what the -1 return is for).  Set BIT_BUF_SIZE = 32 if
byte accounting ever needs to match a 32-bit build exactly.

End of input
------------
The real code calls a need_bytes_func callback when the decode buffer runs dry.
For us the whole compressed payload is already in memory, so we behave like the
C does with eof_flag = True: past the end of the buffer the refill yields a ZERO
byte and the read position is NOT advanced.  That is zero-fill, and it means
`bytes_consumed` never exceeds len(data) - start.
"""

# ---------------------------------------------------------------------------
# constants (lzham_symbol_codec.h)
# ---------------------------------------------------------------------------

cSymbolCodecArithMinLen = 0x01000000
cSymbolCodecArithMaxLen = 0xFFFFFFFF

cSymbolCodecArithProbBits = 11
cSymbolCodecArithProbScale = 1 << cSymbolCodecArithProbBits          # 2048
cSymbolCodecArithProbHalfScale = 1 << (cSymbolCodecArithProbBits - 1)  # 1024
cSymbolCodecArithProbMoveBits = 5

# bit_buf_t width.  64 == an x64 build (LZHAM_CPU_HAS_64BIT_REGISTERS).
BIT_BUF_SIZE = 64
_BIT_BUF_MASK = (1 << BIT_BUF_SIZE) - 1

_UINT32_MASK = 0xFFFFFFFF
_UINT16_MAX = 0xFFFF


class BitModel:
    """lzham::adaptive_bit_model.

    clear() is m_bit_0_prob = 1 << (cSymbolCodecArithProbBits - 1) == 1 << 10.
    """

    __slots__ = ('bit_0_prob',)

    def __init__(self):
        self.bit_0_prob = 1 << (cSymbolCodecArithProbBits - 1)   # 1 << 10

    def clear(self):
        self.bit_0_prob = 1 << (cSymbolCodecArithProbBits - 1)

    # adaptive_bit_model::update(uint bit)
    def update(self, bit):
        if not bit:
            self.bit_0_prob += (cSymbolCodecArithProbScale - self.bit_0_prob) >> cSymbolCodecArithProbMoveBits
        else:
            self.bit_0_prob -= self.bit_0_prob >> cSymbolCodecArithProbMoveBits
        # LZHAM_ASSERT(m_bit_0_prob >= 1 && m_bit_0_prob < cSymbolCodecArithProbScale)


class SymbolCodec:
    """lzham::symbol_codec, decode side only."""

    # exposed so callers can read the compiled-in width
    cBitBufSize = BIT_BUF_SIZE

    def __init__(self, data, start=0):
        self.start_decoding(data, start)

    # symbol_codec::start_decoding(pBuf, buf_size, eof_flag=true, ...)
    def start_decoding(self, data, start=0):
        self._data = data                 # m_pDecode_buf (as a buffer + offsets)
        self._buf_start = start
        self._next = start                # m_pDecode_buf_next
        self._end = len(data)             # m_pDecode_buf_end
        self._eof = True                  # m_decode_buf_eof: everything is in memory

        self.bit_buf = 0                  # m_bit_buf   (bit_buf_t)
        self.bit_count = 0                # m_bit_count (int)

        self.arith_value = 0              # m_arith_value  (uint32)
        self.arith_length = 0             # m_arith_length (uint32)

        self.total_model_updates = 0      # m_total_model_updates
        self._decoding = True             # m_mode == cDecoding

    # ------------------------------------------------------------------
    # bit buffer
    # ------------------------------------------------------------------

    def _fill(self, num_bits):
        """The refill loop shared by get_bits / remove_bits / decode_peek_bits.

            while (m_bit_count < (int)num_bits)
            {
               uint c = 0;
               if (m_pDecode_buf_next == m_pDecode_buf_end) { ...need bytes... }
               else c = *m_pDecode_buf_next++;
               m_bit_count += 8;
               m_bit_buf |= (bit_buf_t(c) << (cBitBufSize - m_bit_count));
            }

        At eof the C leaves c == 0 and does not advance the pointer: zero-fill.
        """
        while self.bit_count < num_bits:
            if self._next == self._end:
                c = 0                      # eof: zero-fill, position unchanged
            else:
                c = self._data[self._next]
                self._next += 1
            self.bit_count += 8
            # LZHAM_ASSERT(m_bit_count <= cBitBufSize)
            self.bit_buf = (self.bit_buf | (c << (BIT_BUF_SIZE - self.bit_count))) & _BIT_BUF_MASK

    # symbol_codec::get_bits(uint num_bits)   -- LZHAM_ASSERT(num_bits <= 25)
    def get_bits(self, num_bits):
        if not num_bits:
            return 0
        self._fill(num_bits)
        result = self.bit_buf >> (BIT_BUF_SIZE - num_bits)
        self.bit_buf = (self.bit_buf << num_bits) & _BIT_BUF_MASK
        self.bit_count -= num_bits
        return result

    # symbol_codec::remove_bits(uint num_bits)   -- LZHAM_ASSERT(num_bits <= 25)
    def remove_bits(self, num_bits):
        if not num_bits:
            return
        self._fill(num_bits)
        self.bit_buf = (self.bit_buf << num_bits) & _BIT_BUF_MASK
        self.bit_count -= num_bits

    # symbol_codec::decode_bits(uint num_bits)
    def decode_bits(self, num_bits):
        if not num_bits:
            return 0
        if num_bits > 16:
            a = self.get_bits(num_bits - 16)
            b = self.get_bits(16)
            return ((a << 16) | b) & _UINT32_MASK
        return self.get_bits(num_bits)

    # symbol_codec::decode_peek_bits(uint num_bits)  -- LZHAM_ASSERT(num_bits <= 25)
    def decode_peek_bits(self, num_bits):
        if not num_bits:
            return 0
        self._fill(num_bits)
        return self.bit_buf >> (BIT_BUF_SIZE - num_bits)

    # symbol_codec::decode_remove_bits(uint num_bits)
    def decode_remove_bits(self, num_bits):
        while num_bits > 16:
            self.remove_bits(16)
            num_bits -= 16
        self.remove_bits(num_bits)

    # symbol_codec::decode_align_to_byte()
    def decode_align_to_byte(self):
        if self.bit_count & 7:
            self.remove_bits(self.bit_count & 7)

    # symbol_codec::decode_remove_byte_from_bit_buf()
    def decode_remove_byte_from_bit_buf(self):
        if self.bit_count < 8:
            return -1
        result = self.bit_buf >> (BIT_BUF_SIZE - 8)
        self.bit_buf = (self.bit_buf << 8) & _BIT_BUF_MASK
        self.bit_count -= 8
        return result

    # ------------------------------------------------------------------
    # arithmetic decoder
    # ------------------------------------------------------------------

    # symbol_codec::start_arith_decoding()
    def start_arith_decoding(self):
        self.arith_length = cSymbolCodecArithMaxLen
        self.arith_value = 0
        self.arith_value = (self.get_bits(8) << 24)
        self.arith_value |= (self.get_bits(8) << 16)
        self.arith_value |= (self.get_bits(8) << 8)
        self.arith_value |= self.get_bits(8)

    # symbol_codec::decode(adaptive_bit_model& model, bool update_model)
    def decode_bit(self, model, update=True):
        # renormalise
        while self.arith_length < cSymbolCodecArithMinLen:
            c = self.get_bits(8)
            self.arith_value = ((self.arith_value << 8) | c) & _UINT32_MASK
            self.arith_length = (self.arith_length << 8) & _UINT32_MASK

        x = (model.bit_0_prob * (self.arith_length >> cSymbolCodecArithProbBits)) & _UINT32_MASK
        bit = 1 if self.arith_value >= x else 0

        if not bit:
            if update:
                model.bit_0_prob += (cSymbolCodecArithProbScale - model.bit_0_prob) >> cSymbolCodecArithProbMoveBits
            self.arith_length = x
        else:
            if update:
                model.bit_0_prob -= model.bit_0_prob >> cSymbolCodecArithProbMoveBits
            self.arith_value = (self.arith_value - x) & _UINT32_MASK
            self.arith_length = (self.arith_length - x) & _UINT32_MASK

        return bit

    # ------------------------------------------------------------------
    # table-driven prefix (Huffman) decode
    # ------------------------------------------------------------------

    def decode_symbol(self, model):
        """symbol_codec::decode(quasi_adaptive_huffman_data_model& model).

        `model.decode_tables` is prefix_coding::decoder_tables, and we read the
        m_-prefixed C fields under their bare names:
            table_bits, table_max_code, lookup, decode_start_code_size,
            max_codes, val_ptrs, sorted_symbol_order
        """
        tables = model.decode_tables            # m_pDecode_tables

        # Refill to at least (cBitBufSize - 8) bits.  Codes are at most
        # cMaxExpectedCodeSize == 16 bits, so this is always enough.
        self._fill(BIT_BUF_SIZE - 8)

        k = (self.bit_buf >> (BIT_BUF_SIZE - 16)) + 1

        if k <= tables.table_max_code:
            # table_max_code is 0 when table_bits == 0, and k >= 1, so this
            # branch is never taken for a table-less model: no shift by
            # cBitBufSize can happen here.
            t = tables.lookup[self.bit_buf >> (BIT_BUF_SIZE - tables.table_bits)]
            sym = t & _UINT16_MAX
            length = t >> 16
            # LZHAM_ASSERT(t != UINT32_MAX); LZHAM_ASSERT(model.m_code_sizes[sym] == len)
        else:
            length = tables.decode_start_code_size
            while True:
                if k <= tables.max_codes[length - 1]:
                    break
                length += 1
            val_ptr = tables.val_ptrs[length - 1] + (self.bit_buf >> (BIT_BUF_SIZE - length))
            # Corrupted stream, or a bug.  Two behaviours exist in the C:
            # symbol_codec::decode() asserts and returns 0 immediately, while
            # LZHAM_SYMBOL_CODEC_DECODE_ADAPTIVE_HUFFMAN clamps val_ptr to 0 and
            # carries on.  lzham_lzdecomp.cpp uses the MACRO, so we clamp.
            # m_val_ptrs is a signed int in the C and can legitimately be
            # negative before the addition, hence the < 0 test.
            if val_ptr < 0 or val_ptr >= model.total_syms:
                val_ptr = 0
            sym = tables.sorted_symbol_order[val_ptr]

        self.bit_buf = (self.bit_buf << length) & _BIT_BUF_MASK
        self.bit_count -= length

        # The C does the freq bump and the update-cycle countdown inline; that
        # is exactly raw_quasi_adaptive_huffman_data_model::update(uint sym).
        if model.symbols_until_update == 1:
            self.total_model_updates += 1       # m_total_model_updates++
        model.update(sym)

        return sym

    # ------------------------------------------------------------------

    # symbol_codec::stop_decoding()
    def stop_decoding(self):
        n = self._next - self._buf_start
        self._decoding = False
        return n

    @property
    def bytes_consumed(self):
        """symbol_codec::decode_get_bytes_consumed()."""
        return self._next - self._buf_start

    @property
    def bits_remaining(self):
        """symbol_codec::decode_get_bits_remaining()."""
        return ((self._end - self._next) << 3) + self.bit_count
