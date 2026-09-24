#!/usr/bin/env python3
"""Reader for the first-generation ForgeLight ``.pack`` archive.

The current H1Z1 client ships ``.pack2`` (``PAK\\x01``, little-endian, 64-bit
Jenkins name hashes, an optional ``{NAMELIST}`` asset). The older PS3-era builds
shipped ``.pack``/``.pack0``/``.pack1``… , which is a different container
entirely:

    chunk:
        uint32 be   offset of the next chunk, 0 on the last one
        uint32 be   number of files in this chunk
        per file:
            uint32 be   length of the name
            bytes       the name itself, stored as text
            uint32 be   absolute offset of the payload
            uint32 be   length of the payload
            uint32 be   CRC-32 of the payload

Names are stored literally, so nothing has to be recovered by hashing, and
payloads are stored uncompressed. There is no magic number at the start of the
file - the first four bytes are the next-chunk offset - so an archive is
identified by parsing it rather than by its extension, and every offset, length
and name length is bounds-checked before it is used.

Confirmed against a real console-era depot of H1Z1 King of the Kill: 256
archives, 55,837 entries and 509 chunks indexed in five seconds with nothing
unreadable, and every stored CRC-32 matched its payload across the sample that
was checked byte for byte. Names are unique across the whole set.

Reading is therefore trusted; writing is not. ``write_archive`` exists for the
tests and is deliberately not wired into the workbench, because an archive this
tool repacked has never been handed back to the game to load.
"""
import binascii
import os
import struct
import sys

MAX_NAME = 1024
MAX_FILES_PER_CHUNK = 200_000
MAX_FILES = 5_000_000
MAX_ASSET = 512 * 1024 * 1024
# The client's per-chunk read of a file list (see write_archive).
CHUNK_HEADER_LIMIT = 0x2000


class NotPack1(ValueError):
    """Raised when a file is not a first-generation pack, so callers can fall back."""


def _u32(data, at):
    return struct.unpack_from('>I', data, at)[0]


def read_archive(path):
    """Index one .pack file. Raises NotPack1 if it does not parse as one."""
    size = os.path.getsize(path)
    if size < 8:
        raise NotPack1('file is too small to hold a chunk header')
    entries = []
    seen_chunks = set()
    with open(path, 'rb') as source:
        chunk_at = 0
        while True:
            if chunk_at in seen_chunks:
                raise NotPack1(f'chunk at {chunk_at} repeats; the chunk chain loops')
            seen_chunks.add(chunk_at)
            if chunk_at + 8 > size:
                raise NotPack1(f'chunk header at {chunk_at} runs past the end of the file')
            source.seek(chunk_at)
            header = source.read(8)
            next_at, count = _u32(header, 0), _u32(header, 4)
            if count > MAX_FILES_PER_CHUNK:
                raise NotPack1(f'chunk at {chunk_at} claims {count} files')
            if len(entries) + count > MAX_FILES:
                raise NotPack1('archive claims more files than this reader will index')
            if next_at and not chunk_at < next_at < size:
                raise NotPack1(f'chunk at {chunk_at} points forward to {next_at}, which is not inside the file')
            for _ in range(count):
                raw = source.read(4)
                if len(raw) != 4:
                    raise NotPack1('file record is truncated')
                name_length = _u32(raw, 0)
                if not 0 < name_length <= MAX_NAME:
                    raise NotPack1(f'file name length {name_length} is out of range')
                name_bytes = source.read(name_length)
                if len(name_bytes) != name_length:
                    raise NotPack1('file name is truncated')
                tail = source.read(12)
                if len(tail) != 12:
                    raise NotPack1('file record is truncated')
                offset, length, crc = struct.unpack('>3I', tail)
                if offset + length > size:
                    raise NotPack1(f'payload at {offset}+{length} runs past the end of the file')
                try:
                    name = name_bytes.decode('ascii')
                except UnicodeDecodeError:
                    name = name_bytes.decode('latin-1')
                if any(ch < ' ' for ch in name):
                    raise NotPack1('file name holds control characters')
                entries.append({'name': name, 'offset': offset, 'length': length, 'crc': crc})
            if not next_at:
                break
            chunk_at = next_at
    if not entries:
        raise NotPack1('no file records found')
    return {'path': os.path.abspath(path), 'version': 1, 'chunks': len(seen_chunks),
            'entries': entries, 'file_size': size}


def looks_like(path):
    """Cheap check before a full parse: does the head read as a v1 chunk header?"""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as source:
            header = source.read(8)
    except OSError:
        return False
    if len(header) != 8 or header[:4] == b'PAK\x01':
        return False
    next_at, count = _u32(header, 0), _u32(header, 4)
    if count == 0 or count > MAX_FILES_PER_CHUNK:
        return False
    return next_at == 0 or 8 < next_at < size


def read_asset(handle, entry):
    if entry['length'] > MAX_ASSET:
        raise ValueError('asset exceeds the 512 MiB read limit')
    handle.seek(entry['offset'])
    data = handle.read(entry['length'])
    if len(data) != entry['length']:
        raise ValueError('asset is truncated')
    return data


def verify(entry, data):
    """True when the stored CRC-32 matches; None when the archive stores none.

    The last field of a record is a CRC-32 of the payload; every entry checked in
    a real depot matched. A mismatch is still reported rather than raised, so a
    damaged archive can be extracted and inspected instead of refusing to open.
    """
    if not entry.get('crc'):
        return None
    return binascii.crc32(data) & 0xffffffff == entry['crc']


def _chunks_by_header(files, limit):
    """Split files so every chunk header (8 + per file 4 + name + 12 bytes) fits in limit."""
    chunks, current, used = [], [], 8
    for name, data in files:
        size = 4 + len(name.encode('ascii')) + 12
        if current and used + size > limit:
            chunks.append(current)
            current, used = [], 8
        current.append((name, data))
        used += size
    return chunks + [current] if current or not chunks else chunks


def write_archive(path, files, per_chunk=None, dedupe=False):
    """Write a v1 archive. Used by the tests to round-trip the reader.

    ``files`` is an iterable of (name, data). ``dedupe`` is accepted but ignored:
    every entry gets bytes of its own, as in every stock pack. (Sharing was first
    blamed for the start-up crash at 0x1579ca2; the real cause was chunk layout -
    see below and pack_startup_check.py - but the client builds its free-range map
    from the gaps between entries, so overlapping entries stay off the table.)

    Chunks are split so each chunk header fits in CHUNK_HEADER_LIMIT bytes: the client
    reads a chunk's file list with one 0x2000-byte read (H1Z1.exe 0x157db10 -> read at
    0x157dc95) and never sees entries past it - the largest stock chunk header is 8,185
    bytes. A single 102 KB header left 1,147 of an overlay's 1,254 files unreachable.

    Layout, as in the stock packs: each chunk is a header block of CHUNK_HEADER_LIMIT bytes
    (the file list, zero-padded) followed by that chunk's data; the next chunk starts after it.
    At start-up the client builds a free-range map from the gaps between file data and then
    reserves [chunk offset, chunk offset + 0x2000) for every chunk (H1Z1.exe 0x157de80 ->
    0x1579c30); a chunk header inside file data or inside another chunk's 8 KB crashes it
    (null free-block lookup, AV at 0x1579ca2) - which chunk headers written back to back did.
    """
    files = [(str(name), bytes(data)) for name, data in files]
    if per_chunk:
        chunks = [files[i:i+per_chunk] for i in range(0, len(files), per_chunk)]
    else:
        chunks = _chunks_by_header(files, CHUNK_HEADER_LIMIT)
    out = bytearray()
    for index, chunk in enumerate(chunks):
        header_at = len(out)
        header_size = 8 + sum(4 + len(name.encode('ascii')) + 12 for name, _ in chunk)
        block = max(CHUNK_HEADER_LIMIT, header_size)  # per_chunk callers may exceed the limit
        data_at = header_at + block
        next_at = data_at + sum(len(data) for _, data in chunk) if index + 1 < len(chunks) else 0
        header = bytearray(struct.pack('>2I', next_at, len(chunk)))
        at = data_at
        for name, data in chunk:
            encoded = name.encode('ascii')
            header += struct.pack('>I', len(encoded)) + encoded
            header += struct.pack('>3I', at, len(data), binascii.crc32(data) & 0xffffffff)
            at += len(data)
        out += header + b'\0' * (block - len(header))
        for _, data in chunk:
            out += data
    with open(path, 'wb') as target:
        target.write(out)
    return len(out)


def main(argv):
    if len(argv) < 2:
        print(__doc__.strip())
        return 2
    info = read_archive(argv[1])
    print(f'{os.path.basename(info["path"])}: {len(info["entries"])} files in {info["chunks"]} chunk(s), '
          f'{info["file_size"]:,} bytes')
    with open(info['path'], 'rb') as source:
        for entry in info['entries'][:int(argv[2]) if len(argv) > 2 else 20]:
            state = {True: 'crc ok', False: 'CRC MISMATCH', None: 'no crc'}[verify(entry, read_asset(source, entry))]
            print(f'  {entry["name"]:60} {entry["length"]:>10,}  {state}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
