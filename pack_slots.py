"""Placeholder packs that keep the KotK client loading every overlay pack.

The depot client opens Resources/Assets/Assets_000.pack upward and stops at the third
missing number (H1Z1.exe 0x1415796a0). Overlays live at 256 and above and several tools
write them (the terrain zone build at 256, other Dev Hub overlays at 261, Christmas at
287), so the numbers between must exist. A placeholder is a tiny pack holding only
z1br_pack_slot_<n>.txt; any tool may write, keep or remove one. `repair` is the one rule:
fill every gap below the highest real overlay, and drop placeholders above it.
"""
import re
from pathlib import Path

import pack1tool

FIRST_OVERLAY = 256
SLOT_NAME = re.compile(r'^z1br_pack_slot_\d+\.txt$', re.I)
PACK_NAME = re.compile(r'^Assets_(\d{3,})\.pack$', re.I)
PACK_DIR_PREFIX = '.\\Resources\\Assets/Assets_'     # exactly what the client hashes (pack order)


def path_key(path):
    """The client's pack-order key for a pack path (H1Z1.exe 0x1415792e0)."""
    def s32(v):
        v &= 0xFFFFFFFF
        return v - (1 << 32) if v & 0x80000000 else v
    r = 0
    for ch in path.encode('latin-1'):
        r = s32((r + (ch - 256 if ch > 127 else ch)) * 0x401)
        r = s32(r ^ (r >> 6))
    c = s32(r * 9)
    c = s32(c ^ (c >> 11))
    return s32(c * 0x8001)


def slot_path(assets, n):
    return Path(assets) / f'Assets_{n:03d}.pack'


def is_placeholder(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 4096:
        return False
    try:
        entries = pack1tool.read_archive(str(path))['entries']
    except Exception:
        return False
    return bool(entries) and all(SLOT_NAME.match(e['name']) for e in entries)


def write_placeholder(assets, n):
    path = slot_path(assets, n)
    pack1tool.write_archive(path, [(f'z1br_pack_slot_{n}.txt',
                                    f'Placeholder so the client keeps loading packs past Assets_{n:03d}.\n'.encode())])
    return str(path)


def overlays(assets):
    """{number: path} of every Assets_NNN.pack at 256 or above."""
    out = {}
    for p in Path(assets).glob('Assets_*.pack'):
        m = PACK_NAME.match(p.name)
        if m and int(m.group(1)) >= FIRST_OVERLAY:
            out[int(m.group(1))] = p
    return out


def ensure_chain(assets, upto):
    """Write placeholders for every missing number from 256 below `upto`."""
    have = overlays(assets)
    return [write_placeholder(assets, n) for n in range(FIRST_OVERLAY, upto) if n not in have]


def release_unneeded(assets):
    """Remove placeholders above the highest real overlay (they hold nothing up)."""
    have = overlays(assets)
    real = [n for n, p in have.items() if not is_placeholder(p)]
    top = max(real, default=FIRST_OVERLAY - 1)
    removed = []
    for n, p in sorted(have.items()):
        if n > top and is_placeholder(p):
            p.unlink()
            removed.append(str(p))
    return removed


def repair(assets):
    """Fill gaps below the highest real overlay and drop placeholders above it."""
    have = overlays(assets)
    real = [n for n, p in have.items() if not is_placeholder(p)]
    written = ensure_chain(assets, max(real) if real else FIRST_OVERLAY)
    return {'written': written, 'removed': release_unneeded(assets)}
