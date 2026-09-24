"""The ForgeLight terrain container around an LZHAM stream (.cnk / .ctg_pc).

    offset  size  field
    0       4     magic         b'CNK0'..b'CNK5' chunk geometry, b'CTG0'..b'CTG5' baked textures
                                (the digit is the file's level of detail, as in Z2_x_y_<lod>)
    4       4     version       u32 little-endian
    8       4     uncompressed  u32 LE - size of the decoded payload
    12      4     compressed    u32 LE - bytes that follow (the whole rest of the file)
    16      ...   LZHAM stream  zlib-style: 0x5E 0xD1, the alpha bitstream, adler32 (big-endian)

A CTG payload is eight 512x512 DXT5 textures, each prefixed by its u32 LE length
(349,680 bytes with mips); the even ones are colour, the odd ones normal/spec-like maps.
Z2's 7,056 CTG files hold 15 GB decoded - rewriting them needs real compression (lzcomp),
not stored blocks.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

try:
    from .fastdecomp import decompress_stream
    from .lzcomp import compress_stream
except ImportError:  # pragma: no cover
    from fastdecomp import decompress_stream
    from lzcomp import compress_stream

__all__ = ['Container', 'read_container', 'write_container', 'store_stream', 'ctg_textures']

HEADER = struct.Struct('<4s3I')


@dataclass
class Container:
    magic: str
    version: int
    uncompressed: int
    compressed: int
    stream: bytes           # the LZHAM stream (starts 0x5E 0xD1)

    def decode(self) -> bytes:
        return decompress_stream(self.stream, self.uncompressed)


def read_container(data: bytes) -> Container:
    if len(data) < HEADER.size:
        raise ValueError('not a terrain container: shorter than its 16-byte header')
    magic, version, uncompressed, compressed = HEADER.unpack_from(data, 0)
    if HEADER.size + compressed != len(data):
        raise ValueError(f'container says {compressed} compressed bytes, file has {len(data) - HEADER.size}')
    return Container(magic.decode('latin-1'), version, uncompressed, compressed, bytes(data[HEADER.size:]))


def store_stream(payload: bytes) -> bytes:
    """A zlib-style LZHAM stream of raw (stored) blocks - instant, no compression."""
    import zlib
    out = bytearray(b'\x5e\xd1')
    acc, n = 0, 0

    def put(v, bits):
        nonlocal acc, n
        acc = (acc << bits) | v
        n += bits
        while n >= 8:
            n -= 8
            out.append((acc >> n) & 0xFF)
        acc &= (1 << n) - 1

    def align():
        if n:
            put(0, 8 - n)
    put(0, 2)                                   # fast / polar flags
    at = 0
    while at < len(payload):
        part = payload[at:at + (1 << 24)]
        k = len(part) - 1
        put(2, 2)                               # raw block
        put(k, 24)
        put((k & 0xFF) ^ ((k >> 8) & 0xFF) ^ ((k >> 16) & 0xFF), 8)
        align()
        out += part
        at += len(part)
    put(3, 2)
    align()
    adler = zlib.adler32(payload, 1) & 0xFFFFFFFF
    put(adler >> 16, 16)
    put(adler & 0xFFFF, 16)
    return bytes(out)


def write_container(magic: str, version: int, payload: bytes, compress: bool = True,
                    chain: int = 24, verify: bool = True) -> bytes:
    """A complete .cnk/.ctg file. compress=True writes a real LZHAM stream (lzcomp),
    falling back to stored blocks if that would not be smaller; verify decodes it back."""
    stream = compress_stream(payload, chain=chain) if compress else store_stream(payload)
    if compress:
        stored = store_stream(payload)
        if len(stored) <= len(stream):
            stream = stored
    if verify and decompress_stream(stream, len(payload)) != payload:
        raise AssertionError(f'{magic}: the stream did not decode back to its payload')
    return HEADER.pack(magic.encode('latin-1')[:4].ljust(4, b'\0'), version, len(payload), len(stream)) + stream


def ctg_textures(payload: bytes) -> list[bytes]:
    """The DDS textures inside a CTG payload ([u32 length][DDS] x 8)."""
    out, at = [], 0
    while at + 4 <= len(payload):
        n = struct.unpack_from('<I', payload, at)[0]
        out.append(payload[at + 4:at + 4 + n])
        at += 4 + n
    if at != len(payload):
        raise ValueError('CTG payload does not end on a texture boundary')
    return out
