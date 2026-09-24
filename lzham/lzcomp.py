"""LZHAM alpha COMPRESSOR - the half the decoder port left out.

Writes streams the game's own decoder reads: zlib-style header 0x5E 0xD1 (1 MiB window,
dict_size_log2 20), slow table updating, Huffman (not polar) codes - the exact settings
of every H1Z1 .cnk/.ctg payload. It is the mirror image of lzdecomp.py: every decision
the decoder reads, this writes, in the same order and through the same adaptive models,
so the decoder's model state stays identical to ours symbol for symbol.

How the bitstream is laid out (read off lzdecomp / symbol_codec, which decode every
shipped chunk with a matching adler32):

  * one MSB-first bitstream carries three kinds of data, interleaved:
      raw bits      (block headers, huge-length escapes, distance extra bits)
      prefix codes  (canonical Huffman, from each quasi-adaptive model's current sizes)
      arithmetic    (the is_match / is_rep... decisions, 11-bit adaptive bit models)
  * the arithmetic coder's bytes sit in the stream exactly where the decoder pulls them:
    4 bytes when a compressed block starts, then one byte per renormalisation before a
    decision. The encoder's interval evolves identically (same probabilities, same
    decisions), so it knows those positions - but a carry can still change bytes it has
    already produced. So each block is done in two passes: (1) model and code
    everything, keeping the arithmetic bytes in their own buffer where carries are
    trivial, recording where each byte will be read; (2) lay the bytes into the
    bitstream at those points. The block ends with 4 bytes of the final interval base,
    which is exactly what the decoder has left to read.

Compression: greedy + one-step lazy LZ with hash chains over the 1 MiB window, the four
rep distances (rep0 single-byte matches included), delta literals after a match
(automatic: the decoder switches model by state), huge-length escapes up to 65,536.
It is not as tight as the original C encoder, but it is a real LZHAM stream - typically
far smaller than the stored (raw-block) streams zone_build.lzham_store writes.

    from lzham import compress_stream, decompress_stream
    blob = compress_stream(payload)           # starts 0x5E 0xD1, ends with adler32
    assert decompress_stream(blob) == payload
"""
from __future__ import annotations

import zlib
from bisect import bisect_right

try:
    from .models import QuasiAdaptiveHuffmanModel, generate_huffman_codes, limit_max_code_size, \
        MAX_EXPECTED_CODE_SIZE
    from .prefix_coding import generate_codes
    from .lzdecomp import (LZDecompBase, cMinMatchLen, cMaxMatchLen, cMaxHugeMatchLen, cNumStates,
                           cNumLitStates, cNumLitPredBits, cNumDeltaLitPredBits, cNumIsMatchContextBits,
                           cLZXNumSpecialLengths, cLZXLowestUsableMatchSlot, cNumHugeMatchCodes,
                           cLZXNumSecondaryLengths, cMaxLen2MatchDist, s_literal_next_state,
                           s_huge_match_base_len, s_huge_match_code_len, DEFAULT_DICT_SIZE_LOG2)
except ImportError:  # pragma: no cover - flat layout
    from models import QuasiAdaptiveHuffmanModel, generate_huffman_codes, limit_max_code_size, \
        MAX_EXPECTED_CODE_SIZE
    from prefix_coding import generate_codes
    from lzdecomp import (LZDecompBase, cMinMatchLen, cMaxMatchLen, cMaxHugeMatchLen, cNumStates,
                          cNumLitStates, cNumLitPredBits, cNumDeltaLitPredBits, cNumIsMatchContextBits,
                          cLZXNumSpecialLengths, cLZXLowestUsableMatchSlot, cNumHugeMatchCodes,
                          cLZXNumSecondaryLengths, cMaxLen2MatchDist, s_literal_next_state,
                          s_huge_match_base_len, s_huge_match_code_len, DEFAULT_DICT_SIZE_LOG2)

__all__ = ['compress', 'compress_stream', 'ZLIB_HEADER']

ZLIB_HEADER = b'\x5e\xd1'        # CM 14 (LZHAM), CINFO 5 (2^20 window), FLEVEL 3 - as the game's
_ARITH_MIN_LEN = 0x01000000
_ARITH_MAX_LEN = 0xFFFFFFFF
_MASK32 = 0xFFFFFFFF
_PROB_HALF = 1 << 10
# event kinds in the per-block record (low 2 bits)
_EV_BITS, _EV_ARITH, _EV_START = 0, 1, 2


class _EncModel(QuasiAdaptiveHuffmanModel):
    """The quasi-adaptive Huffman model, encoding side.

    Identical frequency/cycle bookkeeping to the decoder's model (that is what keeps the
    two in step); the only difference is what a table rebuild produces - canonical codes
    to write instead of decoder lookup tables."""

    def update_tables(self) -> None:
        assert self.symbols_until_update == 0
        self.total_count += self.update_cycle
        while self.total_count >= 32768:
            self.rescale()
        code_sizes, max_code_size, total_freq = generate_huffman_codes(self.sym_freq, self.total_syms)
        if total_freq != self.total_count:
            raise ValueError('total_freq %d != total_count %d' % (total_freq, self.total_count))
        code_sizes = list(code_sizes)
        if max_code_size > MAX_EXPECTED_CODE_SIZE:
            code_sizes = list(limit_max_code_size(self.total_syms, code_sizes, MAX_EXPECTED_CODE_SIZE))
        self.code_sizes = code_sizes
        self.codes = generate_codes(self.total_syms, code_sizes)
        if self.fast_updating:
            self.update_cycle = 2 * self.update_cycle
        else:
            self.update_cycle = (5 * self.update_cycle) >> 2
        if self.update_cycle > self.max_cycle:
            self.update_cycle = self.max_cycle
        self.symbols_until_update = self.update_cycle


class _BitWriter:
    """MSB-first bit output, the order symbol_codec reads bits back."""
    __slots__ = ('out', 'acc', 'n')

    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.n = 0

    def put(self, value, bits):
        if not bits:
            return
        self.acc = (self.acc << bits) | value
        self.n += bits
        out, acc, n = self.out, self.acc, self.n
        while n >= 8:
            n -= 8
            out.append((acc >> n) & 0xFF)
        self.acc = acc & ((1 << n) - 1)
        self.n = n

    def align(self):
        if self.n:
            self.put(0, 8 - self.n)

    def raw(self, data):
        assert self.n == 0
        self.out += data


class _Encoder:
    def __init__(self, data, dict_size_log2=DEFAULT_DICT_SIZE_LOG2, chain=24, lazy=True):
        self.data = bytes(data)
        self.window = (1 << dict_size_log2) - 1
        base = LZDecompBase(dict_size_log2)
        self.slots = base.m_num_lzx_slots
        self.pos_base = base.m_lzx_position_base[:self.slots]
        self.pos_extra = base.m_lzx_position_extra_bits[:self.slots]
        self.chain = chain
        self.lazy = lazy
        mk = lambda n: _EncModel(n, False, False)
        self.lit = [mk(256) for _ in range(1 << cNumLitPredBits)]
        self.delta_lit = [mk(256) for _ in range(1 << cNumDeltaLitPredBits)]
        self.main = mk(cLZXNumSpecialLengths + (self.slots - cLZXLowestUsableMatchSlot) * 8)
        self.rep_len = [mk(cNumHugeMatchCodes + (cMaxMatchLen - cMinMatchLen + 1)) for _ in range(2)]
        self.large_len = [mk(cNumHugeMatchCodes + cLZXNumSecondaryLengths) for _ in range(2)]
        self.dist_lsb = mk(16)
        # adaptive bit models as plain ints in lists (probability of a 0)
        self.p_is_match = [_PROB_HALF] * (cNumStates << cNumIsMatchContextBits)
        self.p_is_rep = [_PROB_HALF] * cNumStates
        self.p_rep0 = [_PROB_HALF] * cNumStates
        self.p_rep0_1 = [_PROB_HALF] * cNumStates
        self.p_rep1 = [_PROB_HALF] * cNumStates
        self.p_rep2 = [_PROB_HALF] * cNumStates

    # ------------------------------------------------------------ pass 1 primitives
    def _block_begin(self):
        self.ev = []                 # the record: ints, kind in the low 2 bits
        self.ab = bytearray()        # arithmetic bytes of this block
        self.low = 0
        self.length = _ARITH_MAX_LEN
        self.ev.append(_EV_START)

    def _bits(self, value, bits):
        if bits:
            self.ev.append((value << 7) | (bits << 2) | _EV_BITS)

    def _bit(self, probs, i, bit):
        """One arithmetic-coded decision through adaptive model probs[i]."""
        length = self.length
        low = self.low
        r = 0
        while length < _ARITH_MIN_LEN:            # renormalise where the decoder does
            self.ab.append((low >> 24) & 0xFF)
            low = (low << 8) & _MASK32
            length = (length << 8) & _MASK32
            r += 1
        p = probs[i]
        x = (p * (length >> 11)) & _MASK32
        if not bit:
            length = x
            probs[i] = p + ((2048 - p) >> 5)
        else:
            low += x
            length = (length - x) & _MASK32
            probs[i] = p - (p >> 5)
            if low > _MASK32:                      # carry into bytes already produced
                low &= _MASK32
                ab = self.ab
                k = len(ab) - 1
                while ab[k] == 0xFF:
                    ab[k] = 0
                    k -= 1
                ab[k] += 1
        self.low, self.length = low, length
        self.ev.append((r << 2) | _EV_ARITH)

    def _sym(self, model, sym):
        self.ev.append((model.codes[sym] << 7) | (model.code_sizes[sym] << 2) | _EV_BITS)
        model.update(sym)

    def _huge(self, length):
        n = 0
        while n < 3 and length >= s_huge_match_base_len[n + 1]:
            n += 1
        for _ in range(n):
            self._bits(1, 1)
        if n < 3:
            self._bits(0, 1)
        self._bits(length - s_huge_match_base_len[n], s_huge_match_code_len[n])

    def _block_end(self, w):
        """Pass 2: the block's record into the bitstream, arithmetic bytes where read."""
        low = self.low
        self.ab += bytes(((low >> 24) & 0xFF, (low >> 16) & 0xFF, (low >> 8) & 0xFF, low & 0xFF))
        ab, at, put = self.ab, 0, w.put
        for e in self.ev:
            kind = e & 3
            if kind == _EV_BITS:
                put(e >> 7, (e >> 2) & 31)
            elif kind == _EV_ARITH:
                r = e >> 2
                for _ in range(r):
                    put(ab[at], 8)
                    at += 1
            else:
                for _ in range(4):
                    put(ab[at], 8)
                    at += 1
        if at != len(ab):
            raise AssertionError(f'arithmetic bytes placed {at} of {len(ab)}')
        w.align()

    # ------------------------------------------------------------ LZ parse
    def _longest(self, pos, cand, limit):
        """Length of the match between data[pos:] and data[cand:], up to limit."""
        d = self.data
        if d[cand:cand + 2] != d[pos:pos + 2]:
            return 0
        lo, hi = 2, limit
        if d[cand:cand + hi] == d[pos:pos + hi]:
            return hi
        # galloping then binary search on slice equality (C-speed compares)
        step = 8
        while lo + step < hi and d[cand:cand + lo + step] == d[pos:pos + lo + step]:
            lo += step
            step <<= 1
        hi = min(hi, lo + step)
        while lo + 1 < hi:
            mid = (lo + hi) >> 1
            if d[cand:cand + mid] == d[pos:pos + mid]:
                lo = mid
            else:
                hi = mid
        return lo

    def _find(self, pos, head, prev):
        """Best (length, distance) normal match at pos from the hash chain."""
        d = self.data
        n = len(d)
        if pos + 3 > n:
            return 0, 0
        limit = min(cMaxHugeMatchLen, n - pos)
        h = (d[pos] << 16) | (d[pos + 1] << 8) | d[pos + 2]
        cand = head.get(h, -1)
        best_len, best_dist, depth = 0, 0, self.chain
        while cand >= 0 and depth:
            dist = pos - cand
            if dist > self.window:
                break
            if d[cand + best_len] == d[pos + best_len] if pos + best_len < n else False:
                ln = self._longest(pos, cand, limit)
                if ln > best_len:
                    best_len, best_dist = ln, dist
                    if ln == limit:
                        break
            cand = prev[cand]
            depth -= 1
        if best_len == 2 and best_dist > cMaxLen2MatchDist:
            return 0, 0
        return best_len, best_dist

    @staticmethod
    def _insert(d, pos, head, prev):
        if pos + 3 <= len(d):
            h = (d[pos] << 16) | (d[pos + 1] << 8) | d[pos + 2]
            prev[pos] = head.get(h, -1)
            head[h] = pos

    def compress(self):
        d = self.data
        n = len(d)
        w = _BitWriter()
        w.raw(ZLIB_HEADER)
        w.put(0, 2)                              # fast_table_updating = 0, use_polar_codes = 0
        if n:
            self._block(w, 0, n)
        w.put(3, 2)                              # EOF block
        w.align()
        adler = zlib.adler32(d, 1) & _MASK32
        w.put(adler >> 16, 16)
        w.put(adler & 0xFFFF, 16)
        return bytes(w.out)

    def _block(self, w, start, end):
        d = self.data
        w.put(1, 2)                              # compressed block
        self._block_begin()
        self._bits(0, 2)                         # block flush type: none
        hist = [1, 1, 1, 1]
        state = 0
        prev_char = prev_prev = 0
        head, prev = {}, [-1] * len(d)
        pos = start
        pending = None                           # lazy: the match found one step ahead
        lit_half, dlit_half = cNumLitPredBits // 2, cNumDeltaLitPredBits // 2
        while pos < end:
            # ---- choose: rep matches, normal match (lazy), or a literal
            best_rep, best_rep_len = -1, 0
            limit = min(cMaxHugeMatchLen, end - pos)
            if limit >= 2:
                for k in range(4):
                    dist = hist[k]
                    if dist <= pos:
                        ln = self._longest(pos, pos - dist, limit)
                        if ln > best_rep_len:
                            best_rep, best_rep_len = k, ln
            if pending is not None and pending[0] == pos:
                m_len, m_dist = pending[1], pending[2]
            else:
                m_len, m_dist = self._find(pos, head, prev)
            pending = None
            if m_len >= 3 and self.lazy and m_len < 128 and pos + 1 < end:
                self._insert(d, pos, head, prev)
                n_len, n_dist = self._find(pos + 1, head, prev)
                if n_len > m_len + 1:
                    pending = (pos + 1, n_len, n_dist)
                    m_len = 0                    # take a literal now, the longer match next
            else:
                self._insert(d, pos, head, prev)
            use_rep = best_rep_len >= 2 and best_rep_len + 1 >= m_len
            if pending is not None:
                use_rep = False
            ctx = (prev_char >> (8 - cNumIsMatchContextBits)) + (state << cNumIsMatchContextBits)
            if use_rep:
                length = best_rep_len
                self._bit(self.p_is_match, ctx, 1)
                self._bit(self.p_is_rep, state, 1)
                lit_state = state < cNumLitStates
                if best_rep == 0:
                    self._bit(self.p_rep0, state, 1)
                    self._bit(self.p_rep0_1, state, 0)
                    self._rep_len(lit_state, length)
                    state = 8 if lit_state else 11
                else:
                    self._bit(self.p_rep0, state, 0)
                    self._rep_len(lit_state, length)
                    self._bit(self.p_rep1, state, 1 if best_rep == 1 else 0)
                    if best_rep >= 2:
                        self._bit(self.p_rep2, state, 1 if best_rep == 2 else 0)
                    dist = hist[best_rep]
                    del hist[best_rep]
                    hist.insert(0, dist)
                    state = 8 if lit_state else 11
                dist = hist[0]
            elif m_len >= 2:
                length, dist = m_len, m_dist
                self._bit(self.p_is_match, ctx, 1)
                self._bit(self.p_is_rep, state, 0)
                slot = bisect_right(self.pos_base, dist) - 1
                self._sym(self.main, cLZXNumSpecialLengths + ((slot - cLZXLowestUsableMatchSlot) << 3)
                          + min(length - 2, 7))
                if length >= 9:
                    large = self.large_len[1 if state >= cNumLitStates else 0]
                    if length <= cMaxMatchLen:
                        self._sym(large, length - 9)
                    else:
                        self._sym(large, cLZXNumSecondaryLengths)
                        self._huge(length)
                extra = dist - self.pos_base[slot]
                nb = self.pos_extra[slot]
                if nb < 3:
                    self._bits(extra, nb)
                else:
                    if nb > 4:
                        self._bits(extra >> 4, nb - 4)
                    self._sym(self.dist_lsb, extra & 15)
                hist = [dist, hist[0], hist[1], hist[2]]
                state = cNumLitStates if state < cNumLitStates else cNumLitStates + 3
            else:
                # literal (or a single-byte rep0 when it is the same byte)
                c = d[pos]
                if hist[0] <= pos and d[pos - hist[0]] == c and state >= cNumLitStates:
                    self._bit(self.p_is_match, ctx, 1)
                    self._bit(self.p_is_rep, state, 1)
                    self._bit(self.p_rep0, state, 1)
                    self._bit(self.p_rep0_1, state, 1)
                    state = 9 if state < cNumLitStates else 11
                    prev_prev, prev_char = prev_char, c
                    pos += 1
                    continue
                self._bit(self.p_is_match, ctx, 0)
                if state < cNumLitStates:
                    pred = (prev_char >> (8 - lit_half)) | ((prev_prev >> (8 - lit_half)) << lit_half)
                    self._sym(self.lit[pred], c)
                else:
                    r0 = d[pos - hist[0]]
                    r1 = d[pos - hist[0] - 1]
                    pred = (r0 >> (8 - dlit_half)) | ((r1 >> (8 - dlit_half)) << dlit_half)
                    self._sym(self.delta_lit[pred], c ^ r0)
                state = s_literal_next_state[state]
                prev_prev, prev_char = prev_char, c
                pos += 1
                continue
            # a match of `length` at distance `dist`: advance, keeping the hash chain fed
            for q in range(pos + 1, min(pos + length, end - 2)):
                if q - pos < 32 or length < 64:
                    self._insert(d, q, head, prev)
            pos += length
            prev_char = d[pos - 1]
            prev_prev = d[pos - 2] if length >= 2 else prev_prev
        # end of block
        ctx = (prev_char >> (8 - cNumIsMatchContextBits)) + (state << cNumIsMatchContextBits)
        self._bit(self.p_is_match, ctx, 1)
        self._bit(self.p_is_rep, state, 0)
        self._sym(self.main, 0)                   # cLZXSpecialCodeEndOfBlockCode
        self._block_end(w)

    def _rep_len(self, lit_state, length):
        model = self.rep_len[0 if lit_state else 1]
        if length <= cMaxMatchLen:
            self._sym(model, length - cMinMatchLen)
        else:
            self._sym(model, cMaxMatchLen - cMinMatchLen + 1)
            self._huge(length)


def compress(data, dict_size_log2=DEFAULT_DICT_SIZE_LOG2, chain=24, lazy=True):
    """A raw LZHAM alpha bitstream for `data` (no zlib header; decompress() reads it)."""
    return compress_stream(data, dict_size_log2, chain, lazy)[2:]


def compress_stream(data, dict_size_log2=DEFAULT_DICT_SIZE_LOG2, chain=24, lazy=True):
    """A zlib-style LZHAM stream (0x5E 0xD1 ... adler32), as in every H1Z1 chunk.

    `chain` is how many earlier positions the match finder tries per byte (more =
    smaller and slower); `lazy` tries one byte later before taking a match."""
    if dict_size_log2 != DEFAULT_DICT_SIZE_LOG2:
        raise ValueError('only the 1 MiB window (dict_size_log2 20) the game uses is written')
    return _Encoder(data, dict_size_log2, chain, lazy).compress()
