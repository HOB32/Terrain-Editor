"""Which texture each sub-mesh of a DME draws with, read from its DMAT block.

The Dev Hub reads this through its model importer, which also rewrites
materials; the Terrain tool only needs the read half, so this is that half on
its own. Same checks, same result as the importer's `material(...)[2]`.
"""
import struct

from .dme import Reader, material_hash
from .packs import basename

# DMAT parameter semantic -> the texture's role
ROLES = {-1295769314 & 0xffffffff: 'color', 789998085: 'normal',
         67211600: 'spec', 739857270: 'paintmask', 1716414136: 'detail'}


def texture_bindings(data):
    """[{role: texture file name}] per material, in material order."""
    r = Reader(data)
    if r.take(4) != b'DMAT' or r.count() != 1:
        raise ValueError('Only DMAT v1 is supported')
    table = r.take(r.count(16*1024*1024))
    names = [n.decode('latin1') for n in table.split(b'\0') if n]
    hashes = {material_hash(n.upper()): n for n in names}
    bindings = []
    for _ in range(r.count(1024)):
        start = r.at
        _, length, _, parameters = struct.unpack('<4I', r.take(16))
        if parameters > 4096:
            raise ValueError('Too many material parameters')
        roles = {}
        for _ in range(parameters):
            semantic, kind, _, size = struct.unpack('<4I', r.take(16))
            raw = r.take(size)
            if kind == 4 and size == 4:
                name = hashes.get(struct.unpack('<I', raw)[0])
                if name and semantic in ROLES:
                    roles[ROLES[semantic]] = basename(name)
        if r.at - start != length + 8:
            raise ValueError('Inconsistent DMAT material length')
        bindings.append(roles)
    if r.at != len(data):
        raise ValueError('Unexpected DMAT trailer')
    return bindings
