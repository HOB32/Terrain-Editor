"""Adaptive data models from lzham_alpha, decode side only.

Straight transcription of:
  lzhamdecomp/lzham_symbol_codec.h
  lzhamdecomp/lzham_symbol_codec.cpp
(richgel999/lzham_alpha, master - the pre-1.0 bitstream ForgeLight uses)

Ported here:
  raw_quasi_adaptive_huffman_data_model -> QuasiAdaptiveHuffmanModel
  adaptive_arith_data_model             -> ArithDataModel

Encoder-only members are absent here (lzcomp._EncModel adds the codes on top of this class):
m_codes / prefix_coding::generate_codes
(only used when m_encoding is true), get_cost(), g_prob_cost[], the copy/assign
plumbing, and the alloca'd scratch tables (Python's generate_*_codes allocate
their own).  m_encoding is hard-wired false throughout.

Everything the DECODER touches is present, including reset_update_rate(), which
lzham_lzdecomp.cpp calls on every table at a reset/sync point.

Bit-exactness notes:
  - 16-bit frequency stores are masked with & 0xFFFF, matching the C's
    static_cast<uint16>.  Python ints do not wrap on their own.
  - All the integer divides in the update-cycle growth are C's >> on unsigned
    values, so >> in Python is identical for the non-negative values involved.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Imports of the sibling modules.  Package-relative first, flat-directory
# second, so this file works both as part of a package and as a loose module.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - import plumbing
    from . import prefix_coding as _prefix_coding
except ImportError:  # pragma: no cover - import plumbing
    import prefix_coding as _prefix_coding

generate_huffman_codes = _prefix_coding.generate_huffman_codes
generate_polar_codes = _prefix_coding.generate_polar_codes
limit_max_code_size = _prefix_coding.limit_max_code_size
DecoderTables = _prefix_coding.DecoderTables

# prefix_coding::cMaxExpectedCodeSize == 16, prefix_coding::cMaxTableBits == 11.
MAX_EXPECTED_CODE_SIZE = _prefix_coding.MAX_EXPECTED_CODE_SIZE
MAX_TABLE_BITS = _prefix_coding.MAX_TABLE_BITS

try:  # pragma: no cover - import plumbing
    from .symbol_codec import BitModel
except ImportError:  # pragma: no cover - import plumbing
    try:
        from symbol_codec import BitModel
    except ImportError:
        # Fallback copy of the agreed interface, so this module is importable
        # (and testable) on its own.  Identical to symbol_codec.BitModel:
        # lzham adaptive_bit_model, cleared to cSymbolCodecArithProbHalfScale.
        class BitModel:  # type: ignore[no-redef]
            __slots__ = ('bit_0_prob',)

            def __init__(self):
                self.bit_0_prob = 1 << 10


# ---------------------------------------------------------------------------
# Constants from lzham_symbol_codec.h
# ---------------------------------------------------------------------------

# cSymbolCodecArithProbBits / Scale / HalfScale / MoveBits
ARITH_PROB_BITS = 11
ARITH_PROB_SCALE = 1 << ARITH_PROB_BITS           # 2048
ARITH_PROB_HALF_SCALE = 1 << (ARITH_PROB_BITS - 1)  # 1024
ARITH_PROB_MOVE_BITS = 5

# #define LZHAM_MORE_FREQUENT_TABLE_UPDATING 1  (lzham_symbol_codec.cpp, top)
# "Set to 1 to enable ~2x more frequent Huffman table updating (at slower
# decompression)."  It is 1 in the shipped alpha source, and it changes
# m_max_cycle, so it must match the encoder that produced the stream.
LZHAM_MORE_FREQUENT_TABLE_UPDATING = 1

_MAX_CYCLE_LIMIT = 32767  # m_max_cycle = LZHAM_MIN(m_max_cycle, 32767)
_UINT16_MAX = 0xFFFF


# ---------------------------------------------------------------------------
# lzham_math.h helpers (only the ones these models need)
# ---------------------------------------------------------------------------

def floor_log2i(v: int) -> int:
    """math::floor_log2i"""
    l = 0
    while v > 1:
        v >>= 1
        l += 1
    return l


def ceil_log2i(v: int) -> int:
    """math::ceil_log2i (cIntBits == 32; v here is always well under 1<<32)."""
    l = floor_log2i(v)
    if l != 32 and v > (1 << l):
        l += 1
    return l


def is_power_of_2(x: int) -> bool:
    """math::is_power_of_2"""
    return bool(x) and (x & (x - 1)) == 0


def next_pow2(val: int) -> int:
    """math::next_pow2 - unchanged if already a power of 2."""
    val -= 1
    val |= val >> 16
    val |= val >> 8
    val |= val >> 4
    val |= val >> 2
    val |= val >> 1
    return (val + 1) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# raw_quasi_adaptive_huffman_data_model
# ---------------------------------------------------------------------------

class QuasiAdaptiveHuffmanModel:
    """lzham::raw_quasi_adaptive_huffman_data_model, decoding side.

    Usage from the decoder (this is exactly what symbol_codec::decode does):

        sym = <huffman-decode using model.decode_tables / model.code_sizes>
        model.update(sym)        # freq++, tick the counter, rebuild on 0

    A decoder that inlines the freq bump itself (as the C does) should instead
    do the ++/-- on model.sym_freq / model.symbols_until_update and call
    model.update_tables() when the counter reaches zero.  Both routes are
    bit-identical.
    """

    def __init__(self, total_syms: int, fast_updating: bool,
                 use_polar_codes: bool, initial_sym_freq=None):
        self.clear()
        if total_syms:
            self.init(total_syms, fast_updating, use_polar_codes,
                      initial_sym_freq)

    # -- clear() ------------------------------------------------------------
    def clear(self) -> None:
        self.sym_freq: list[int] = []          # m_sym_freq       (uint16[])
        self.initial_sym_freq: list[int] = []  # m_initial_sym_freq
        self.code_sizes: list[int] = []        # m_code_sizes     (uint8[])
        self.decode_tables = None              # m_pDecode_tables
        self.total_syms = 0                    # m_total_syms
        self.max_cycle = 0                     # m_max_cycle
        self.update_cycle = 0                  # m_update_cycle
        self.symbols_until_update = 0          # m_symbols_until_update
        self.total_count = 0                   # m_total_count
        self.decoder_table_bits = 0            # m_decoder_table_bits
        self.fast_updating = False             # m_fast_updating
        self.use_polar_codes = False           # m_use_polar_codes
        # m_encoding is always false here (decode-only port).

    # -- init() -------------------------------------------------------------
    def init(self, total_syms: int, fast_updating: bool,
             use_polar_codes: bool, initial_sym_freq=None) -> None:
        self.fast_updating = bool(fast_updating)
        self.use_polar_codes = bool(use_polar_codes)
        self.symbols_until_update = 0

        self.sym_freq = [0] * total_syms

        if initial_sym_freq is not None:
            # memcpy(m_initial_sym_freq.begin(), pInitial_sym_freq, ...)
            # NOTE: the alpha decompressor never passes pInitial_sym_freq - every
            # lzham_lzdecomp.cpp init() call uses the 4-argument form, so all
            # frequencies start at 1 for real streams.  Supported here anyway.
            # (The C memcpy here has a length bug - it passes
            #  total_syms * m_initial_sym_freq.size_in_bytes(), i.e. total_syms
            #  times too many bytes.  Dead code in the alpha decompressor, so it
            #  never bites; this port copies the total_syms entries it means to.)
            self.initial_sym_freq = [f & _UINT16_MAX for f in initial_sym_freq]
            if len(self.initial_sym_freq) != total_syms:
                raise ValueError('initial_sym_freq must have total_syms entries')
        else:
            self.initial_sym_freq = []

        self.code_sizes = [0] * total_syms

        self.total_syms = total_syms

        # if (m_total_syms <= 16) m_decoder_table_bits = 0;
        # else m_decoder_table_bits = min(1 + ceil_log2i(m_total_syms), cMaxTableBits);
        if self.total_syms <= 16:
            self.decoder_table_bits = 0
        else:
            self.decoder_table_bits = min(1 + ceil_log2i(self.total_syms),
                                          MAX_TABLE_BITS)

        # TODO (in the C): "Make this setting a user controllable parameter?"
        if self.fast_updating:
            self.max_cycle = (max(64, self.total_syms) + 6) << 5
        elif LZHAM_MORE_FREQUENT_TABLE_UPDATING:
            self.max_cycle = (max(24, self.total_syms) + 6) * 12
        else:
            self.max_cycle = (max(32, self.total_syms) + 6) * 16

        self.max_cycle = min(self.max_cycle, _MAX_CYCLE_LIMIT)

        self.reset()

    # -- reset() ------------------------------------------------------------
    def reset(self) -> None:
        if not self.total_syms:
            return

        if self.initial_sym_freq:
            self.update_cycle = 0
            for i in range(self.total_syms):
                sym_freq = self.initial_sym_freq[i]
                self.sym_freq[i] = sym_freq & _UINT16_MAX
                self.update_cycle += sym_freq
        else:
            for i in range(self.total_syms):
                self.sym_freq[i] = 1
            self.update_cycle = self.total_syms

        self.total_count = 0
        self.symbols_until_update = 0

        # First table build.  This is where update_cycle first grows, but reset()
        # throws that result away on the very next line - so after reset() the
        # cycle is always 8, whatever total_syms is.
        self.update_tables()

        self.symbols_until_update = self.update_cycle = 8

    # -- rescale() ----------------------------------------------------------
    def rescale(self) -> None:
        """Halve every frequency, rounding up: freq = (freq + 1) >> 1.

        Rounding up is what keeps a frequency of 1 at 1 - no symbol can ever
        fall out of the code (1->1, 2->1, 3->2, ...).
        """
        total_freq = 0
        for i in range(self.total_syms):
            freq = (self.sym_freq[i] + 1) >> 1
            total_freq += freq
            self.sym_freq[i] = freq & _UINT16_MAX
        self.total_count = total_freq

    # -- reset_update_rate() ------------------------------------------------
    def reset_update_rate(self) -> None:
        """Called by the decompressor at a reset/sync point.

        Folds the symbols seen so far in the current (unfinished) cycle into
        total_count, optionally rescales, and drops the cycle back to 8 so the
        model re-adapts quickly.  It does NOT rebuild the tables.
        """
        self.total_count += (self.update_cycle - self.symbols_until_update)

        # #ifdef _DEBUG: LZHAM_ASSERT(actual_total == m_total_count)
        assert sum(self.sym_freq) == self.total_count, (
            'sym_freq sum %d != total_count %d'
            % (sum(self.sym_freq), self.total_count))

        if self.total_count > self.total_syms:
            self.rescale()

        self.symbols_until_update = self.update_cycle = min(8, self.update_cycle)

    # -- update() [no-arg overload]: rebuild the tables ---------------------
    def update_tables(self) -> None:
        """raw_quasi_adaptive_huffman_data_model::update() - the table rebuild.

        Called when symbols_until_update hits 0, i.e. AFTER the frequency of the
        triggering symbol has already been bumped.  So the symbol that closes a
        cycle is decoded with the OLD tables and counted into the NEW ones.
        """
        assert self.symbols_until_update == 0
        self.total_count += self.update_cycle
        assert self.total_count <= 65535, 'total_count overflow: %d' % self.total_count

        while self.total_count >= 32768:
            self.rescale()

        if self.use_polar_codes:
            code_sizes, max_code_size, total_freq = generate_polar_codes(
                self.sym_freq, self.total_syms)
        else:
            code_sizes, max_code_size, total_freq = generate_huffman_codes(
                self.sym_freq, self.total_syms)

        # LZHAM_ASSERT(total_freq == m_total_count);
        # if ((!status) || (total_freq != m_total_count)) return false;
        if total_freq != self.total_count:
            raise ValueError('total_freq %d != total_count %d'
                             % (total_freq, self.total_count))

        self.code_sizes = list(code_sizes)

        if max_code_size > MAX_EXPECTED_CODE_SIZE:
            self.code_sizes = list(limit_max_code_size(
                self.total_syms, self.code_sizes, MAX_EXPECTED_CODE_SIZE))

        # Decode side only: prefix_coding::generate_decoder_tables(...).
        # (The encoder branch, generate_codes() into m_codes, is not ported.)
        self.decode_tables = DecoderTables(self.total_syms, self.code_sizes,
                                           self.decoder_table_bits)

        # The quasi-adaptive part: the cycle grows, so tables get rebuilt less
        # and less often as the stream goes on.  x2 when fast_updating,
        # otherwise x1.25 with a truncating shift - (5 * cycle) >> 2.
        if self.fast_updating:
            self.update_cycle = 2 * self.update_cycle
        else:
            self.update_cycle = (5 * self.update_cycle) >> 2

        if self.update_cycle > self.max_cycle:
            self.update_cycle = self.max_cycle

        self.symbols_until_update = self.update_cycle

    # -- update(sym) --------------------------------------------------------
    def update(self, sym: int) -> None:
        """raw_quasi_adaptive_huffman_data_model::update(uint sym)."""
        freq = self.sym_freq[sym]
        freq += 1
        self.sym_freq[sym] = freq & _UINT16_MAX
        assert freq <= _UINT16_MAX

        self.symbols_until_update -= 1
        if self.symbols_until_update == 0:
            self.update_tables()

    # -- convenience --------------------------------------------------------
    def get_total_syms(self) -> int:
        return self.total_syms

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return ('QuasiAdaptiveHuffmanModel(total_syms=%d, fast=%r, polar=%r, '
                'table_bits=%d, max_cycle=%d, update_cycle=%d, '
                'symbols_until_update=%d, total_count=%d)'
                % (self.total_syms, self.fast_updating, self.use_polar_codes,
                   self.decoder_table_bits, self.max_cycle, self.update_cycle,
                   self.symbols_until_update, self.total_count))


# ---------------------------------------------------------------------------
# adaptive_arith_data_model
# ---------------------------------------------------------------------------

class ArithDataModel:
    """lzham::adaptive_arith_data_model - a binary tree of adaptive_bit_model.

    The C source says: "This class is not actually used by LZHAM - it's only
    here for comparison/experimental purposes."  Ported for completeness; the
    LZHAM decompressor never instantiates one, so it is on no hot path and is
    not exercised by real chunk data.
    """

    def __init__(self, total_syms: int):
        self.total_syms = 0       # m_total_syms
        self.probs: list = []     # m_probs
        self.init(total_syms)

    def clear(self) -> None:
        self.total_syms = 0
        self.probs = []

    def init(self, total_syms: int) -> None:
        if not total_syms:
            self.clear()
            return

        # if ((total_syms < 2) || (!is_power_of_2(total_syms)))
        #    total_syms = next_pow2(total_syms);
        if (total_syms < 2) or (not is_power_of_2(total_syms)):
            total_syms = next_pow2(total_syms)

        self.total_syms = total_syms
        # m_probs.try_resize(m_total_syms) - index 0 is unused; the tree uses
        # 1..total_syms-1 for internal nodes.
        self.probs = [BitModel() for _ in range(self.total_syms)]

    def reset(self) -> None:
        # for (i) m_probs[i].clear();  clear() == half scale
        for p in self.probs:
            p.bit_0_prob = ARITH_PROB_HALF_SCALE

    def reset_update_rate(self) -> None:
        # Empty in the C.
        pass

    def decode(self, codec) -> int:
        """symbol_codec::decode(adaptive_arith_data_model& model).

        Walks the tree with codec.decode_bit(), which updates each bit model it
        passes through (update_model defaults to true in the C).
        """
        node = 1
        while True:
            bit = codec.decode_bit(self.probs[node])
            node = (node << 1) + bit
            if node >= self.total_syms:
                break
        return node - self.total_syms

    def update(self, sym: int) -> None:
        """adaptive_arith_data_model::update(uint sym) - encode-side model bump.

        Kept because it is part of the agreed interface.  A decoder does not
        need it: decode() already updates the models as it descends.
        """
        node = 1
        bitmask = self.total_syms
        while True:
            bitmask >>= 1
            bit = 1 if (sym & bitmask) else 0
            # adaptive_bit_model::update(bit), inlined
            p = self.probs[node]
            if not bit:
                p.bit_0_prob += ((ARITH_PROB_SCALE - p.bit_0_prob)
                                 >> ARITH_PROB_MOVE_BITS)
            else:
                p.bit_0_prob -= (p.bit_0_prob >> ARITH_PROB_MOVE_BITS)
            assert 1 <= p.bit_0_prob < ARITH_PROB_SCALE
            node = (node << 1) + bit
            if bitmask <= 1:
                break

    def get_total_syms(self) -> int:
        return self.total_syms
