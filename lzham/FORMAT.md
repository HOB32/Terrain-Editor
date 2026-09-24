# LZHAM in H1Z1: the format, from the file down to the bit

Everything below was read off our port of `lzham_alpha` (richgel999, master) and
confirmed against the game's own files: every shipped `.cnk`/`.ctg_pc` decodes with
the stream's adler32 and the container's size both agreeing, and the encoder in
`lzcomp.py` round-trips through the reference decoder. H1Z1.exe links the same library
(`...\gametechnology\code\external\lzham\lzhamdecomp\...` strings; it even carries the
compressor, `lzham_lzcomp_internal.cpp`).

## 1. Where LZHAM appears

| Where | What |
|---|---|
| `Z2_<x>_<y>_<lod>.cnk` | terrain chunk geometry: heights, ground types, meshes (CNK0-CNK5 by LOD) |
| `Z2_<x>_<y>_<lod>.ctg_pc` | baked terrain textures, the far ground (CTG0-CTG5 by LOD) |
| `%s_IrradianceVolume_%d_%d.xml.lzham` | named in the exe (lighting probes) |

KotK Z2: 9,552 `.cnk` (2.0 GB packed) and 7,056 `.ctg_pc` (1.6 GB packed, **15 GB
decoded**: 8 x 512x512 DXT5 each; even = colour, odd = normal/spec-like).

## 2. The container (16 bytes, little-endian)

```
magic[4]  "CNK0".."CNK5" | "CTG0".."CTG5"     digit = level of detail
u32 version
u32 uncompressed size
u32 compressed size      = file size - 16
... LZHAM stream
```
`lzham.container.read_container / write_container`. CTG payload: `[u32 len][DDS] x 8`
(`ctg_textures`).

## 3. The stream wrapper (zlib-style)

```
0x5E  CMF: CM = 14 (LZHAM), CINFO = 5 -> dict_size_log2 = 5 + 15 = 20 (1 MiB window)
0xD1  FLG: FLEVEL 3, FDICT 0, (0x5E<<8 | 0xD1) % 31 == 0
...   the LZHAM bitstream
u32   adler32 of the decoded data, big-endian, after a byte align
```
Every game stream is `5E D1`. The window size matters: it sets the number of distance
slots (`m_num_lzx_slots`), which sets the main table's symbol count - a wrong window
desynchronises the very first Huffman table.

## 4. The bitstream

One MSB-first bitstream carries three interleaved kinds of data:

* **raw bits** - headers, distance extra bits, huge-length escapes (`decode_bits`);
* **prefix codes** - canonical Huffman symbols from *quasi-adaptive* models;
* **arithmetic coding** - binary decisions with 11-bit adaptive probabilities.

The arithmetic coder has no separate byte stream: when the decoder renormalises
(interval length < 2^24) it pulls the next **8 bits from the same bit buffer**, at
whatever bit position the reader is at. A compressed block starts by pulling 4 bytes.

```
2 bits   flags: fast_table_updating, use_polar_codes      (game: 0, 0)
repeat:
  2 bits block type
    0 sync      2 bits flush type, align, 0x0000, 0xFFFF
    1 comp      4 arith bytes, 2 bits flush type (0 none, 1 reset update rates,
                2 reset all models), symbols until end-of-block, align
    2 raw       24 bits (n-1), 8 bits check = xor of the three bytes, align, n bytes
    3 eof
align, adler32 (16 + 16 bits)
```
Game streams are a single comp block per file (no sync blocks seen).

### 4.1 Models (persist across blocks unless a flush resets them)

| Model | Kind | Symbols | Chosen by |
|---|---|---|---|
| `lit[64]` | Huffman | 256 | top 3 bits of prev char and prev-prev char |
| `delta_lit[64]` | Huffman | 256 (byte XOR rep0 byte) | top 3 bits of the two bytes at rep0 |
| `main` | Huffman | 2 + (slots-1) x 8 = 322 for 1 MiB | - |
| `rep_len[2]` | Huffman | 257 (len-2, 256 = huge) | state < 7 or not |
| `large_len[2]` | Huffman | 250 (len-9, 249 = huge) | state < 7 or not |
| `dist_lsb` | Huffman | 16 | - |
| `is_match[768]` | arith | bit | state x 64 + (prev char >> 2) |
| `is_rep`, `is_rep0`, `is_rep0_single_byte`, `is_rep1`, `is_rep2` | arith | bit | state (12) |

**Quasi-adaptive Huffman**: every symbol bumps its frequency; after `update_cycle`
symbols the code is rebuilt from the frequencies (the symbol that closes a cycle is
coded with the OLD code). The cycle starts at 8 and grows x1.25 (slow updating, as the
game) up to `(max(24, n) + 6) * 12` (LZHAM_MORE_FREQUENT_TABLE_UPDATING). Frequencies
halve (rounding up) when the total reaches 32,768. Codes are limited to 16 bits;
canonical order is (length, symbol).

**Arithmetic bits**: `x = p0 * (length >> 11)`; value < x -> 0 (length = x, p0 +=
(2048 - p0) >> 5) else 1 (value -= x, length -= x, p0 -= p0 >> 5).

### 4.2 The decoder's state machine (per comp block: state 0, rep0-3 = 1)

```
is_match? 0 -> literal: state < 7 ? lit[pred] : delta_lit[pred] ^ byte_at_rep0
                        state = next_state[state]  (0,0,0,0,1,2,3, 4,5,6,4,5)
          1 -> is_rep? 1 -> is_rep0? 1 -> single byte? 1 -> 1 byte from rep0     (state 9|11)
                                                     0 -> rep_len               (state 8|11)
                                      0 -> rep_len, is_rep1? rep1 : is_rep2? rep2 : rep3
                                           (the chosen distance moves to rep0)  (state 8|11)
                       0 -> main symbol: 0 = end of block, 1 = partial state reset,
                            else len = (s & 7) + 2 (9 -> + large_len), slot = (s >> 3) + 1
                            distance = base[slot] + extra
                              extra bits < 3: raw bits; else raw (nb - 4) bits << 4 + dist_lsb
                            rep3..rep0 shift, rep0 = distance                     (state 7|10)
huge length (len symbol escape): up to 3 one-bits then a zero, then 8/10/12/16 bits
  over bases 258 / 514 / 1538 / 5634 (max 65,536).
```
Distance slots: extra bits 0,0,0,0,1,1,2,2,...(+1 every two slots, max 25); 1 MiB
window -> 40 slots.

## 5. Writing streams (`lzcomp.py`)

The encoder runs the decoder's state machine forwards through identical models (the
Huffman model subclass only differs in building *codes* instead of *decode tables*).
The hard part is the arithmetic bytes: the decoder reads them at renormalisation points
inside the shared bitstream, but an encoder's carry can still change a byte it has
already produced. So each block is two passes:

1. model and code everything; arithmetic bytes go into their own buffer (carries just
   walk back through it); record, per decision, how many bytes the decoder will pull
   before it (the interval evolves identically on both sides);
2. write the bitstream, dropping arithmetic bytes in at those points: 4 at block
   start, `r` before each decision; the block ends with the interval base's 4 bytes.

Parsing: hash chains (3-byte hash, 24 candidates by default), one-step lazy matching,
all four rep distances, single-byte rep0, huge lengths; length-2 matches only within
2,047 bytes (cMaxLen2MatchDist). Output is about 17-20% larger than the game's own
encoder on busy terrain and much smaller than zlib -9; raw-block (stored) streams, which
the zone builder used to write, are ~3.5x larger again.

## 6. Speed and scale

| | busy 2.8 MB CTG | notes |
|---|---|---|
| reference decoder (`lzdecomp`) | ~1.9 s | readable, line-by-line port |
| fast decoder (`fastdecomp`) | ~1.4 s | fused loop, byte-identical; the package default |
| encoder (`lzcomp`) | ~15 s | `chain` trades size for time |
| `lzham.batch` / `lzham_tool.py verify` | 672 Z2 files (992 MB out) in 50 s | 11 processes + SHA-1 cache |

Pure Python's floor is per-symbol interpretation; the next step for a whole-map rewrite
(all 7,056 CTG files) is either the batch path (hours -> tens of minutes) or a compiled
decoder/encoder (Node/V8 is on this PC; there is no C compiler).

## 7. Tools

```
py -3 lzham_tool.py info    FILE --decode
py -3 lzham_tool.py verify  "…\cnk\Z2_*_3.cnk" --cache DIR
py -3 lzham_tool.py decode  FILES -o DIR          (CTG files also split into .dds)
py -3 lzham_tool.py encode  PAYLOAD -o FILE --magic CNK0 --version 1
py -3 lzham_tool.py recompress FILES -o DIR       (game vs ours, per file)
py -3 lzham_tool.py bench   FILE
py -3 -m unittest test_lzham test_lzham_codec
```

## 8. Pitfalls met so far

* The window is 1 MiB: a match distance must stay below 2^20 (the game's decoder is the
  buffered, ring-buffer build).
* Streams live inside `.pack` files: each chunk's file list must fit the 8 KB the
  client reads, chunks are 8 KB-aligned blocks, and no two entries may share bytes
  (see pack1tool) - a stream can be perfect and still crash the client from a bad pack.
* The flags must stay `0, 0`: polar codes / fast updating change every model's rebuild
  schedule; the game has only ever been seen with both off.
* Not yet proven: the game's *own* decoder on our encoder's output. Its code is in
  H1Z1.exe (probably the protected second `.text`); running it under the offline
  emulator (launcher/debugger/re/h1emu.py) is the next check.
