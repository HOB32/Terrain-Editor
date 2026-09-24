"""The LZHAM alpha decoder as one fused loop - same output as lzdecomp, ~1.3x faster.

lzdecomp.py is the readable, line-by-line port of lzham_lzdecomp.cpp and stays the
reference. It spends most of its time in Python calls: ~0.9 M symbol decodes and ~1 M
arithmetic decisions on a busy 2.8 MB terrain texture file, each several method calls
deep (SymbolCodec.decode_symbol -> _fill -> model.update ...). This module does the same
work with the bit buffer, the arithmetic coder and the Huffman table walk held in local
variables of a single function, and the bit models as plain int lists. The models keep
the reference frequency/cycle bookkeeping (QuasiAdaptiveHuffmanModel); only the decode
table is built by _Tables, with list slices. test_lzham_codec.py checks both decoders
byte-for-byte on real chunks. Per-symbol interpretation is now the floor in Python: for
a folder of files, lzham.batch spreads the work over every core instead.

    from lzham.fastdecomp import decompress_stream
"""
from __future__ import annotations

import zlib

try:
    from .lzdecomp import (LzhamDecompressError, LZDecompBase, dict_size_log2_from_cmf,
                           DEFAULT_DICT_SIZE_LOG2, cMinDictSizeLog2, cMaxDictSizeLog2, LZHAM_Z_LZHAM,
                           cNumStates, cNumLitStates, cNumIsMatchContextBits, cLZXNumSpecialLengths,
                           cLZXLowestUsableMatchSlot, cMinMatchLen, cMaxMatchLen, cNumHugeMatchCodes,
                           cLZXNumSecondaryLengths, s_literal_next_state, s_huge_match_base_len,
                           s_huge_match_code_len, _new_sym_model)
except ImportError:  # pragma: no cover
    from lzdecomp import (LzhamDecompressError, LZDecompBase, dict_size_log2_from_cmf,
                          DEFAULT_DICT_SIZE_LOG2, cMinDictSizeLog2, cMaxDictSizeLog2, LZHAM_Z_LZHAM,
                          cNumStates, cNumLitStates, cNumIsMatchContextBits, cLZXNumSpecialLengths,
                          cLZXLowestUsableMatchSlot, cMinMatchLen, cMaxMatchLen, cNumHugeMatchCodes,
                          cLZXNumSecondaryLengths, s_literal_next_state, s_huge_match_base_len,
                          s_huge_match_code_len, _new_sym_model)

try:
    from .models import QuasiAdaptiveHuffmanModel, generate_huffman_codes, limit_max_code_size, \
        MAX_EXPECTED_CODE_SIZE
except ImportError:  # pragma: no cover
    from models import QuasiAdaptiveHuffmanModel, generate_huffman_codes, limit_max_code_size, \
        MAX_EXPECTED_CODE_SIZE

__all__ = ['decompress', 'decompress_stream']

_M64 = (1 << 64) - 1
_M32 = 0xFFFFFFFF


class _Tables:
    """prefix_coding.DecoderTables with only what the decode walk reads, built with list
    slices instead of one lookup entry at a time (same values; see DecoderTables.init)."""
    __slots__ = ('table_bits', 'table_max_code', 'decode_start_code_size', 'max_codes', 'val_ptrs',
                 'sorted_symbol_order', 'lookup')

    def __init__(self, code_sizes, table_bits):
        n_codes = [0] * 17
        for c in code_sizes:
            n_codes[c] += 1
        order = sorted((c, s) for s, c in enumerate(code_sizes) if c)
        self.sorted_symbol_order = [s for _, s in order]
        max_codes = [0] * 17
        val_ptrs = [0] * 17
        min_codes = [0] * 16
        next_code = used = 0
        min_size, max_size = 255, 0
        for i in range(1, 17):
            n = n_codes[i]
            if n:
                min_size = min(min_size, i)
                max_size = i
                min_codes[i - 1] = next_code
                mc = next_code + n - 1
                max_codes[i - 1] = 1 + (((mc << (16 - i)) | ((1 << (16 - i)) - 1)) & _M32)
                val_ptrs[i - 1] = used - next_code
                next_code += n
                used += n
            next_code <<= 1
        max_codes[16] = _M32
        val_ptrs[16] = 0xFFFFF
        if table_bits <= min_size:
            table_bits = 0
        lookup = None
        table_max_code = 0
        start = min_size
        if table_bits:
            lookup = [_M32] * (1 << table_bits)
            k = 0
            for c, s in order:
                if c > table_bits:
                    break
                code = min_codes[c - 1] + (k - (val_ptrs[c - 1] + min_codes[c - 1]))
                fill = 1 << (table_bits - c)
                at = code << (table_bits - c)
                lookup[at:at + fill] = [s | (c << 16)] * fill
                k += 1
            for i in range(table_bits, 0, -1):
                if n_codes[i]:
                    table_max_code = max_codes[i - 1]
                    start = table_bits + 1
                    for j in range(table_bits + 1, max_size + 1):
                        if n_codes[j]:
                            start = j
                            break
                    break
        self.table_bits = table_bits
        self.table_max_code = table_max_code
        self.decode_start_code_size = start
        self.max_codes = max_codes
        self.val_ptrs = val_ptrs
        self.lookup = lookup


class _FastModel(QuasiAdaptiveHuffmanModel):
    """The reference model's bookkeeping, with _Tables for the rebuild."""

    def update_tables(self) -> None:
        self.total_count += self.update_cycle
        while self.total_count >= 32768:
            self.rescale()
        sizes, max_size, total = generate_huffman_codes(self.sym_freq, self.total_syms)
        if total != self.total_count:
            raise LzhamDecompressError('model out of step (total_freq %d != %d)' % (total, self.total_count))
        sizes = list(sizes)
        if max_size > MAX_EXPECTED_CODE_SIZE:
            sizes = list(limit_max_code_size(self.total_syms, sizes, MAX_EXPECTED_CODE_SIZE))
        self.code_sizes = sizes
        self.decode_tables = _Tables(sizes, self.decoder_table_bits)
        self.update_cycle = 2 * self.update_cycle if self.fast_updating else (5 * self.update_cycle) >> 2
        if self.update_cycle > self.max_cycle:
            self.update_cycle = self.max_cycle
        self.symbols_until_update = self.update_cycle


def decompress_stream(data: bytes, uncompressed_size: int | None = None) -> bytes:
    """Decode a zlib-style LZHAM stream (starts 0x5E 0xD1), checking header and adler32."""
    if len(data) < 2:
        raise LzhamDecompressError('FAILED_BAD_ZLIB_HEADER: fewer than 2 bytes')
    cmf, flg = data[0], data[1]
    if ((cmf << 8) + flg) % 31 or (cmf & 15) != LZHAM_Z_LZHAM:
        raise LzhamDecompressError('FAILED_BAD_ZLIB_HEADER (cmf=0x%02X flg=0x%02X)' % (cmf, flg))
    if flg & 32:
        raise LzhamDecompressError('FAILED_NEED_SEED_BYTES: FDICT is set')
    log2 = dict_size_log2_from_cmf(cmf)
    if not cMinDictSizeLog2 <= log2 <= cMaxDictSizeLog2:
        log2 = DEFAULT_DICT_SIZE_LOG2
    return decompress(memoryview(data)[2:], log2, uncompressed_size)


def decompress(data, dict_size_log2: int = DEFAULT_DICT_SIZE_LOG2, uncompressed_size: int | None = None) -> bytes:
    """Decode a raw alpha bitstream (after the 2-byte zlib header). A damaged stream always
    raises LzhamDecompressError, never an IndexError from deep inside the table walk."""
    try:
        return _decompress(data, dict_size_log2, uncompressed_size)
    except LzhamDecompressError:
        raise
    except (IndexError, KeyError, TypeError, ValueError, AssertionError, OverflowError) as error:
        raise LzhamDecompressError(f'FAILED_BAD_CODE ({type(error).__name__}: {error})') from error


def _decompress(data, dict_size_log2, uncompressed_size):
    data = bytes(data)
    end = len(data)
    if not end:
        raise LzhamDecompressError('FAILED_INITIALIZING: empty input')
    base = LZDecompBase(dict_size_log2)
    pos_base, pos_extra = base.m_lzx_position_base, base.m_lzx_position_extra_bits

    # ---- bit buffer (64-bit, MSB first), held in locals: bb, bc, nxt
    bb = bc = 0
    nxt = 0

    def bits(n):
        """Raw bits (decode_bits): the rare path, a closure over the buffer state."""
        nonlocal bb, bc, nxt
        if not n:
            return 0
        v = 0
        while n:
            take = 16 if n > 16 else n
            while bc < take:
                c = data[nxt] if nxt < end else 0
                if nxt < end:
                    nxt += 1
                bc += 8
                bb |= c << (64 - bc)
            v = (v << take) | (bb >> (64 - take))
            bb = (bb << take) & _M64
            bc -= take
            n -= take
        return v

    tmp = bits(2)
    fast, polar = (tmp & 2) != 0, (tmp & 1) != 0
    mk = (lambda n: _new_sym_model(n, fast, polar)) if polar else (lambda n: _FastModel(n, fast, False))
    lit = [mk(256) for _ in range(64)]
    dlit = [mk(256) for _ in range(64)]
    main = mk(cLZXNumSpecialLengths + (base.m_num_lzx_slots - cLZXLowestUsableMatchSlot) * 8)
    rep_len = [mk(cNumHugeMatchCodes + (cMaxMatchLen - cMinMatchLen + 1)) for _ in range(2)]
    large_len = [mk(cNumHugeMatchCodes + cLZXNumSecondaryLengths) for _ in range(2)]
    dist_lsb = mk(16)
    all_models = lit + dlit + [main] + rep_len + large_len + [dist_lsb]
    p_match = [1024] * (cNumStates << cNumIsMatchContextBits)
    p_rep, p_rep0, p_rep0_1, p_rep1, p_rep2 = ([1024] * cNumStates for _ in range(5))

    out = bytearray()
    append = out.append
    limit = uncompressed_size

    def sym(model):
        """Huffman symbol through `model` (inlined twice below for literals)."""
        nonlocal bb, bc, nxt
        while bc <= 56:
            c = data[nxt] if nxt < end else 0
            if nxt < end:
                nxt += 1
            bc += 8
            bb |= c << (64 - bc)
        t = model.decode_tables
        k = (bb >> 48) + 1
        if k <= t.table_max_code:
            e = t.lookup[bb >> (64 - t.table_bits)]
            s, ln = e & 0xFFFF, e >> 16
        else:
            ln = t.decode_start_code_size
            mc = t.max_codes
            while k > mc[ln - 1]:
                ln += 1
            vp = t.val_ptrs[ln - 1] + (bb >> (64 - ln))
            if vp < 0 or vp >= model.total_syms:
                vp = 0
            s = t.sorted_symbol_order[vp]
        bb = (bb << ln) & _M64
        bc -= ln
        model.sym_freq[s] += 1
        model.symbols_until_update -= 1
        if not model.symbols_until_update:
            model.update_tables()
        return s

    # ---- a match's further decisions (arithmetic) go through this helper
    def dec(probs, j):
        nonlocal av, al, bb, bc, nxt
        while al < 0x1000000:
            while bc < 8:
                c = data[nxt] if nxt < end else 0
                if nxt < end:
                    nxt += 1
                bc += 8
                bb |= c << (64 - bc)
            av = ((av << 8) | (bb >> 56)) & _M32
            bb = (bb << 8) & _M64
            bc -= 8
            al = (al << 8) & _M32
        q = probs[j]
        y = q * (al >> 11)
        if av < y:
            probs[j] = q + ((2048 - q) >> 5)
            al = y
            return 0
        probs[j] = q - (q >> 5)
        av -= y
        al -= y
        return 1

    def huge():
        n = 0
        while n < 3 and bits(1):
            n += 1
        return s_huge_match_base_len[n] + bits(s_huge_match_code_len[n])

    while True:
        block_type = bits(2)
        if block_type == 0:                                   # sync
            flush = bits(2)
            if flush == 1:
                for m in all_models:
                    m.reset_update_rate()
            elif flush == 2:
                for m in all_models:
                    m.reset()
                p_match[:] = [1024] * len(p_match)
                for p in (p_rep, p_rep0, p_rep0_1, p_rep1, p_rep2):
                    p[:] = [1024] * cNumStates
            if bc & 7:
                bits(bc & 7)
            if bits(16) != 0 or bits(16) != 0xFFFF:
                raise LzhamDecompressError('FAILED_BAD_SYNC_BLOCK')
        elif block_type == 2:                                 # raw
            n = bits(24)
            check = bits(8)
            if check != ((n & 0xFF) ^ ((n >> 8) & 0xFF) ^ ((n >> 16) & 0xFF)):
                raise LzhamDecompressError('FAILED_BAD_RAW_BLOCK (check bits)')
            n += 1
            if bc & 7:
                bits(bc & 7)
            while n and bc >= 8:                              # bytes still in the buffer
                append(bb >> 56)
                bb = (bb << 8) & _M64
                bc -= 8
                n -= 1
            out += data[nxt:nxt + n]
            nxt += n
        elif block_type == 1:                                 # compressed
            av = (bits(8) << 24) | (bits(8) << 16) | (bits(8) << 8) | bits(8)
            al = _M32
            h0 = h1 = h2 = h3 = 1
            state = prev_char = prev_prev = 0
            flush = bits(2)
            if flush == 1:
                for m in all_models:
                    m.reset_update_rate()
            elif flush == 2:
                for m in all_models:
                    m.reset()
                p_match[:] = [1024] * len(p_match)
                for p in (p_rep, p_rep0, p_rep0_1, p_rep1, p_rep2):
                    p[:] = [1024] * cNumStates
            while True:
                # ---- is_match decision (arithmetic), inlined
                while al < 0x1000000:
                    while bc < 8:
                        c = data[nxt] if nxt < end else 0
                        if nxt < end:
                            nxt += 1
                        bc += 8
                        bb |= c << (64 - bc)
                    av = ((av << 8) | (bb >> 56)) & _M32
                    bb = (bb << 8) & _M64
                    bc -= 8
                    al = (al << 8) & _M32
                i = (prev_char >> 2) + (state << 6)
                p = p_match[i]
                x = p * (al >> 11)
                if av < x:
                    p_match[i] = p + ((2048 - p) >> 5)
                    al = x
                    # ---- literal
                    if state < cNumLitStates:
                        c = sym(lit[(prev_char >> 5) | ((prev_prev >> 5) << 3)])
                    else:
                        o = len(out) - h0
                        if o < 1:
                            raise LzhamDecompressError('FAILED_BAD_CODE (delta literal context)')
                        r0 = out[o]
                        c = sym(dlit[(r0 >> 5) | ((out[o - 1] >> 5) << 3)]) ^ r0
                    append(c)
                    prev_prev, prev_char = prev_char, c
                    state = s_literal_next_state[state]
                    continue
                p_match[i] = p - (p >> 5)
                av -= x
                al -= x

                lit_state = state < cNumLitStates
                if dec(p_rep, state):
                    if dec(p_rep0, state):
                        if dec(p_rep0_1, state):
                            length = 1
                            state = 9 if lit_state else 11
                        else:
                            length = sym(rep_len[0 if lit_state else 1]) + cMinMatchLen
                            if length == cMaxMatchLen + 1:
                                length = huge()
                            state = 8 if lit_state else 11
                    else:
                        length = sym(rep_len[0 if lit_state else 1]) + cMinMatchLen
                        if length == cMaxMatchLen + 1:
                            length = huge()
                        if dec(p_rep1, state):
                            h0, h1 = h1, h0
                        elif dec(p_rep2, state):
                            h0, h1, h2 = h2, h0, h1
                        else:
                            h0, h1, h2, h3 = h3, h0, h1, h2
                        state = 8 if lit_state else 11
                else:
                    s = sym(main) - cLZXNumSpecialLengths
                    if s < 0:
                        if s == -cLZXNumSpecialLengths:          # end of block
                            break
                        h0 = h1 = h2 = h3 = 1                    # partial state reset
                        state = 0
                        continue
                    length = (s & 7) + 2
                    slot = (s >> 3) + cLZXLowestUsableMatchSlot
                    if length == 9:
                        length += sym(large_len[0 if lit_state else 1])
                        if length == cMaxMatchLen + 1:
                            length = huge()
                    nb = pos_extra[slot]
                    if nb < 3:
                        extra = bits(nb)
                    else:
                        extra = bits(nb - 4) << 4 if nb > 4 else 0
                        extra += sym(dist_lsb)
                    h0, h1, h2, h3 = pos_base[slot] + extra, h0, h1, h2
                    state = cNumLitStates if lit_state else cNumLitStates + 3

                dst = len(out)
                if h0 > dst or (limit is not None and dst + length > limit):
                    raise LzhamDecompressError('FAILED_BAD_CODE (match dist %d len %d at ofs %d)' % (h0, length, dst))
                src = dst - h0
                if h0 == 1:
                    c = out[src]
                    out += bytes((c,)) * length
                    prev_prev = prev_char if length == 1 else c
                    prev_char = c
                elif length == 1:
                    prev_prev = prev_char
                    prev_char = out[src]
                    append(prev_char)
                elif h0 >= length:
                    out += out[src:src + length]
                    prev_prev, prev_char = out[-2], out[-1]
                else:                                             # overlapping: repeat the period
                    period = out[src:dst]
                    reps = -(-length // h0)
                    out += (period * reps)[:length]
                    prev_prev, prev_char = out[-2], out[-1]
            if bc & 7:
                bits(bc & 7)
        elif block_type == 3:                                 # EOF
            break

    if bc & 7:
        bits(bc & 7)
    adler = (bits(16) << 16) | bits(16)
    mine = zlib.adler32(out, 1) & _M32
    if adler != mine:
        raise LzhamDecompressError('FAILED_ADLER32: stream says 0x%08X, output is 0x%08X (%d bytes)'
                                   % (adler, mine, len(out)))
    if uncompressed_size is not None and len(out) != uncompressed_size:
        raise LzhamDecompressError('decoded %d bytes, expected %d' % (len(out), uncompressed_size))
    return bytes(out)
