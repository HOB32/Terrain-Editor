"""Pure-Python port of lzham_alpha's LZ decompressor state machine.

Source: https://github.com/richgel999/lzham_alpha (master branch)
  lzhamdecomp/lzham_lzdecomp.cpp          -> decompress() below
  lzhamdecomp/lzham_lzdecompbase.h/.cpp   -> the constants + LZDecompBase

This is the *pre-1.0 alpha* bitstream, which is what ForgeLight / H1Z1
terrain chunks use.  This is the reference decoder; the encoder is lzcomp.py (its mirror
image), the faster fused decoder is fastdecomp.py, and FORMAT.md walks the format.

Two structural deviations from the C, both deliberate and both explained
where they occur:

1.  The C decompress() is a coroutine (LZHAM_CR_BEGIN / LZHAM_CR_RETURN /
    the LZHAM_SAVE_LOCAL_STATE macro soup) so that it can be suspended when
    the input buffer runs dry or the output dictionary fills up.  We always
    have the whole input in memory and we build the whole output in memory,
    so this is written as a straight loop.  Every LZHAM_CR_RETURN in the C
    is either "give me more input" (impossible here), "take this output"
    (we just keep appending), or "here is the final status" (we return /
    raise).  No state-machine variables are needed.

2.  We port the ``unbuffered`` template instantiation (``decompress<true>``),
    which is the one ``lzham_lib_decompress_memory()`` forces via
    LZHAM_DECOMP_FLAG_OUTPUT_UNBUFFERED.  In that mode
    ``dict_size_mask == UINT_MAX``, ``pDst`` is the caller's whole output
    buffer, and there is no dictionary wrap and no output flushing.  That
    makes the window an infinite window, which is a strict superset of the
    ring buffer the buffered path uses: the encoder never emits a distance
    >= dict_size, so every match that the buffered path would satisfy by
    wrapping we satisfy with a plain backwards index.  All the
    ``if (!unbuffered)`` branches of the C (seed bytes,
    LZHAM_FLUSH_OUTPUT_BUFFER, the byte-at-a-time wrapping match copy) are
    therefore dead here and are marked as such rather than transcribed.

dict_size_log2 is NOT in the LZHAM bitstream - the caller supplies it.  For
a zlib-style stream it *is* recoverable from the CMF byte; see
``dict_size_log2_from_cmf()`` and DEFAULT_DICT_SIZE_LOG2 below.

VERIFIED END TO END: all 14 real H1Z1 chunk payloads in scratch vectors/
(CNK0..CNK5 and CTG0, 13 KiB..305 KiB compressed, 117 KiB..1000 KiB
uncompressed) decode to exactly the length in the chunk header AND their
trailing adler32 matches zlib.adler32 of the output.  dict_size_log2 = 20
is uniquely correct: 15, 18, 19, 21 and 22 all fail inside the first six
output bytes, because m_num_lzx_slots changes m_main_table's symbol count
and desynchronises the very first Huffman table.

Also verified empirically: handing this function ``payload[2:]`` (the 2-byte
zlib-style header stripped) is equivalent to letting the codec read those
two bytes itself, which is what the C does.  The bit buffer starts empty and
byte-aligned, so the C's two 8-bit reads consume exactly bytes 0 and 1 and
leave it byte-aligned again.  Passing the un-stripped payload fails
immediately, as it must.

Depends on ``symbol_codec`` (SymbolCodec, BitModel) and ``models``
(QuasiAdaptiveHuffmanModel, ArithDataModel), which are separate modules.
Python 3.14, standard library only.
"""

from __future__ import annotations

import zlib

try:  # works both as a package module and as a flat directory of modules
    from .symbol_codec import SymbolCodec, BitModel
    from .models import QuasiAdaptiveHuffmanModel, ArithDataModel
except ImportError:  # pragma: no cover - flat layout
    from symbol_codec import SymbolCodec, BitModel
    from models import QuasiAdaptiveHuffmanModel, ArithDataModel


__all__ = [
    "LzhamDecompressError",
    "LZDecompBase",
    "decompress",
    "decompress_stream",
    "dict_size_log2_from_cmf",
    "DEFAULT_DICT_SIZE_LOG2",
]


class LzhamDecompressError(Exception):
    """Raised instead of returning one of the LZHAM_DECOMP_STATUS_FAILED_* codes."""


# ---------------------------------------------------------------------------
# lzham_lzdecompbase.h  --  struct CLZDecompBase enums, transcribed verbatim
# ---------------------------------------------------------------------------

cMinMatchLen = 2               # cMinMatchLen = 2U
cMaxMatchLen = 257             # cMaxMatchLen = 257U
cMaxHugeMatchLen = 65536       # cMaxHugeMatchLen = 65536

cMinDictSizeLog2 = 15          # cMinDictSizeLog2 = 15
cMaxDictSizeLog2 = 29          # cMaxDictSizeLog2 = 29  (== LZHAM_MAX_DICT_SIZE_LOG2_X64)

cMatchHistSize = 4             # cMatchHistSize = 4
cMaxLen2MatchDist = 2047       # cMaxLen2MatchDist = 2047

cLZXNumSecondaryLengths = 249  # cLZXNumSecondaryLengths = 249
cNumHugeMatchCodes = 1         # cNumHugeMatchCodes = 1
cMaxHugeMatchCodeBits = 16     # cMaxHugeMatchCodeBits = 16
cLZXNumSpecialLengths = 2      # cLZXNumSpecialLengths = 2
cLZXLowestUsableMatchSlot = 1  # cLZXLowestUsableMatchSlot = 1
cLZXMaxPositionSlots = 128     # cLZXMaxPositionSlots = 128

cLZXSpecialCodeEndOfBlockCode = 0     # cLZXSpecialCodeEndOfBlockCode = 0
cLZXSpecialCodePartialStateReset = 1  # cLZXSpecialCodePartialStateReset = 1

# Only emitted/expected when the C is built with LZHAM_LZDEBUG defined, which
# release ForgeLight data is not.  Kept because the task asks for them, and
# because the two bare literals in the C (166 for the outer marker, 366 for
# the end-of-block marker) are easy to lose.
cLZHAMDebugSyncMarkerValue = 666       # cLZHAMDebugSyncMarkerValue = 666
cLZHAMDebugSyncMarkerBits = 12         # cLZHAMDebugSyncMarkerBits = 12
cLZHAMDebugOuterSyncMarkerValue = 166  # hardcoded 166 in the outer block loop
cLZHAMDebugEndSyncMarkerValue = 366    # hardcoded 366 after a comp block

cBlockHeaderBits = 2           # cBlockHeaderBits = 2
cBlockCheckBits = 4            # cBlockCheckBits = 4  (declared, unused by the decoder)
cBlockFlushTypeBits = 2        # cBlockFlushTypeBits = 2

cSyncBlock = 0                 # cSyncBlock = 0
cCompBlock = 1                 # cCompBlock = 1
cRawBlock = 2                  # cRawBlock = 2
cEOFBlock = 3                  # cEOFBlock = 3

cNumStates = 12                # cNumStates = 12
cNumLitStates = 7              # cNumLitStates = 7
cNumLitPredBits = 6            # cNumLitPredBits = 6       (must be even)
cNumDeltaLitPredBits = 6       # cNumDeltaLitPredBits = 6  (must be even)
cNumIsMatchContextBits = 6     # cNumIsMatchContextBits = 6

# #define LZHAM_USE_ALL_ARITHMETIC_CODING 0
LZHAM_USE_ALL_ARITHMETIC_CODING = 0

# include/lzham.h: #define LZHAM_Z_LZHAM 14
LZHAM_Z_LZHAM = 14
# lzham_checksum.h: const uint cInitAdler32 = 1U;
cInitAdler32 = 1


def is_match_model_index(prev_char: int, cur_state: int) -> int:
    """LZHAM_IS_MATCH_MODEL_INDEX(prev_char, cur_state).

    #define LZHAM_IS_MATCH_MODEL_INDEX(prev_char, cur_state) \
       ((prev_char) >> (8 - CLZDecompBase::cNumIsMatchContextBits)) + \
       ((cur_state) << CLZDecompBase::cNumIsMatchContextBits)
    """
    return (prev_char >> (8 - cNumIsMatchContextBits)) + (cur_state << cNumIsMatchContextBits)


# lzham_lzdecomp.cpp: static const uint8 s_literal_next_state[24]
s_literal_next_state = (
    0, 0, 0, 0, 1, 2, 3,          # 0-6: literal states
    4, 5, 6, 4, 5,                # 7-11: match states
    7, 7, 7, 7, 7, 7, 7, 10, 10, 10, 10, 10,   # 12-23: unused
)
assert len(s_literal_next_state) == 24

# static const uint s_huge_match_base_len[4] =
#   { cMaxMatchLen + 1, cMaxMatchLen + 1 + 256,
#     cMaxMatchLen + 1 + 256 + 1024, cMaxMatchLen + 1 + 256 + 1024 + 4096 };
s_huge_match_base_len = (
    cMaxMatchLen + 1,
    cMaxMatchLen + 1 + 256,
    cMaxMatchLen + 1 + 256 + 1024,
    cMaxMatchLen + 1 + 256 + 1024 + 4096,
)
# static const uint8 s_huge_match_code_len[4] = { 8, 10, 12, 16 };
s_huge_match_code_len = (8, 10, 12, 16)


# ---------------------------------------------------------------------------
# lzham_lzdecompbase.cpp  --  CLZDecompBase::init_position_slots()
# ---------------------------------------------------------------------------

class LZDecompBase:
    """CLZDecompBase.  Builds the LZX position-slot tables at init time.

    Transcribed from lzham_lzdecompbase.cpp.  Nothing is hardcoded; the
    builder below is the only source of the tables.
    """

    __slots__ = (
        "m_dict_size_log2", "m_dict_size", "m_num_lzx_slots",
        "m_lzx_position_base", "m_lzx_position_extra_mask",
        "m_lzx_position_extra_bits",
    )

    def __init__(self, dict_size_log2: int):
        self.init_position_slots(dict_size_log2)

    def init_position_slots(self, dict_size_log2: int) -> None:
        self.m_dict_size_log2 = dict_size_log2
        self.m_dict_size = 1 << dict_size_log2

        extra_bits = [0] * cLZXMaxPositionSlots   # m_lzx_position_extra_bits (uint8)
        base = [0] * cLZXMaxPositionSlots         # m_lzx_position_base       (uint)
        extra_mask = [0] * cLZXMaxPositionSlots   # m_lzx_position_extra_mask (uint)

        # for (i = 0, j = 0; i < cLZXMaxPositionSlots; i += 2)
        # {
        #    m_lzx_position_extra_bits[i]     = (uint8)j;
        #    m_lzx_position_extra_bits[i + 1] = (uint8)j;
        #    if ((i != 0) && (j < 25)) j++;
        # }
        j = 0
        for i in range(0, cLZXMaxPositionSlots, 2):
            extra_bits[i] = j
            extra_bits[i + 1] = j
            if (i != 0) and (j < 25):
                j += 1

        # for (i = 0, j = 0; i < cLZXMaxPositionSlots; i++)
        # {
        #    m_lzx_position_base[i] = j;
        #    m_lzx_position_extra_mask[i] = (1 << m_lzx_position_extra_bits[i]) - 1;
        #    j += (1 << m_lzx_position_extra_bits[i]);
        # }
        j = 0
        for i in range(cLZXMaxPositionSlots):
            base[i] = j & 0xFFFFFFFF
            extra_mask[i] = (1 << extra_bits[i]) - 1
            j += 1 << extra_bits[i]

        self.m_lzx_position_extra_bits = extra_bits
        self.m_lzx_position_base = base
        self.m_lzx_position_extra_mask = extra_mask

        # m_num_lzx_slots = 0;
        # const uint largest_dist = m_dict_size - 1;
        # for (i = 0; i < cLZXMaxPositionSlots; i++)
        #    if ((largest_dist >= base[i]) && (largest_dist < (base[i] + (1 << eb[i]))))
        #       { m_num_lzx_slots = i + 1; break; }
        # LZHAM_VERIFY(m_num_lzx_slots);
        self.m_num_lzx_slots = 0
        largest_dist = self.m_dict_size - 1
        for i in range(cLZXMaxPositionSlots):
            if base[i] <= largest_dist < base[i] + (1 << extra_bits[i]):
                self.m_num_lzx_slots = i + 1
                break
        if not self.m_num_lzx_slots:
            raise LzhamDecompressError(
                "init_position_slots: no slot covers dict_size_log2=%d" % dict_size_log2)


# ---------------------------------------------------------------------------
# zlib-style header helpers
# ---------------------------------------------------------------------------

# lzhamcomp/lzham_lzcomp_internal.cpp, lzcompressor::send_zlib_header():
#     // Method is set to 14 (LZHAM) and CINFO is (window_size - 15).
#     int cmf = LZHAM_Z_LZHAM | ((m_params.m_dict_size_log2 - 15) << 4);
# so CINFO (the high nibble of CMF) == dict_size_log2 - 15, and the window
# size IS recoverable from the stream after all - for zlib-wrapped streams.
def dict_size_log2_from_cmf(cmf: int) -> int:
    """Recover dict_size_log2 from a zlib-style CMF byte."""
    return ((cmf >> 4) & 15) + 15


# Every H1Z1 chunk payload starts 0x5E 0xD1:
#   CM    = 0x5E & 15 = 14 = LZHAM_Z_LZHAM
#   CINFO = 0x5E >> 4 = 5   -> dict_size_log2 = 5 + 15 = 20  (1 MiB window)
#   FLG   = 0xD1: FLEVEL = 3, FDICT = 0, and (0x5E<<8 | 0xD1) % 31 == 0
# (0xD1 also cross-checks: flg starts at 3<<6 = 192, check = (0x5E*256+192)%31
#  = 14, so flg += 31-14 = 17 -> 209 = 0xD1.  Exactly what the alpha encoder
#  writes at LZHAM_COMP_LEVEL_UBER.)
DEFAULT_DICT_SIZE_LOG2 = 20


# ---------------------------------------------------------------------------
# model helpers
# ---------------------------------------------------------------------------

def _new_sym_model(total_syms: int, fast_updating: bool, use_polar_codes: bool):
    """m_xxx_table.init(false /*encoding*/, total_syms, fast_updating, use_polar_codes).

    #if LZHAM_USE_ALL_ARITHMETIC_CODING
    #   typedef adaptive_arith_data_model sym_data_model;
    #else
    #   typedef quasi_adaptive_huffman_data_model sym_data_model;
    #endif
    LZHAM_USE_ALL_ARITHMETIC_CODING is 0 in lzham_lzdecompbase.h, so the real
    bitstream always uses the quasi-adaptive Huffman models.
    """
    if LZHAM_USE_ALL_ARITHMETIC_CODING:
        return ArithDataModel(total_syms)
    return QuasiAdaptiveHuffmanModel(total_syms, fast_updating, use_polar_codes)


def _decode_sym(codec, model) -> int:
    """LZHAM_DECOMPRESS_DECODE_ADAPTIVE_SYMBOL(codec, result, model).

    Note: symbol_codec::decode(quasi_adaptive_huffman_data_model&) already does
    the m_sym_freq[sym]++ bump and the "--m_symbols_until_update == 0 -> update()"
    rebuild.  So this module must NOT also call model.update(sym); that overload
    exists in the C for the *encoder's* benefit.
    """
    if LZHAM_USE_ALL_ARITHMETIC_CODING:
        return model.decode(codec)
    return codec.decode_symbol(model)


def _reset_update_rate(model) -> None:
    """raw_quasi_adaptive_huffman_data_model::reset_update_rate().

    Preferred path is the model's own method.  The stated models.py interface
    does not list reset_update_rate()/rescale()/max_cycle, so a transcribed
    fallback is provided against the attributes the interface does list.

        void reset_update_rate()
        {
           m_total_count += (m_update_cycle - m_symbols_until_update);
           if (m_total_count > m_total_syms) rescale();
           m_symbols_until_update = m_update_cycle = LZHAM_MIN(8, m_update_cycle);
        }
        void rescale()
        {
           uint total_freq = 0;
           for (i = 0; i < m_total_syms; i++) {
              uint freq = (m_sym_freq[i] + 1) >> 1;
              total_freq += freq; m_sym_freq[i] = (uint16)freq;
           }
           m_total_count = total_freq;
        }
    """
    fn = getattr(model, "reset_update_rate", None)
    if fn is not None:
        fn()
        return

    model.total_count = (model.total_count + (model.update_cycle - model.symbols_until_update)) & 0xFFFFFFFF
    if model.total_count > model.total_syms:
        rescale = getattr(model, "rescale", None)
        if rescale is not None:
            rescale()
        else:
            total_freq = 0
            freqs = model.sym_freq
            for i in range(model.total_syms):
                f = (freqs[i] + 1) >> 1
                total_freq += f
                freqs[i] = f
            model.total_count = total_freq
    model.update_cycle = min(8, model.update_cycle)
    model.symbols_until_update = model.update_cycle


def _decode_huge_match_len(codec) -> int:
    """The "huge" match length escape, which appears verbatim three times in the C.

        match_len = 0;
        do {
           uint b; GET_BITS(codec, b, 1);
           if (!b) break;
           match_len++;
        } while (match_len < 3);
        uint k; GET_BITS(codec, k, s_huge_match_code_len[match_len]);
        match_len = s_huge_match_base_len[match_len] + k;
    """
    n = 0
    while True:
        b = codec.decode_bits(1)
        if not b:
            break
        n += 1
        if n >= 3:
            break
    k = codec.decode_bits(s_huge_match_code_len[n])
    return s_huge_match_base_len[n] + k


# ---------------------------------------------------------------------------
# the decompressor
# ---------------------------------------------------------------------------

def decompress(data: bytes, dict_size_log2: int,
               uncompressed_size: int | None = None) -> bytes:
    """Decode a raw LZHAM alpha bitstream.

    ``data`` starts at the LZHAM bitstream, i.e. AFTER any 2-byte zlib-style
    header - the caller strips it (decompress_stream() does).

    ``dict_size_log2`` must be in [cMinDictSizeLog2, cMaxDictSizeLog2].  It is
    not in the bitstream; see decompress_stream()/DEFAULT_DICT_SIZE_LOG2.

    ``uncompressed_size``, when given, is used exactly the way the C uses
    ``out_buf_size`` in unbuffered mode: as the bound whose violation means
    LZHAM_DECOMP_STATUS_FAILED_DEST_BUF_TOO_SMALL / FAILED_BAD_CODE.  It is
    also checked against the final length.

    The trailing adler32 that the stream carries is verified here (we always
    have the whole output, so the C's LZHAM_DECOMP_FLAG_COMPUTE_ADLER32 path
    is always taken).
    """
    out, _adler = _decompress_impl(data, dict_size_log2, uncompressed_size)
    return bytes(out)


def _decompress_impl(data: bytes, dict_size_log2: int,
                     uncompressed_size: int | None = None):
    """The port of lzham_decompressor::decompress<true>().  Returns (bytearray, adler32)."""

    # check_params()
    if not (cMinDictSizeLog2 <= dict_size_log2 <= cMaxDictSizeLog2):
        raise LzhamDecompressError(
            "dict_size_log2 %r out of range [%d, %d]"
            % (dict_size_log2, cMinDictSizeLog2, cMaxDictSizeLog2))
    if not data:
        # symbol_codec::start_decoding() returns false for an empty buffer
        raise LzhamDecompressError("FAILED_INITIALIZING: empty input")

    # lzham_decompressor::init(): m_lzBase.init_position_slots(m_params.m_dict_size_log2)
    lz_base = LZDecompBase(dict_size_log2)

    # const uint dict_size = 1U << m_params.m_dict_size_log2;
    # const uint dict_size_mask = unbuffered ? UINT_MAX : (dict_size - 1);
    # unbuffered: the mask is UINT_MAX, i.e. every "& dict_size_mask" below is a
    # no-op on a value we have already bounds-checked.  dict_size itself is only
    # used by the (dead) flushing paths.

    # m_codec.start_decoding(m_pIn_buf, *m_pIn_buf_size, ...)
    codec = SymbolCodec(data)

    # pDst / pDst_end / dst_ofs.  dst_ofs is always len(out); kept explicit to
    # stay readable against the C.
    out = bytearray()
    dst_ofs = 0
    out_buf_size = uncompressed_size   # None == unbounded

    # int match_hist0 = 0, match_hist1 = 0, match_hist2 = 0, match_hist3 = 0;
    # uint cur_state = 0, prev_char = 0, prev_prev_char = 0, dst_ofs = 0;
    match_hist0 = match_hist1 = match_hist2 = match_hist3 = 0
    cur_state = 0
    prev_char = 0
    prev_prev_char = 0

    # --- the header LZHAM writes at the start of its own stream -------------
    # NOTE ON THE 2-BYTE ZLIB HEADER: when built with
    # LZHAM_DECOMP_FLAG_READ_ZLIB_STREAM the C reads CMF and FLG here, with
    # LZHAM_SYMBOL_CODEC_DECODE_GET_BITS(codec, x, 8) twice, from this same
    # bitstream.  The bit buffer is empty and byte-aligned at that point, so
    # those two 8-bit reads consume exactly bytes 0 and 1 and leave the codec
    # byte-aligned again.  Stripping the two bytes before constructing the
    # codec is therefore bit-identical, which is why this function takes the
    # post-header slice.  (The FDICT/seed-bytes branch that follows it in the
    # C needs a preset dictionary; H1Z1 chunks have FLG=0xD1, FDICT clear.)
    #
    # {
    #    uint tmp;
    #    LZHAM_SYMBOL_CODEC_DECODE_GET_BITS(codec, tmp, 2);
    #    fast_table_updating = (tmp & 2) != 0;
    #    use_polar_codes     = (tmp & 1) != 0;
    # }
    tmp = codec.decode_bits(2)
    fast_table_updating = (tmp & 2) != 0
    use_polar_codes = (tmp & 1) != 0

    # --- the model set, exactly as lzham_lzdecomp.cpp builds it -------------
    # sym_data_model m_lit_table[1 << cNumLitPredBits];              64 tables
    #   .init(false, 256, ...)                                       256 syms
    # sym_data_model m_delta_lit_table[1 << cNumDeltaLitPredBits];   64 tables
    #   .init(false, 256, ...)                                       256 syms
    # sym_data_model m_main_table;
    #   .init(false, cLZXNumSpecialLengths + (m_num_lzx_slots - cLZXLowestUsableMatchSlot) * 8, ...)
    # sym_data_model m_rep_len_table[2];
    #   .init(false, cNumHugeMatchCodes + (cMaxMatchLen - cMinMatchLen + 1), ...)   == 1 + 256 = 257
    # sym_data_model m_large_len_table[2];
    #   .init(false, cNumHugeMatchCodes + cLZXNumSecondaryLengths, ...)             == 1 + 249 = 250
    # sym_data_model m_dist_lsb_table;
    #   .init(false, 16, ...)                                        16 syms
    #
    # The C inits m_lit_table[0] then assign()s it into [1..63] (a copy).  init()
    # is deterministic given (total_syms, fast_updating, use_polar_codes), so
    # constructing 64 fresh identical models is equivalent.
    n_lit_pred = 1 << cNumLitPredBits            # 64
    n_delta_lit_pred = 1 << cNumDeltaLitPredBits  # 64

    lit_table = [_new_sym_model(256, fast_table_updating, use_polar_codes)
                 for _ in range(n_lit_pred)]
    delta_lit_table = [_new_sym_model(256, fast_table_updating, use_polar_codes)
                       for _ in range(n_delta_lit_pred)]
    main_table = _new_sym_model(
        cLZXNumSpecialLengths + (lz_base.m_num_lzx_slots - cLZXLowestUsableMatchSlot) * 8,
        fast_table_updating, use_polar_codes)
    rep_len_table = [
        _new_sym_model(cNumHugeMatchCodes + (cMaxMatchLen - cMinMatchLen + 1),
                       fast_table_updating, use_polar_codes)
        for _ in range(2)]
    large_len_table = [
        _new_sym_model(cNumHugeMatchCodes + cLZXNumSecondaryLengths,
                       fast_table_updating, use_polar_codes)
        for _ in range(2)]
    dist_lsb_table = _new_sym_model(16, fast_table_updating, use_polar_codes)

    # adaptive_bit_model m_is_match_model[cNumStates * (1 << cNumIsMatchContextBits)];  12*64 = 768
    # adaptive_bit_model m_is_rep_model[cNumStates];                 12
    # adaptive_bit_model m_is_rep0_model[cNumStates];                12
    # adaptive_bit_model m_is_rep0_single_byte_model[cNumStates];    12
    # adaptive_bit_model m_is_rep1_model[cNumStates];                12
    # adaptive_bit_model m_is_rep2_model[cNumStates];                12
    # ...each .clear()ed, i.e. bit_0_prob = 1 << (cSymbolCodecArithProbBits - 1)
    # == 1 << 10, which is what BitModel.__init__ already sets.
    is_match_model = [BitModel() for _ in range(cNumStates * (1 << cNumIsMatchContextBits))]
    is_rep_model = [BitModel() for _ in range(cNumStates)]
    is_rep0_model = [BitModel() for _ in range(cNumStates)]
    is_rep0_single_byte_model = [BitModel() for _ in range(cNumStates)]
    is_rep1_model = [BitModel() for _ in range(cNumStates)]
    is_rep2_model = [BitModel() for _ in range(cNumStates)]

    def reset_all_tables():
        """lzham_decompressor::reset_all_tables()."""
        for m in lit_table:
            m.reset()
        for m in delta_lit_table:
            m.reset()
        main_table.reset()
        for m in rep_len_table:
            m.reset()
        for m in large_len_table:
            m.reset()
        dist_lsb_table.reset()
        for m in is_match_model:
            m.bit_0_prob = 1 << 10
        for i in range(cNumStates):
            is_rep_model[i].bit_0_prob = 1 << 10
            is_rep0_model[i].bit_0_prob = 1 << 10
            is_rep0_single_byte_model[i].bit_0_prob = 1 << 10
            is_rep1_model[i].bit_0_prob = 1 << 10
            is_rep2_model[i].bit_0_prob = 1 << 10

    def reset_huffman_table_update_rates():
        """lzham_decompressor::reset_huffman_table_update_rates()."""
        for m in lit_table:
            _reset_update_rate(m)
        for m in delta_lit_table:
            _reset_update_rate(m)
        _reset_update_rate(main_table)
        for m in rep_len_table:
            _reset_update_rate(m)
        for m in large_len_table:
            _reset_update_rate(m)
        _reset_update_rate(dist_lsb_table)

    lzx_position_base = lz_base.m_lzx_position_base
    lzx_position_extra_bits = lz_base.m_lzx_position_extra_bits

    SUCCESS, NOT_FINISHED = 1, 0
    status = NOT_FINISHED
    block_index = 0

    # =======================================================================
    # Output block loop:  do { ... } while (m_status == NOT_FINISHED);
    # =======================================================================
    while True:
        # #ifdef LZHAM_LZDEBUG: 12-bit outer sync marker == 166.  Not present
        # in release streams, so not read.

        # LZHAM_SYMBOL_CODEC_DECODE_GET_BITS(codec, m_block_type, cBlockHeaderBits)
        block_type = codec.decode_bits(cBlockHeaderBits)

        if block_type == cSyncBlock:
            # ---------------------------------------------------------------
            # Sync block.
            # ---------------------------------------------------------------
            flush_type = codec.decode_bits(cBlockFlushTypeBits)
            if flush_type == 1:
                reset_huffman_table_update_rates()
            elif flush_type == 2:
                reset_all_tables()

            codec.decode_align_to_byte()

            n = codec.decode_bits(16)
            if n != 0:
                raise LzhamDecompressError("FAILED_BAD_SYNC_BLOCK (expected 0x0000)")
            n = codec.decode_bits(16)
            if n != 0xFFFF:
                raise LzhamDecompressError("FAILED_BAD_SYNC_BLOCK (expected 0xFFFF)")

            # if (m_tmp == 2) { ... full flush ...; dst_ofs = 0; }
            # DELIBERATE DEVIATION: in the buffered path a full flush hands the
            # caller the dictionary so far and rewinds dst_ofs to 0, but the
            # *dictionary contents* survive and later matches reach back into
            # them through the ring-buffer mask.  Our window is the whole
            # output (unbuffered), so the equivalent of "rewind and keep the
            # history" is simply to keep appending.  We do not reset dst_ofs.
            # (The C's unbuffered full-flush path does reset dst_ofs, which in
            # unbuffered mode would overwrite already-produced output - that
            # looks like an unsupported combination in the alpha.  No H1Z1
            # chunk seen so far contains a sync block, so this is untested.)

        elif block_type == cRawBlock:
            # ---------------------------------------------------------------
            # Raw (stored) block.
            # ---------------------------------------------------------------
            num_raw_bytes_remaining = codec.decode_bits(24)

            num_raw_bytes_check_bits = codec.decode_bits(8)
            b0 = num_raw_bytes_remaining & 0xFF
            b1 = (num_raw_bytes_remaining >> 8) & 0xFF
            b2 = (num_raw_bytes_remaining >> 16) & 0xFF
            if num_raw_bytes_check_bits != ((b0 ^ b1) ^ b2):
                raise LzhamDecompressError("FAILED_BAD_RAW_BLOCK (check bits)")

            num_raw_bytes_remaining += 1

            # Discard any partial byte from the bit buffer.
            codec.decode_align_to_byte()

            # The C then drains whole bytes out of the bit buffer with
            # LZHAM_SYMBOL_CODEC_DECODE_REMOVE_BYTE_FROM_BIT_BUF and memcpy()s
            # the rest straight out of the input buffer, purely for speed.
            # After decode_align_to_byte() the bit buffer holds a whole number
            # of bytes, so pulling each byte with an 8-bit read consumes
            # exactly the same bytes in the same order.
            if out_buf_size is not None and dst_ofs + num_raw_bytes_remaining > out_buf_size:
                raise LzhamDecompressError("FAILED_DEST_BUF_TOO_SMALL (raw block)")
            for _ in range(num_raw_bytes_remaining):
                out.append(codec.decode_bits(8))
            dst_ofs += num_raw_bytes_remaining
            # prev_char / prev_prev_char / cur_state are NOT touched by a raw
            # block in the C either - a raw block is always followed by a fresh
            # comp block (which resets them) or by EOF.

        elif block_type == cCompBlock:
            # ---------------------------------------------------------------
            # Compressed block.
            # ---------------------------------------------------------------
            # LZHAM_SYMBOL_CODEC_DECODE_ARITH_START(codec)
            codec.start_arith_decoding()

            match_hist0 = 1
            match_hist1 = 1
            match_hist2 = 1
            match_hist3 = 1
            cur_state = 0
            prev_char = 0
            prev_prev_char = 0

            start_block_dst_ofs = dst_ofs   # m_start_block_dst_ofs (debug only)

            block_flush_type = codec.decode_bits(cBlockFlushTypeBits)
            if block_flush_type == 1:
                reset_huffman_table_update_rates()
            elif block_flush_type == 2:
                reset_all_tables()

            # for ( ; ; )   -- the main decode loop
            while True:
                # #ifdef LZHAM_LZDEBUG: 12-bit sync marker == 666, then
                # is_match(1), match_len(17), cur_state(4).  Not in release
                # streams, so not read.

                # Read "is match" bit.
                match_model_index = is_match_model_index(prev_char, cur_state)
                is_match_bit = codec.decode_bit(is_match_model[match_model_index])

                if not is_match_bit:
                    # --------------------------- literal ---------------------
                    if out_buf_size is not None and dst_ofs >= out_buf_size:
                        raise LzhamDecompressError("FAILED_DEST_BUF_TOO_SMALL (literal)")

                    if cur_state < cNumLitStates:
                        # Regular literal.
                        # lit_pred = (prev_char >> (8 - cNumLitPredBits / 2)) |
                        #            (prev_prev_char >> (8 - cNumLitPredBits / 2)) << (cNumLitPredBits / 2);
                        half = cNumLitPredBits // 2      # 3
                        lit_pred = (prev_char >> (8 - half)) | ((prev_prev_char >> (8 - half)) << half)
                        r = _decode_sym(codec, lit_table[lit_pred])
                        out.append(r)
                        prev_prev_char = prev_char
                        prev_char = r
                    else:
                        # Delta literal.
                        # match_hist0_ofs = dst_ofs - match_hist0;
                        # rep_lit0 = pDst[match_hist0_ofs & dict_size_mask];
                        # rep_lit1 = pDst[(match_hist0_ofs - 1) & dict_size_mask];
                        match_hist0_ofs = dst_ofs - match_hist0
                        if match_hist0_ofs < 1:
                            # In unbuffered mode the C would index with a
                            # wrapped uint here; treat as corruption.
                            raise LzhamDecompressError("FAILED_BAD_CODE (delta literal context)")
                        rep_lit0 = out[match_hist0_ofs]
                        rep_lit1 = out[match_hist0_ofs - 1]

                        half = cNumDeltaLitPredBits // 2   # 3
                        lit_pred = (rep_lit0 >> (8 - half)) | ((rep_lit1 >> (8 - half)) << half)

                        r = _decode_sym(codec, delta_lit_table[lit_pred])
                        r ^= rep_lit0
                        out.append(r)
                        prev_prev_char = prev_char
                        prev_char = r

                    cur_state = s_literal_next_state[cur_state]
                    dst_ofs += 1
                    # if ((!unbuffered) && (dst_ofs > dict_size_mask)) flush - dead here.

                else:
                    # ---------------------------- match ----------------------
                    match_len = 1

                    # Determine if match is a rep_match, and if so what type.
                    is_rep = codec.decode_bit(is_rep_model[cur_state])
                    if is_rep:
                        is_rep0 = codec.decode_bit(is_rep0_model[cur_state])
                        if is_rep0:
                            is_rep0_len1 = codec.decode_bit(is_rep0_single_byte_model[cur_state])
                            if is_rep0_len1:
                                # rep0, length 1.  match_len stays 1.
                                cur_state = 9 if cur_state < cNumLitStates else 11
                            else:
                                match_len = _decode_sym(
                                    codec, rep_len_table[1 if cur_state >= cNumLitStates else 0])
                                match_len += cMinMatchLen
                                if match_len == (cMaxMatchLen + 1):
                                    match_len = _decode_huge_match_len(codec)
                                cur_state = 8 if cur_state < cNumLitStates else 11
                        else:
                            match_len = _decode_sym(
                                codec, rep_len_table[1 if cur_state >= cNumLitStates else 0])
                            match_len += cMinMatchLen
                            if match_len == (cMaxMatchLen + 1):
                                match_len = _decode_huge_match_len(codec)

                            is_rep1 = codec.decode_bit(is_rep1_model[cur_state])
                            if is_rep1:
                                match_hist1, match_hist0 = match_hist0, match_hist1
                            else:
                                is_rep2 = codec.decode_bit(is_rep2_model[cur_state])
                                if is_rep2:
                                    # rep2
                                    temp = match_hist2
                                    match_hist2 = match_hist1
                                    match_hist1 = match_hist0
                                    match_hist0 = temp
                                else:
                                    # rep3
                                    temp = match_hist3
                                    match_hist3 = match_hist2
                                    match_hist2 = match_hist1
                                    match_hist1 = match_hist0
                                    match_hist0 = temp

                            cur_state = 8 if cur_state < cNumLitStates else 11
                    else:
                        # Handle normal/full match.
                        sym = _decode_sym(codec, main_table)
                        sym -= cLZXNumSpecialLengths

                        if sym < 0:
                            # Handle special symbols.  (static_cast<int>(sym) < 0)
                            if sym == (cLZXSpecialCodeEndOfBlockCode - cLZXNumSpecialLengths):
                                break            # end of block
                            # Must be cLZXSpecialCodePartialStateReset.
                            match_hist0 = 1
                            match_hist1 = 1
                            match_hist2 = 1
                            match_hist3 = 1
                            cur_state = 0
                            continue

                        # Low 3 bits of symbol = match length category,
                        # higher bits = distance category.
                        match_len = (sym & 7) + 2
                        match_slot = (sym >> 3) + cLZXLowestUsableMatchSlot

                        if match_len == 9:
                            # Match is >= 9 bytes, decode the actual length.
                            e = _decode_sym(
                                codec, large_len_table[1 if cur_state >= cNumLitStates else 0])
                            match_len += e
                            if match_len == (cMaxMatchLen + 1):
                                match_len = _decode_huge_match_len(codec)

                        # Distance = slot base + extra bits.
                        num_extra_bits = lzx_position_extra_bits[match_slot]
                        if num_extra_bits < 3:
                            # GET_BITS with num_bits == 0 yields 0 in the C macro.
                            extra_bits = codec.decode_bits(num_extra_bits) if num_extra_bits else 0
                        else:
                            extra_bits = 0
                            if num_extra_bits > 4:
                                extra_bits = codec.decode_bits(num_extra_bits - 4) << 4
                            j = _decode_sym(codec, dist_lsb_table)
                            extra_bits += j

                        match_hist3 = match_hist2
                        match_hist2 = match_hist1
                        match_hist1 = match_hist0
                        match_hist0 = lzx_position_base[match_slot] + extra_bits

                        cur_state = cNumLitStates if cur_state < cNumLitStates else cNumLitStates + 3

                    # ---- We have the match's length and distance, do the copy.
                    # #ifdef LZHAM_LZDEBUG: 25+4 bits of match distance.  Not read.

                    # if ((unbuffered) && ((match_hist0 > dst_ofs) ||
                    #                      ((dst_ofs + match_len) > out_buf_size)))
                    #    -> LZHAM_DECOMP_STATUS_FAILED_BAD_CODE
                    if match_hist0 > dst_ofs or (
                            out_buf_size is not None and dst_ofs + match_len > out_buf_size):
                        raise LzhamDecompressError(
                            "FAILED_BAD_CODE (match dist %d len %d at ofs %d)"
                            % (match_hist0, match_len, dst_ofs))

                    src_ofs = dst_ofs - match_hist0   # & dict_size_mask == UINT_MAX

                    # The C's "(!unbuffered) && (max(src_ofs, dst_ofs) + match_len) >
                    # dict_size_mask" byte-at-a-time wrapping copy is dead in
                    # unbuffered mode, so only the else branch is ported.  Its
                    # three cases are transcribed as-is; all three are forward
                    # byte copies, which is what makes an overlapping match
                    # (match_hist0 < match_len) replicate correctly - the bytes
                    # written earlier in this same copy are read back later.
                    if match_hist0 == 1:
                        # Handle byte runs (memset in the C).
                        c = out[src_ofs]
                        out += bytes((c,)) * match_len
                        if match_len == 1:
                            prev_prev_char = prev_char
                        else:
                            prev_prev_char = c
                        prev_char = c
                    elif match_len == 1:
                        # Handle single byte matches.
                        prev_prev_char = prev_char
                        prev_char = out[src_ofs]
                        out.append(prev_char)
                    else:
                        # Handle matches of length 2 or higher.  The C picks
                        # memcpy() when (bytes_to_copy >= 8 && bytes_to_copy <=
                        # match_hist0), i.e. only when the ranges cannot
                        # overlap, so a plain forward byte loop is equivalent
                        # for every case.
                        bytes_to_copy = match_len - 2
                        for i in range(bytes_to_copy):
                            out.append(out[src_ofs + i])
                        # The final 2 bytes are handled specially in the C only
                        # because it tracks them in locals.
                        prev_prev_char = out[src_ofs + bytes_to_copy]
                        out.append(prev_prev_char)
                        prev_char = out[src_ofs + bytes_to_copy + 1]
                        out.append(prev_char)

                    dst_ofs += match_len
                # end lit-or-match
            # end for ( ; ; )

            # #ifdef LZHAM_LZDEBUG: 12-bit end sync marker == 366.  Not read.
            codec.decode_align_to_byte()

        elif block_type == cEOFBlock:
            # Received EOF.
            status = SUCCESS
        else:
            # This block type is currently undefined.
            raise LzhamDecompressError("FAILED_BAD_CODE (undefined block type %d)" % block_type)

        block_index += 1
        if status != NOT_FINISHED:
            break

    # if ((!unbuffered) && (dst_ofs)) LZHAM_FLUSH_OUTPUT_BUFFER(dst_ofs) - dead here.

    # if (m_status == LZHAM_DECOMP_STATUS_SUCCESS) { align; read 32-bit adler32 }
    codec.decode_align_to_byte()
    file_src_file_adler32 = codec.decode_bits(16)
    low = codec.decode_bits(16)
    file_src_file_adler32 = ((file_src_file_adler32 << 16) | low) & 0xFFFFFFFF

    # unbuffered: m_decomp_adler32 = adler32(pDst, dst_ofs, cInitAdler32)
    decomp_adler32 = zlib.adler32(out, cInitAdler32) & 0xFFFFFFFF
    if file_src_file_adler32 != decomp_adler32:
        raise LzhamDecompressError(
            "FAILED_ADLER32: stream says 0x%08X, output is 0x%08X (%d bytes)"
            % (file_src_file_adler32, decomp_adler32, len(out)))

    codec.stop_decoding()

    if uncompressed_size is not None and len(out) != uncompressed_size:
        raise LzhamDecompressError(
            "decoded %d bytes, expected %d" % (len(out), uncompressed_size))

    assert dst_ofs == len(out), (dst_ofs, len(out))
    return out, decomp_adler32


def decompress_stream(data: bytes) -> bytes:
    """Decode a zlib-style LZHAM stream: ``data`` starts at the 2-byte header.

    Validates the header the way lzham_decompressor::decompress() does under
    LZHAM_DECOMP_FLAG_READ_ZLIB_STREAM:

        LZHAM_SYMBOL_CODEC_DECODE_GET_BITS(codec, m_z_cmf, 8);
        LZHAM_SYMBOL_CODEC_DECODE_GET_BITS(codec, m_z_flg, 8);
        check = ((m_z_cmf << 8) + m_z_flg) % 31;
        if ((check != 0) || ((m_z_cmf & 15) != LZHAM_Z_LZHAM))
           return LZHAM_DECOMP_STATUS_FAILED_BAD_ZLIB_HEADER;

    HOW dict_size_log2 IS DETERMINED: the decompressor itself never reads it -
    lzham_lib_z_inflateInit2() takes it as ``window_bits`` from the caller, and
    for a raw lzham_lib_decompress() it comes from
    lzham_decompress_params::m_dict_size_log2.  But the *compressor* encodes it
    in CMF (lzcompressor::send_zlib_header():
    ``cmf = LZHAM_Z_LZHAM | ((m_dict_size_log2 - 15) << 4)``), so for a
    zlib-wrapped alpha stream CINFO + 15 is authoritative.  H1Z1 chunks have
    CMF = 0x5E -> CINFO 5 -> dict_size_log2 = 20 (1 MiB), matching
    DEFAULT_DICT_SIZE_LOG2.  We use CINFO and fall back to
    DEFAULT_DICT_SIZE_LOG2 only if CINFO yields an out-of-range value.

    What would settle it beyond doubt: a successful decode whose trailing
    adler32 matches zlib.adler32 of the output and whose length matches the
    chunk header's uncompressed_size.  A wrong dict_size_log2 changes
    m_num_lzx_slots, which changes m_main_table's symbol count, which
    desynchronises the very first Huffman table - so a correct adler32 with a
    given dict_size_log2 is conclusive.
    """
    if len(data) < 2:
        raise LzhamDecompressError("FAILED_BAD_ZLIB_HEADER: fewer than 2 bytes")

    cmf = data[0]
    flg = data[1]
    if (((cmf << 8) + flg) % 31) != 0:
        raise LzhamDecompressError(
            "FAILED_BAD_ZLIB_HEADER: check %d != 0 (cmf=0x%02X flg=0x%02X)"
            % (((cmf << 8) + flg) % 31, cmf, flg))
    if (cmf & 15) != LZHAM_Z_LZHAM:
        raise LzhamDecompressError(
            "FAILED_BAD_ZLIB_HEADER: CM %d != LZHAM_Z_LZHAM (%d)" % (cmf & 15, LZHAM_Z_LZHAM))
    if flg & 32:
        # FDICT: the stream is followed by a 4-byte adler32 of the preset
        # dictionary and cannot be decoded without the seed bytes.
        raise LzhamDecompressError("FAILED_NEED_SEED_BYTES: FDICT is set")

    dict_size_log2 = dict_size_log2_from_cmf(cmf)
    if not (cMinDictSizeLog2 <= dict_size_log2 <= cMaxDictSizeLog2):
        dict_size_log2 = DEFAULT_DICT_SIZE_LOG2

    return decompress(data[2:], dict_size_log2)
