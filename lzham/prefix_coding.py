"""Pure-Python port of the lzham_alpha prefix-coding decoder support code.

Sources (github.com/richgel999/lzham_alpha, master branch):
  lzhamdecomp/lzham_prefix_coding.h    -> constants, decoder_tables layout
  lzhamdecomp/lzham_prefix_coding.cpp  -> limit_max_code_size, generate_codes,
                                          generate_decoder_tables
  lzhamdecomp/lzham_huffman_codes.cpp  -> generate_huffman_codes
                                          (calculate_minimum_redundancy)
  lzhamdecomp/lzham_polar_codes.cpp    -> generate_polar_codes
  lzhamdecomp/lzham_math.h             -> total_bits, next_pow2, is_power_of_2

Decoder side only.  Transcribed faithfully in preference to being idiomatic;
the original C identifiers are kept in comments where they help.  Every
quantity that the C code holds in a fixed-width unsigned is masked explicitly,
because Python ints do not wrap.

Stdlib only.
"""

# ---------------------------------------------------------------------------
# Constants, transcribed exactly.
# ---------------------------------------------------------------------------

# lzham_prefix_coding.h
MAX_EXPECTED_CODE_SIZE = 16     # prefix_coding::cMaxExpectedCodeSize
MAX_SUPPORTED_SYMS = 1024       # prefix_coding::cMaxSupportedSyms
MAX_TABLE_BITS = 11             # prefix_coding::cMaxTableBits

# lzham_prefix_coding.cpp, local to limit_max_code_size()
MAX_EVER_CODE_SIZE = 34         # cMaxEverCodeSize

# lzham_huffman_codes.h / lzham_polar_codes.h
HUFFMAN_MAX_SUPPORTED_SYMS = 1024   # cHuffmanMaxSupportedSyms
POLAR_MAX_SUPPORTED_SYMS = 1024     # cPolarMaxSupportedSyms

UINT_MAX = 0xFFFFFFFF
UINT16_MAX = 0xFFFF
UINT32_MAX = 0xFFFFFFFF

# Aliases matching the C names, for call sites that prefer them.
cMaxExpectedCodeSize = MAX_EXPECTED_CODE_SIZE
cMaxSupportedSyms = MAX_SUPPORTED_SYMS
cMaxTableBits = MAX_TABLE_BITS


# ---------------------------------------------------------------------------
# lzham_math.h helpers
# ---------------------------------------------------------------------------

def total_bits(v):
    """math::total_bits - number of bits needed to encode v.  total_bits(0)==0."""
    # The C loop shifts v right until zero, counting iterations.
    return int(v).bit_length()


def next_pow2(val):
    """math::next_pow2 (uint32 flavour).  Unchanged if already a power of 2."""
    val &= 0xFFFFFFFF
    if val <= 1:
        # C: val-- underflows to 0xFFFFFFFF for val==0, then returns 0.
        # val==1 returns 1.  Reproduce both.
        return 0 if val == 0 else 1
    val -= 1
    val |= val >> 16
    val |= val >> 8
    val |= val >> 4
    val |= val >> 2
    val |= val >> 1
    return (val + 1) & 0xFFFFFFFF


def is_power_of_2(x):
    """math::is_power_of_2"""
    x &= 0xFFFFFFFF
    return bool(x) and (x & (x - 1)) == 0


# ---------------------------------------------------------------------------
# prefix_coding::limit_max_code_size
# ---------------------------------------------------------------------------

def limit_max_code_size(num_syms, code_sizes, max_code_size):
    """Port of prefix_coding::limit_max_code_size().

    The C function edits pCodesizes in place and returns bool.  Here we return
    a new list (and also write the result back into `code_sizes` if it is a
    mutable list, so callers written either way behave the same).

    Returns the (possibly unchanged) code size list.  Raises ValueError where
    the C returns false, since every caller in lzham asserts on failure.
    """
    cs = list(code_sizes)

    if (not num_syms) or (num_syms > MAX_SUPPORTED_SYMS) or \
       (max_code_size < 1) or (max_code_size > MAX_EVER_CODE_SIZE):
        raise ValueError(
            "limit_max_code_size: bad args num_syms=%r max_code_size=%r"
            % (num_syms, max_code_size))

    # uint num_codes[cMaxEverCodeSize + 1]; zero_object
    num_codes = [0] * (MAX_EVER_CODE_SIZE + 1)

    should_limit = False
    for i in range(num_syms):
        c = cs[i]
        assert c <= MAX_EVER_CODE_SIZE, "codesize %d exceeds cMaxEverCodeSize" % c
        num_codes[c] += 1
        if c > max_code_size:
            should_limit = True

    if not should_limit:
        _write_back(code_sizes, cs)
        return cs

    # next_sorted_ofs[] is built from the ORIGINAL per-length counts; this is
    # what makes symbols keep their relative order by original code length.
    ofs = 0
    next_sorted_ofs = [0] * (MAX_EVER_CODE_SIZE + 1)
    for i in range(1, MAX_EVER_CODE_SIZE + 1):
        next_sorted_ofs[i] = ofs
        ofs += num_codes[i]

    if (ofs < 2) or (ofs > MAX_SUPPORTED_SYMS):
        # C returns true (nothing to do) here.
        _write_back(code_sizes, cs)
        return cs

    if ofs > (1 << max_code_size):
        raise ValueError("limit_max_code_size: %d used syms cannot fit in %d bits"
                         % (ofs, max_code_size))

    # Fold every over-long length into max_code_size.
    for i in range(max_code_size + 1, MAX_EVER_CODE_SIZE + 1):
        num_codes[max_code_size] += num_codes[i]

    # Technique of adjusting tree to enforce maximum code size from LHArc.
    total = 0
    for i in range(max_code_size, 0, -1):
        total += (num_codes[i] << (max_code_size - i))
    total &= 0xFFFFFFFF

    if total == (1 << max_code_size):
        _write_back(code_sizes, cs)
        return cs

    while True:
        num_codes[max_code_size] -= 1

        i = max_code_size - 1
        while i:
            if num_codes[i]:
                num_codes[i] -= 1
                num_codes[i + 1] += 2
                break
            i -= 1
        if not i:
            raise ValueError("limit_max_code_size: could not rebalance tree")

        total -= 1
        if total == (1 << max_code_size):
            break

    # new_codesizes: the adjusted lengths laid out in increasing order.
    new_codesizes = []
    for i in range(1, max_code_size + 1):
        n = num_codes[i]
        if n:
            new_codesizes.extend([i] * n)

    for i in range(num_syms):
        c = cs[i]
        if c:
            next_ofs = next_sorted_ofs[c]
            next_sorted_ofs[c] = next_ofs + 1
            cs[i] = new_codesizes[next_ofs] & 0xFF

    _write_back(code_sizes, cs)
    return cs


def _write_back(dst, src):
    """Mirror the C in-place semantics when the caller passed a mutable list."""
    if dst is src:
        return
    try:
        if len(dst) >= len(src):
            dst[:len(src)] = src
    except TypeError:
        pass


# ---------------------------------------------------------------------------
# prefix_coding::generate_codes
# ---------------------------------------------------------------------------

def generate_codes(num_syms, code_sizes):
    """Port of prefix_coding::generate_codes().

    Returns the canonical code for each symbol (0 for unused symbols, i.e.
    code size 0 - the C also writes next_code[0]++ there, which starts at 0).
    Raises ValueError where the C returns false (over/under-subscribed set with
    more than one used symbol).
    """
    # uint num_codes[cMaxExpectedCodeSize + 1]; zero_object
    num_codes = [0] * (MAX_EXPECTED_CODE_SIZE + 1)

    for i in range(num_syms):
        c = code_sizes[i]
        assert c <= MAX_EXPECTED_CODE_SIZE, \
            "generate_codes: codesize %d exceeds cMaxExpectedCodeSize" % c
        num_codes[c] += 1

    code = 0
    next_code = [0] * (MAX_EXPECTED_CODE_SIZE + 1)
    next_code[0] = 0

    for i in range(1, MAX_EXPECTED_CODE_SIZE + 1):
        next_code[i] = code
        code = ((code + num_codes[i]) << 1) & 0xFFFFFFFF

    # The C compares against (1 << (cMaxExpectedCodeSize + 1)) == 1 << 17,
    # which is the fully-saturated value after 16 doublings.
    if code != (1 << (MAX_EXPECTED_CODE_SIZE + 1)):
        t = 0
        for i in range(1, MAX_EXPECTED_CODE_SIZE + 1):
            t += num_codes[i]
            if t > 1:
                raise ValueError("generate_codes: code set is not complete")

    codes = [0] * num_syms
    for i in range(num_syms):
        c = code_sizes[i]
        assert (not c) or (next_code[c] <= UINT16_MAX)
        codes[i] = next_code[c] & 0xFFFF
        next_code[c] += 1
        assert (not c) or (total_bits(codes[i]) <= code_sizes[i])

    return codes


# ---------------------------------------------------------------------------
# prefix_coding::decoder_tables
# ---------------------------------------------------------------------------

class DecoderTables:
    """Port of prefix_coding::decoder_tables + generate_decoder_tables().

    Attribute names are the C members without the m_ prefix:

      num_syms, total_used_syms, table_bits, table_shift, table_max_code,
      decode_start_code_size, min_code_size, max_code_size,
      max_codes[17], val_ptrs[17],
      cur_lookup_size, lookup[], cur_sorted_symbol_order_size,
      sorted_symbol_order[]

    Decoding works as in symbol_codec::decode(quasi_adaptive_huffman_data_model&):

      k = (next 16 bits, MSB-first) + 1
      if k <= table_max_code:                       # code fits the fast table
          t = lookup[next table_bits bits]
          sym, len = t & 0xFFFF, t >> 16
      else:                                         # long code
          len = decode_start_code_size
          while k > max_codes[len-1]: len += 1
          sym = sorted_symbol_order[val_ptrs[len-1] + (next len bits)]
      consume len bits

    max_codes[n-1] holds 1 + (max_code_of_length_n left-justified into 16 bits
    with the low 16-n bits set), or 0 when no code has length n - 0 being the
    "no codes of this length" sentinel, since k >= 1 always so `k <= 0` is
    never true and the search loop skips that length.  max_codes[16] and
    val_ptrs[16] are the loop sentinels UINT_MAX and 0xFFFFF.

    lookup[] entries are sym | (codesize << 16); UINT32_MAX (0xFFFFFFFF) means
    "not in the fast table" (the code is longer than table_bits).  Such an
    entry is never read, because table_max_code gates the fast path.
    """

    def __init__(self, num_syms=0, code_sizes=None, table_bits=0):
        # decoder_tables() ctor
        self.num_syms = 0
        self.total_used_syms = 0
        self.table_bits = 0
        self.table_shift = 0
        self.table_max_code = 0
        self.decode_start_code_size = 0
        self.min_code_size = 0
        self.max_code_size = 0
        self.max_codes = [0] * (MAX_EXPECTED_CODE_SIZE + 1)
        self.val_ptrs = [0] * (MAX_EXPECTED_CODE_SIZE + 1)
        self.cur_lookup_size = 0
        self.lookup = None
        self.cur_sorted_symbol_order_size = 0
        self.sorted_symbol_order = None

        if code_sizes is not None:
            self.init(num_syms, code_sizes, table_bits)

    def clear(self):
        """decoder_tables::clear()"""
        self.lookup = None
        self.cur_lookup_size = 0
        self.sorted_symbol_order = None
        self.cur_sorted_symbol_order_size = 0

    def get_unshifted_max_code(self, length):
        """decoder_tables::get_unshifted_max_code(uint len)"""
        assert 1 <= length <= MAX_EXPECTED_CODE_SIZE
        k = self.max_codes[length - 1]
        if not k:
            return UINT_MAX
        return (k - 1) >> (16 - length)

    def init(self, num_syms, code_sizes, table_bits):
        """Port of prefix_coding::generate_decoder_tables().

        Safe to call repeatedly on the same object: the lookup and
        sorted_symbol_order buffers only ever grow, exactly as in the C.
        Raises ValueError where the C returns false.
        """
        # uint min_codes[cMaxExpectedCodeSize]; -- deliberately left at 0 for
        # lengths with no codes.  The C leaves those entries uninitialised and
        # subtracts them from val_ptrs entries that are themselves
        # uninitialised; neither is ever read, because a length with no codes
        # has max_codes == 0 and so is skipped by the decode search loop.
        min_codes = [0] * MAX_EXPECTED_CODE_SIZE

        if (not num_syms) or (table_bits > MAX_TABLE_BITS):
            raise ValueError("generate_decoder_tables: num_syms=%r table_bits=%r"
                             % (num_syms, table_bits))

        self.num_syms = num_syms

        num_codes = [0] * (MAX_EXPECTED_CODE_SIZE + 1)
        for i in range(num_syms):
            c = code_sizes[i]
            num_codes[c] += 1

        sorted_positions = [0] * (MAX_EXPECTED_CODE_SIZE + 1)

        next_code = 0
        total_used_syms = 0
        max_code_size = 0
        min_code_size = UINT_MAX

        for i in range(1, MAX_EXPECTED_CODE_SIZE + 1):
            n = num_codes[i]

            if not n:
                self.max_codes[i - 1] = 0   # //UINT_MAX in the original source
            else:
                min_code_size = min(min_code_size, i)
                max_code_size = max(max_code_size, i)

                min_codes[i - 1] = next_code

                mc = next_code + n - 1
                mc = 1 + (((mc << (16 - i)) | ((1 << (16 - i)) - 1)) & 0xFFFFFFFF)
                self.max_codes[i - 1] = mc & 0xFFFFFFFF

                self.val_ptrs[i - 1] = total_used_syms

                sorted_positions[i] = total_used_syms

                next_code += n
                total_used_syms += n

            next_code = (next_code << 1) & 0xFFFFFFFF

        self.total_used_syms = total_used_syms

        if total_used_syms > self.cur_sorted_symbol_order_size:
            self.cur_sorted_symbol_order_size = total_used_syms
            if not is_power_of_2(total_used_syms):
                self.cur_sorted_symbol_order_size = min(
                    num_syms, next_pow2(total_used_syms))
            self.sorted_symbol_order = [0] * self.cur_sorted_symbol_order_size
        elif self.sorted_symbol_order is None:
            # total_used_syms == 0 and nothing allocated yet.
            self.sorted_symbol_order = []

        # static_cast<uint8>(...): min_code_size stays UINT_MAX -> 255 when no
        # symbol is used at all.
        self.min_code_size = min_code_size & 0xFF
        self.max_code_size = max_code_size & 0xFF

        for i in range(num_syms):
            c = code_sizes[i]
            if c:
                assert num_codes[c]
                sorted_pos = sorted_positions[c]
                sorted_positions[c] = sorted_pos + 1
                assert sorted_pos < total_used_syms
                self.sorted_symbol_order[sorted_pos] = i & 0xFFFF

        # Note the <= : a table no wider than the shortest code is useless.
        if table_bits <= self.min_code_size:
            table_bits = 0
        self.table_bits = table_bits

        if table_bits:
            table_size = 1 << table_bits
            if table_size > self.cur_lookup_size:
                self.cur_lookup_size = table_size
                self.lookup = [UINT32_MAX] * table_size

            # memset(m_lookup, 0xFF, sizeof(uint32) * (1 << table_bits))
            for t in range(table_size):
                self.lookup[t] = UINT32_MAX

            for codesize in range(1, table_bits + 1):
                if not num_codes[codesize]:
                    continue

                fillsize = table_bits - codesize
                fillnum = 1 << fillsize

                min_code = min_codes[codesize - 1]
                max_code = self.get_unshifted_max_code(codesize)
                val_ptr = self.val_ptrs[codesize - 1]

                for code in range(min_code, max_code + 1):
                    sym_index = self.sorted_symbol_order[val_ptr + code - min_code]
                    assert code_sizes[sym_index] == codesize
                    base = code << fillsize
                    for j in range(fillnum):
                        t = j + base
                        assert t < (1 << table_bits)
                        assert self.lookup[t] == UINT32_MAX
                        self.lookup[t] = (sym_index | (codesize << 16)) & 0xFFFFFFFF

        # Bias val_ptrs by min_code so that
        #   val_ptrs[len-1] + (peek len bits) == index into sorted_symbol_order.
        # Entries for unused lengths become 0 - 0 == 0 here and are never read.
        for i in range(MAX_EXPECTED_CODE_SIZE):
            self.val_ptrs[i] -= min_codes[i]

        self.table_max_code = 0
        self.decode_start_code_size = self.min_code_size

        if table_bits:
            # for (i = table_bits; i >= 1; i--) ... ; the C tests i >= 1
            # afterwards to tell "found" from "fell out of the loop".
            i = table_bits
            found = False
            while i >= 1:
                if num_codes[i]:
                    self.table_max_code = self.max_codes[i - 1]
                    found = True
                    break
                i -= 1
            if found:
                self.decode_start_code_size = table_bits + 1
                for i in range(table_bits + 1, max_code_size + 1):
                    if num_codes[i]:
                        self.decode_start_code_size = i
                        break

        # sentinels
        self.max_codes[MAX_EXPECTED_CODE_SIZE] = UINT_MAX
        self.val_ptrs[MAX_EXPECTED_CODE_SIZE] = 0xFFFFF

        self.table_shift = (32 - self.table_bits) & 0xFFFFFFFF

        return self

    # -- decoding -----------------------------------------------------------

    def decode(self, codec):
        """Prefix-decode one symbol from `codec` and consume its bits.

        Transcribed from the table-walking half of
        symbol_codec::decode(quasi_adaptive_huffman_data_model&).  The model
        frequency bump and update-cycle bookkeeping are NOT done here; that is
        SymbolCodec.decode_symbol's job.
        """
        k = (codec.decode_peek_bits(16) + 1) & 0xFFFFFFFF

        if k <= self.table_max_code:
            t = self.lookup[codec.decode_peek_bits(self.table_bits)]
            # t != UINT32_MAX is guaranteed by the table_max_code gate.
            sym = t & UINT16_MAX
            length = t >> 16
        else:
            length = self.decode_start_code_size
            while True:
                if length > MAX_EXPECTED_CODE_SIZE:
                    raise ValueError("prefix decode: ran past cMaxExpectedCodeSize")
                if k <= self.max_codes[length - 1]:
                    break
                length += 1

            val_ptr = self.val_ptrs[length - 1] + codec.decode_peek_bits(length)
            # The C compares against model.m_total_syms; total_used_syms is the
            # tighter and equally valid bound on a well-formed table.
            if val_ptr < 0 or val_ptr >= self.total_used_syms:
                raise ValueError("prefix decode: val_ptr %d out of range (%d syms)"
                                 % (val_ptr, self.total_used_syms))
            sym = self.sorted_symbol_order[val_ptr]

        codec.decode_remove_bits(length)
        return sym


def generate_decoder_tables(num_syms, code_sizes, tables, table_bits):
    """Free-function spelling of prefix_coding::generate_decoder_tables()."""
    if tables is None:
        tables = DecoderTables()
    tables.init(num_syms, code_sizes, table_bits)
    return tables


# ---------------------------------------------------------------------------
# lzham_huffman_codes.cpp
# ---------------------------------------------------------------------------

def _radix_sort_syms(items):
    """radix_sort_syms() - a stable 2-pass LSB radix sort on the 16-bit freq.

    The C sorts an array of {freq, ...} structs by freq ascending, stably.
    A stable sort on the freq key gives an identical permutation, so this is
    equivalent while being far shorter.  `items` is a list of (freq, payload).
    """
    return sorted(items, key=lambda sf: sf[0])


def _calculate_minimum_redundancy(A, n):
    """calculate_minimum_redundancy() by Moffat & Katajainen, in place on A.

    A[0..n-1] holds the frequencies in ascending order on entry, and the code
    lengths on exit.
    """
    if n == 0:
        return
    if n == 1:
        A[0] = 0
        return

    # first pass, left to right, setting parent pointers
    A[0] += A[1]
    root = 0
    leaf = 2
    for nxt in range(1, n - 1):
        # select first item for a pairing
        if leaf >= n or A[root] < A[leaf]:
            A[nxt] = A[root]
            A[root] = nxt
            root += 1
        else:
            A[nxt] = A[leaf]
            leaf += 1

        # add on the second item
        if leaf >= n or (root < nxt and A[root] < A[leaf]):
            A[nxt] += A[root]
            A[root] = nxt
            root += 1
        else:
            A[nxt] += A[leaf]
            leaf += 1

    # second pass, right to left, setting internal depths
    A[n - 2] = 0
    for nxt in range(n - 3, -1, -1):
        A[nxt] = A[A[nxt]] + 1

    # third pass, right to left, setting leaf depths
    avbl = 1
    used = 0
    dpth = 0
    root = n - 2
    nxt = n - 1
    while avbl > 0:
        while root >= 0 and A[root] == dpth:
            used += 1
            root -= 1
        while avbl > used:
            A[nxt] = dpth
            nxt -= 1
            avbl -= 1
        avbl = 2 * used
        dpth += 1
        used = 0


def generate_huffman_codes(freqs, num_syms):
    """Port of lzham::generate_huffman_codes().

    Returns (code_sizes, max_code_size, total_freq).

    Symbols with zero frequency get code size 0.  The C leaves max_code_size
    untouched in the single-used-symbol case (its caller's variable is
    uninitialised); here we return the true value, 1.  That is benign: the
    caller only uses max_code_size to decide whether to call
    limit_max_code_size, and limiting a lone length-1 code is a no-op.
    """
    if (not num_syms) or (num_syms > HUFFMAN_MAX_SUPPORTED_SYMS):
        raise ValueError("generate_huffman_codes: num_syms=%r" % (num_syms,))

    code_sizes = [0] * num_syms

    total_freq = 0
    used = []                       # state.syms0[0 .. num_used_syms-1]
    for i in range(num_syms):
        freq = freqs[i]
        if not freq:
            code_sizes[i] = 0
        else:
            total_freq += freq
            used.append((freq, i))  # m_freq, m_left(=symbol index)

    num_used_syms = len(used)

    if num_used_syms == 0:
        # The C would run radix_sort_syms(0, ...) then
        # calculate_minimum_redundancy(x, 0), which returns immediately,
        # leaving all code sizes 0.  Same result.
        return code_sizes, 0, total_freq

    if num_used_syms == 1:
        code_sizes[used[0][1]] = 1
        return code_sizes, 1, total_freq

    syms = _radix_sort_syms(used)

    x = [sf[0] for sf in syms]
    _calculate_minimum_redundancy(x, num_used_syms)

    max_len = 0
    for i in range(num_used_syms):
        length = x[i]
        max_len = max(length, max_len)
        code_sizes[syms[i][1]] = length & 0xFF

    return code_sizes, max_len, total_freq


# ---------------------------------------------------------------------------
# lzham_polar_codes.cpp
#
# Note: LZHAM_USE_SHANNON_FANO_CODES and LZHAM_USE_FYFFE_CODES are both 0 in
# lzham_alpha, so only Andrew Polar's algorithm is live.  The two disabled
# variants are not ported.
# ---------------------------------------------------------------------------

def _generate_polar_codes_inner(num_syms, sf, code_sizes):
    """Port of the static void generate_polar_codes(num_syms, pSF, ...).

    `sf` is the ascending-by-frequency sorted list of (freq, sym); the C walks
    it backwards, i.e. most frequent first.  Writes into code_sizes and returns
    max_code_size.
    """
    tmp_freq = [0] * num_syms

    orig_total_freq = 0
    cur_total = 0
    for i in range(num_syms):
        sym_freq = sf[num_syms - 1 - i][0]
        orig_total_freq += sym_freq

        sym_len = total_bits(sym_freq)
        adjusted_sym_freq = 1 << (sym_len - 1)
        tmp_freq[i] = adjusted_sym_freq
        cur_total += adjusted_sym_freq

    tree_total = 1 << (total_bits(orig_total_freq) - 1)
    if tree_total < orig_total_freq:
        tree_total <<= 1

    start_index = 0
    while (cur_total < tree_total) and (start_index < num_syms):
        for i in range(start_index, num_syms):
            freq = tmp_freq[i]
            if (cur_total + freq) <= tree_total:
                tmp_freq[i] += freq
                cur_total += freq
                if cur_total == tree_total:
                    break
            else:
                start_index = i + 1

    assert cur_total == tree_total, \
        "polar codes: cur_total %d != tree_total %d" % (cur_total, tree_total)

    max_code_size = 0
    tree_total_bits = total_bits(tree_total)
    for i in range(num_syms):
        codesize = tree_total_bits - total_bits(tmp_freq[i])
        max_code_size = max(codesize, max_code_size)
        code_sizes[sf[num_syms - 1 - i][1]] = codesize & 0xFF

    return max_code_size


def generate_polar_codes(freqs, num_syms):
    """Port of lzham::generate_polar_codes().

    Returns (code_sizes, max_code_size, total_freq).

    Faithfulness note: the C passes the OUTER `num_syms` to the inner
    generate_polar_codes(), not `num_used_syms`, so when some frequency is zero
    it reads past the sorted region of the work buffer (stale data from a
    previous call - undefined behaviour).  That never happens in the
    quasi-adaptive models, whose symbol frequencies are all >= 1 at every
    update, so the two agree on every real stream.  Here we pass
    num_used_syms, which is deterministic; if they differ, this port and the C
    may disagree.  UNVERIFIED for that case.
    """
    if (not num_syms) or (num_syms > POLAR_MAX_SUPPORTED_SYMS):
        raise ValueError("generate_polar_codes: num_syms=%r" % (num_syms,))

    code_sizes = [0] * num_syms

    total_freq = 0
    used = []
    for i in range(num_syms):
        freq = freqs[i]
        if not freq:
            code_sizes[i] = 0
        else:
            total_freq += freq
            used.append((freq & 0xFFFF, i))   # m_freq is uint16 in the C

    num_used_syms = len(used)

    if num_used_syms == 0:
        return code_sizes, 0, total_freq

    if num_used_syms == 1:
        # The C sets the code size but leaves max_code_size untouched; see the
        # note in generate_huffman_codes.
        code_sizes[used[0][1]] = 1
        return code_sizes, 1, total_freq

    syms = _radix_sort_syms(used)
    max_code_size = _generate_polar_codes_inner(num_used_syms, syms, code_sizes)

    return code_sizes, max_code_size, total_freq
