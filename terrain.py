"""Terrain chunks: the shape of the ground, once LZHAM is out of the way.

A zone states the grid its chunks fill in - Z1 is 128x128 tiles of 64 units,
65 vertices across each, heights scaled by tile_height 0.03125 - and the chunks
themselves carry the heights, the vertex colours and the per-tile eco layers
that say which flora grows where.

The layout is checked the way the zone parser is: every section is walked to its
declared end and the parse must land exactly on the end of the buffer. A chunk
that does not is reported rather than half-read, because a terrain tool that
silently mis-reads heights produces a world that looks plausible and is wrong.
"""
import struct
from collections import namedtuple

HEIGHT_SCALE = 1.0/32.0        # zone tile_height 0.03125: heights are i32nds of a unit
TILE_SIZE = 64.0               # zone tile_size
TILES_PER_CHUNK_SIDE = 4       # a .cnk covers 4x4 tiles = 256 world units

Eco = namedtuple('Eco', 'id floras')
Tile = namedtuple('Tile', 'x y unk1 unk2 ecos index unk3 unk4 image_data')
Vertex = namedtuple('Vertex', 'x y hf hn color1 color2')
Batch = namedtuple('Batch', 'unk index_offset index_count vertex_offset vertex_count')


class ChunkError(ValueError):
    """A chunk that does not parse to its own end, so its heights cannot be trusted."""


class Reader:
    def __init__(self, data):
        self.data, self.at = data, 0

    def u32(self):
        value, = struct.unpack_from('<I', self.data, self.at)
        self.at += 4
        return value

    def i32(self):
        value, = struct.unpack_from('<i', self.data, self.at)
        self.at += 4
        return value

    def take(self, count):
        if self.at + count > len(self.data):
            raise ChunkError(f'chunk ends early: wanted {count} bytes at {self.at} of {len(self.data)}')
        out = self.data[self.at:self.at+count]
        self.at += count
        return out

    def array(self, fmt, count):
        size = struct.calcsize('<'+fmt)
        raw = self.take(size*count)
        return list(struct.iter_unpack('<'+fmt, raw))


def parse_chunk(payload, name='', magic='CNK0'):
    """Decode a CNK0 payload (already decompressed) into its parts.

    CNK0 is the chunk that carries the ground: the height samples, the mesh and
    the per-tile eco layers. CNK1..CNK5 are the lower levels of detail and do
    NOT share this layout - they were tried and desynchronise within the first
    few dozen bytes - so they are refused here rather than mis-read. Nothing in
    a terrain tool needs them: they are what the game draws in the distance.
    """
    if magic and magic != 'CNK0':
        raise ChunkError(f'{name or "chunk"}: {magic} is a level-of-detail chunk; only CNK0 carries the heights')
    r = Reader(payload)
    tiles = []
    for _ in range(r.u32()):
        x, y, unk1, unk2 = r.i32(), r.i32(), r.i32(), r.i32()
        ecos = []
        for _ in range(r.u32()):
            eco_id = r.u32()
            floras = []
            for _ in range(r.u32()):
                floras.append(r.array('2I', r.u32()))       # layers of (unk1, unk2)
            ecos.append(Eco(eco_id, floras))
        index, unk3, unk4 = r.u32(), r.u32(), r.u32()
        # A tile ends after its image data. H1emu's schema has a layer_textures
        # array here as well, but in H1Z1's own chunks the next tile follows
        # immediately - tile 1 of Z1_20_-28_0 begins at the byte after tile 0's
        # four image bytes - so reading one would swallow the next tile's x.
        image_data = r.take(r.u32())
        tiles.append(Tile(x, y, unk1, unk2, ecos, index, unk3, unk4, image_data))

    # Not unknown: this is the zone's verts_per_tile, 65 for every H1Z1 chunk.
    verts_per_tile = r.i32()
    samples = r.array('hBB', r.u32())                        # height, then two bytes per sample
    indices = [v[0] for v in r.array('H', r.u32())]
    vertices = [Vertex(*v) for v in r.array('4h2I', r.u32())]
    batches = [Batch(*v) for v in r.array('5I', r.u32())]
    draws = [r.take(320) for _ in range(r.u32())]
    shorts = [v[0] for v in r.array('H', r.u32())]
    vectors = r.array('3f', r.u32())
    tileinfo = [r.take(64) for _ in range(r.u32())]
    # One float closes the chunk - 1.0 in every chunk seen.
    trailer, = struct.unpack_from('<f', r.take(4))

    if r.at != len(payload):
        raise ChunkError(f'{name or "chunk"}: parsed to {r.at} of {len(payload)} bytes '
                         f'({len(payload)-r.at} left over)')
    return {'tiles': tiles, 'verts_per_tile': verts_per_tile, 'height_samples': samples,
            'indices': indices, 'vertices': vertices, 'batches': batches, 'draws': draws,
            'shorts': shorts, 'vectors': vectors, 'tileinfo': tileinfo, 'trailer': trailer}


def height_grid(chunk):
    """Raw height samples as (side, values), for inspection rather than lookup.

    The samples are a square - 260x260 for a Z1 chunk - but they are NOT one
    continuous image. They are 16 blocks of 65x65, one per tile, so reading them
    as a picture interleaves tiles that sit hundreds of metres apart. Use
    height_at() for anything positional.
    """
    samples = chunk['height_samples']
    side = int(round(len(samples) ** 0.5))
    if side*side != len(samples):
        raise ChunkError(f'{len(samples)} height samples is not a square')
    return side, [sample[0]*HEIGHT_SCALE for sample in samples]


def tile_origin(tile):
    """World (x, z) of a tile's corner.

    The tile's two coordinates are NOT (x, z) in that order: tile.y indexes the
    world X axis and tile.x indexes Z. Taking them the obvious way round leaves
    the decoded ground uncorrelated with where the game puts its props
    (r = +0.35); this way round gives r = +0.99 against 831 boulders.
    """
    return tile.y*TILE_SIZE, tile.x*TILE_SIZE


def height_at(chunk, x, z):
    """Ground height in world units at world (x, z), or None if outside this chunk.

    Within a tile the samples run column-major - index = local_x*65 + local_z -
    which is both what the chunk's own mesh vertices say and the reading that
    makes the surface smoothest.
    """
    heights = chunk['height_samples']
    per_tile = chunk.get('verts_per_tile') or 65
    span = per_tile - 1
    for i, tile in enumerate(chunk['tiles']):
        ox, oz = tile_origin(tile)
        if not (ox <= x < ox + TILE_SIZE and oz <= z < oz + TILE_SIZE):
            continue
        u = (x - ox)*span/TILE_SIZE
        v = (z - oz)*span/TILE_SIZE
        u0, v0 = int(u), int(v)
        u1, v1 = min(u0+1, span), min(v0+1, span)
        fu, fv = u - u0, v - v0
        base = i*per_tile*per_tile

        def at(a, b):
            return heights[base + a*per_tile + b][0]
        top = at(u0, v0)*(1-fu) + at(u1, v0)*fu
        bottom = at(u0, v1)*(1-fu) + at(u1, v1)*fu
        return (top*(1-fv) + bottom*fv)*HEIGHT_SCALE
    return None


def chunk_name(zone, x, z, lod=0):
    """The chunk file covering world (x, z): Zone_<z index>_<x index>_<lod>.cnk."""
    span = TILE_SIZE*TILES_PER_CHUNK_SIDE
    return f'{zone}_{int(z // span)*TILES_PER_CHUNK_SIDE}_{int(x // span)*TILES_PER_CHUNK_SIDE}_{lod}.cnk'


def surface_mesh(chunk, step=1):
    """The chunk's ground as (positions, indices) in world units, for drawing.

    `step` thins the grid - 1 gives every sample, 4 gives a quarter-resolution
    mesh, which is usually enough for a viewport and a sixteenth of the work.
    """
    per_tile = chunk.get('verts_per_tile') or 65
    span = per_tile - 1
    positions, indices = [], []
    for i, tile in enumerate(chunk['tiles']):
        ox, oz = tile_origin(tile)
        base = i*per_tile*per_tile
        first = len(positions)//3
        columns = list(range(0, per_tile, step))
        if columns[-1] != span:
            columns.append(span)
        for a in columns:
            for b in columns:
                positions += [ox + a*TILE_SIZE/span,
                              chunk['height_samples'][base + a*per_tile + b][0]*HEIGHT_SCALE,
                              oz + b*TILE_SIZE/span]
        n = len(columns)
        for a in range(n-1):
            for b in range(n-1):
                p = first + a*n + b
                indices += [p, p+1, p+n, p+1, p+n+1, p+n]
    return positions, indices


def chunk_origin(name):
    """World (x, z) of a chunk's corner, from its own file name: Zone_<x>_<y>_<lod>."""
    parts = name.rsplit('.', 1)[0].split('_')
    if len(parts) < 4:
        raise ChunkError(f'{name}: not a Zone_<x>_<y>_<lod> chunk name')
    return int(parts[-3])*TILE_SIZE, int(parts[-2])*TILE_SIZE


def load_chunk(packs, name):
    """Read, decompress and parse one named chunk out of the game archives."""
    from lzham import decompress_stream
    raw = packs.read(name)
    magic = raw[:4].decode('latin-1')
    version, uncompressed, compressed = struct.unpack_from('<3I', raw, 4)
    if 16 + compressed != len(raw):
        raise ChunkError(f'{name}: payload is {len(raw)-16} bytes, header says {compressed}')
    payload = decompress_stream(raw[16:])
    if len(payload) != uncompressed:
        raise ChunkError(f'{name}: decoded {len(payload)} bytes, header says {uncompressed}')
    chunk = parse_chunk(payload, name, magic)
    chunk['name'], chunk['magic'], chunk['version'] = name, magic, version
    return chunk
