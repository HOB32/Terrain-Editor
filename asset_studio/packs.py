"""Bounded, read-only access to installed Forgelight PACK2 archives."""
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
import struct
import zlib

MAX_ASSET = 64 * 1024 * 1024
MASK = (1 << 64) - 1
TABLE = []
for value in range(256):
    for _ in range(8):
        value = (value >> 1) ^ (0x95AC9329AC4BC9B5 if value & 1 else 0)
    TABLE.append(value)


def name_hash(name):
    value = MASK
    for byte in name.upper().encode('ascii'):
        value = TABLE[(value ^ byte) & 255] ^ (value >> 8)
    return value ^ MASK


def basename(name):
    return PureWindowsPath(name).name


@dataclass(frozen=True)
class Entry:
    pack: Path
    offset: int
    size: int
    flags: int
    crc: int


class Packs:
    format = 'pack2'

    def __init__(self, root, exclude=()):
        self.root = Path(root).resolve()
        self.entries = {}
        archives = [p for p in sorted(self.root.glob('*.pack2')) if p.name.lower() not in {n.lower() for n in exclude}]
        self.archives = archives
        if not archives:
            raise ValueError(f'No PACK2 archives found in {self.root}. Use --assets to select Resources/Assets.')
        for path in archives:
            size = path.stat().st_size
            with path.open('rb') as source:
                header = source.read(32)
                if len(header) != 32 or header[:4] != b'PAK\x01':
                    raise ValueError(f'Unsupported PACK2 header: {path.name}')
                count, = struct.unpack_from('<I', header, 4)
                offset, = struct.unpack_from('<Q', header, 16)
                if count > 2_000_000 or offset < 32 or offset + count * 32 > size:
                    raise ValueError(f'Invalid PACK2 index: {path.name}')
                source.seek(offset)
                for key, start, length, flags, crc in struct.iter_unpack('<QQQII', source.read(count * 32)):
                    if start < 32 or start + length > size:
                        raise ValueError(f'Invalid asset bounds: {path.name}')
                    self.entries[key] = Entry(path, start, length, flags, crc)
        self.archive_count = len(archives)

    def find(self, name):
        for candidate in (name, basename(name)):
            try:
                row = self.entries.get(name_hash(candidate))
            except UnicodeEncodeError:
                continue
            if row:
                return row
        return None

    def read(self, name):
        row = self.find(name)
        if row is None:
            raise ValueError(f'Asset is absent: {basename(name)}')
        if row.size > MAX_ASSET:
            raise ValueError('Asset exceeds the 64 MiB extraction limit')
        with row.pack.open('rb') as source:
            source.seek(row.offset)
            data = source.read(row.size)
        if len(data) != row.size:
            raise ValueError('Truncated asset')
        if row.flags & 1:
            if len(data) < 10:
                raise ValueError('Truncated compressed asset')
            decoder = zlib.decompressobj(-15)
            data = decoder.decompress(data[10:], MAX_ASSET + 1)
            if len(data) > MAX_ASSET or not decoder.eof:
                raise ValueError('Invalid or oversized compressed asset')
        return data
