"""LZHAM for H1Z1 terrain: read AND write the game's .cnk / .ctg_pc streams.

H1Z1 keeps its terrain in ~21,700 .cnk and .ctg_pc chunks (KotK Z2 alone: 9,552 chunk
files and 7,056 baked-texture files), and every one is an LZHAM stream. LZHAM is not in
the standard library, there is no binding for it here, and this machine has no C
compiler to build one, so this package is a pure-Python implementation of both halves.

It is the *alpha* bitstream (LZHAM_DLL_VERSION 0x1008), not LZHAM 1.x: cnkdec, the tool
that already decodes these files, bundles that lzham.h and calls inflateInit2(&stream, 20);
CMF 0x5E carries CINFO 5 = dict_size_log2 20 (a 1 MiB window) in every chunk. Every
decode is checked against two numbers the game wrote independently - the stream's
trailing adler32 and the container's uncompressed size.

Modules (FORMAT.md explains the bitstream from the top):
    lzdecomp      the reference decoder: a line-by-line port of lzham_lzdecomp.cpp
    fastdecomp    the same decoder fused into one loop (byte-identical, ~1.3x faster)
    lzcomp        the encoder: the decoder's mirror image, writes game-compatible streams
    container     the 16-byte .cnk/.ctg header, CTG1 texture split, stored streams
    batch         many files at once over every core, with a decode cache
    models, prefix_coding, symbol_codec   the adaptive models, Huffman tables, bit/arith coder

    from lzham import decompress_stream, compress_stream
    payload = decompress_stream(stream)             # stream starts at 0x5E 0xD1
    stream = compress_stream(payload)               # the game's settings: 1 MiB window, Huffman
"""
from .lzdecomp import LzhamDecompressError, decompress, decompress_stream as decompress_stream_reference
from .fastdecomp import decompress_stream
from .lzcomp import compress, compress_stream
from .container import Container, read_container, write_container, store_stream, ctg_textures

__all__ = ['decompress', 'decompress_stream', 'decompress_stream_reference', 'compress', 'compress_stream',
           'LzhamDecompressError', 'Container', 'read_container', 'write_container', 'store_stream',
           'ctg_textures']
