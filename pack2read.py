#!/usr/bin/env python3
"""
pack2read.py - read Forgelight .pack2 archives (H1Z1, PlanetSide 2, ...)


Format (little-endian):
  Header (0x200 bytes):
    0x00  magic       "PAK\\x01"
    0x04  asset_count uint32
    0x08  file_length uint64
    0x10  map_offset  uint64   -> asset table
    0x18  unknown     uint64   (0x100)
    0x20  checksum    16 bytes (often zero)
    ...   padding to 0x200
  Asset table (32 bytes each, sorted by hash):
    name_hash   uint64  CRC-64 (reversed Jones poly, init/xorout all-ones) of the UPPERCASED name
    offset      uint64
    data_length uint64  (stored size, incl. zip header if compressed)
    zip_flag    uint32  H1Z1: 0x01 = zlib, 0x00 / 0x10 = raw.  Detect by payload magic, not flag.
    data_hash   uint32  CRC-32 of the stored bytes (compressed blob incl. its 8-byte header, or raw data)
  Compressed asset payload:
    "\\xa1\\xb2\\xc3\\xd4" + uncompressed_length (uint32 BE) + zlib stream
  The asset named "{NAMELIST}" holds newline-separated file names, which is
  how hashes are mapped back to names.
"""
import struct
import zlib

MAGIC = b"PAK\x01"
ZIP_MAGIC = b"\xa1\xb2\xc3\xd4"
NAMELIST = "{NAMELIST}"
HEADER_SIZE = 0x200

# ---------------------------------------------------------------- CRC-64
_POLY = 0x95AC9329AC4BC9B5  # bit-reversed Jones polynomial 0xAD93D23594C935A9
_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ _POLY if _c & 1 else _c >> 1
    _TABLE.append(_c)


def crc64(data: bytes) -> int:
    crc = 0xFFFFFFFFFFFFFFFF
    for b in data:
        crc = _TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFFFFFFFFFF


def name_hash(name: str) -> int:
    return crc64(name.upper().encode("utf-8"))  # names are hashed case-insensitively


NAMELIST_HASH = name_hash(NAMELIST)


def read_archive(path):
    with open(path, "rb") as f:
        hdr = f.read(HEADER_SIZE)
        if hdr[:4] != MAGIC:
            raise SystemExit(f"{path}: not a .pack2 file (magic {hdr[:4]!r})")
        asset_count, file_len, map_off, unknown = struct.unpack_from("<IQQQ", hdr, 4)
        checksum = hdr[0x20:0x30]
        f.seek(map_off)
        table = f.read(asset_count * 32)
        entries = []
        for i in range(asset_count):
            h, off, ln, zf, dh = struct.unpack_from("<QQQII", table, i * 32)
            entries.append({"hash": h, "offset": off, "length": ln, "zip_flag": zf, "data_hash": dh})
        return {"unknown": unknown, "checksum": checksum.hex(), "entries": entries}


def is_compressed(raw: bytes) -> bool:
    """Compression is signalled by the payload magic, not reliably by zip_flag."""
    return len(raw) >= 10 and raw[:4] == ZIP_MAGIC and raw[8] == 0x78


def decode_payload(raw: bytes) -> bytes:
    if is_compressed(raw):
        (ulen,) = struct.unpack(">I", raw[4:8])
        data = zlib.decompress(raw[8:])
        if len(data) != ulen:
            raise ValueError("bad uncompressed length")
        return data
    return raw


def read_asset(f, e):
    f.seek(e["offset"])
    return decode_payload(f.read(e["length"]))


def load_names(f, entries):
    for e in entries:
        if e["hash"] == NAMELIST_HASH:
            text = read_asset(f, e).decode("utf-8", "replace")
            return [n for n in text.replace("\r", "").split("\n") if n]
    return []
