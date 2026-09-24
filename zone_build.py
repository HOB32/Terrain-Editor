"""Writing zones the KotK client can load: ground chunks, zone files, a pack.

The reading side lives in terrain.py (chunks), lzham/ (decompression) and
zone_format.decode_zone (zone header). This is the inverse, kept separate
because nothing here is trusted until the game itself has loaded it.

Compression. Every .cnk/.ctg is a 16-byte header (magic, version, uncompressed
size, compressed size) then a zlib-wrapped LZHAM alpha stream. LZHAM has a
*stored* block type (cRawBlock = 2), so a valid stream can be written without a
compressor at all:

    CMF 0x5E, FLG 0xD1                       zlib header (dict 2^20), as the game's own
    2 bits  table flags                      copied from the game's streams
    per block (at most 2^24 bytes):
      2 bits  block type = 2 (raw)
      24 bits length - 1
      8 bits  the three length bytes XORed   (the decoder's check)
      align to a byte, then the bytes themselves
    2 bits  block type = 3 (EOF)
    align, adler32 as two 16-bit halves, high first

Bits are written most-significant first, the order the decoder's bit buffer
reads them in. lzham.decompress_stream() is a line-by-line port of the game's
decoder, so a stream it accepts with a matching adler32 is one the game accepts.
"""
import struct
import zlib

import terrain

CHUNK_TILES = terrain.TILES_PER_CHUNK_SIDE          # a .cnk covers 4x4 tiles
TILE = terrain.TILE_SIZE                             # 64 m


# ------------------------------------------------------------------ LZHAM
class _Bits:
    def __init__(self):
        self.out, self.acc, self.n = bytearray(), 0, 0

    def put(self, value, bits):
        for shift in range(bits - 1, -1, -1):
            self.acc = (self.acc << 1) | ((value >> shift) & 1)
            self.n += 1
            if self.n == 8:
                self.out.append(self.acc)
                self.acc, self.n = 0, 0

    def align(self):
        if self.n:
            self.put(0, 8 - self.n)

    def raw(self, data):
        assert self.n == 0
        self.out += data


def lzham_store(payload, flags=0):
    """A zlib-wrapped LZHAM stream holding `payload` in stored (raw) blocks."""
    bits = _Bits()
    bits.raw(b'\x5e\xd1')
    bits.put(flags, 2)
    at, limit = 0, 1 << 24
    while at < len(payload):
        part = payload[at:at + limit]
        n = len(part) - 1
        bits.put(2, 2)
        bits.put(n, 24)
        bits.put((n & 0xff) ^ ((n >> 8) & 0xff) ^ ((n >> 16) & 0xff), 8)
        bits.align()
        bits.raw(part)
        at += len(part)
    bits.put(3, 2)
    bits.align()
    adler = zlib.adler32(payload, 1) & 0xffffffff
    bits.put(adler >> 16, 16)
    bits.put(adler & 0xffff, 16)
    return bytes(bits.out)


def pack_file(magic, version, payload, compress=None):
    """A complete .cnk/.ctg file: header plus LZHAM stream.

    Stored (raw-block) streams by default - instant, and the only kind proven in game so
    far. compress=True (or ZONE_BUILD_COMPRESS=1) writes a real LZHAM stream with
    lzham.lzcomp instead: ~3.5x smaller, a few seconds per chunk, verified by decoding it
    back (lzham/FORMAT.md)."""
    if compress is None:
        import os
        compress = os.environ.get('ZONE_BUILD_COMPRESS') == '1'
    if compress:
        from lzham import write_container
        return write_container(magic, version, payload, compress=True)
    stream = lzham_store(payload)
    return magic.encode('latin-1')[:4] + struct.pack('<3I', version, len(payload), len(stream)) + stream


def unpack_file(raw):
    """(magic, version, payload) of a .cnk/.ctg file."""
    from lzham import decompress_stream
    magic = raw[:4].decode('latin-1')
    version, uncompressed, compressed = struct.unpack_from('<3I', raw, 4)
    if 16 + compressed != len(raw):
        raise ValueError(f'payload is {len(raw) - 16} bytes, header says {compressed}')
    payload = decompress_stream(raw[16:])
    if len(payload) != uncompressed:
        raise ValueError(f'decoded {len(payload)} bytes, header says {uncompressed}')
    return magic, version, payload


# ------------------------------------------------------------------ CNK0
def serialize_cnk0(chunk):
    """The inverse of terrain.parse_chunk: a CNK0 payload from its parts."""
    out = bytearray()
    u32 = lambda v: out.extend(struct.pack('<I', v))
    i32 = lambda v: out.extend(struct.pack('<i', v))

    def array(fmt, rows):
        u32(len(rows))
        for row in rows:
            out.extend(struct.pack('<' + fmt, *(row if isinstance(row, (tuple, list)) else (row,))))
    u32(len(chunk['tiles']))
    for t in chunk['tiles']:
        for v in (t.x, t.y, t.unk1, t.unk2):
            i32(v)
        u32(len(t.ecos))
        for eco in t.ecos:
            u32(eco.id)
            u32(len(eco.floras))
            for layer in eco.floras:
                array('2I', layer)
        for v in (t.index, t.unk3, t.unk4):
            u32(v)
        u32(len(t.image_data))
        out.extend(t.image_data)
    i32(chunk['verts_per_tile'])
    array('hBB', chunk['height_samples'])
    array('H', chunk['indices'])
    array('4h2I', chunk['vertices'])
    array('5I', chunk['batches'])
    u32(len(chunk['draws']))
    for d in chunk['draws']:
        out.extend(d)
    array('H', chunk['shorts'])
    array('3f', chunk['vectors'])
    u32(len(chunk['tileinfo']))
    for d in chunk['tileinfo']:
        out.extend(d)
    out.extend(struct.pack('<f', chunk['trailer']))
    return bytes(out)


def rename_chunk(name, old_zone, new_zone):
    if not name.startswith(old_zone + '_'):
        raise ValueError(f'{name} is not a {old_zone} file')
    return new_zone + name[len(old_zone):]


# ------------------------------------------------------------------ zone file
ZONE_SECTIONS = ('ecos', 'floras', 'invisible_walls', 'objects', 'lights', 'unknowns', 'decals')


def zone_sections(data):
    """Split a v4+ zone into its header and its seven sections, verbatim."""
    magic, version = data[:4], struct.unpack_from('<I', data, 4)[0]
    if magic != b'ZONE' or version < 4:
        raise ValueError(f'expected a ZONE v4+ file, got {magic!r} v{version}')
    offsets = struct.unpack_from('<7I', data, 12)
    if list(offsets) != sorted(offsets) or offsets[0] < 40:
        raise ValueError('zone section offsets are not in order')
    ends = list(offsets[1:]) + [len(data)]
    return data[:offsets[0]], {k: data[o:e] for k, o, e in zip(ZONE_SECTIONS, offsets, ends)}


def build_zone(template, start_x, start_y, tiles_x, tiles_y, sections=None):
    """A zone file from a template zone: its ground types and flora kept, its
    tile grid replaced, and any section swapped for new bytes (an empty section
    is a single u32 count of 0)."""
    header, parts = zone_sections(template)
    parts = dict(parts)
    for key, value in (sections or {}).items():
        if key not in parts:
            raise ValueError(f'unknown zone section {key}')
        parts[key] = value
    header = bytearray(header)
    at = 40
    offsets = []
    body = bytearray()
    for key in ZONE_SECTIONS:
        offsets.append(len(header) + len(body))
        body += parts[key]
    struct.pack_into('<7I', header, 12, *offsets)
    # grid: quads/tile u32, tile size f32, tile height f32, verts/tile u32, tiles/chunk u32, then start x/y, count x/y
    struct.pack_into('<4i', header, at + 20, start_x, start_y, tiles_x, tiles_y)
    return bytes(header + body)


EMPTY = struct.pack('<I', 0)


# ------------------------------------------------------------------ zone objects
def _instance_end(data, o, version):
    """Where one placed instance ends: pos/rot/scale float4s, id, (v7: a u32), a
    flag byte, a 1.0 float, then counted lists A (8-byte rows), B (8-byte rows),
    C (always 0), D (20-byte rows), then 5 bytes. Parsed to the byte over Z2's
    614 296 instances."""
    o += 52 + (4 if version >= 7 else 0) + 1 + 4
    a, = struct.unpack_from('<I', data, o); o += 4 + 8*a
    b, = struct.unpack_from('<I', data, o); o += 4 + 8*b
    c, = struct.unpack_from('<I', data, o); o += 4
    if c:
        raise ValueError(f'object list C = {c} at {o}; this layout is not understood')
    d, = struct.unpack_from('<I', data, o); o += 4 + 20*d
    return o + 5


def objects_section(zone_bytes, keep, shift=None, extra=(), dx=0.0, dz=0.0):
    """A new objects section: the template's instances whose position `keep(x, z)`
    accepts, each raised by `shift(x, z)` metres (ground edited under it), plus
    `extra` placements {actor, pos, rot:[yaw,pitch,roll], scale}. Kept instances
    are copied byte for byte apart from their height."""
    version = struct.unpack_from('<I', zone_bytes, 4)[0]
    _, parts = zone_sections(zone_bytes)
    data = parts['objects']
    o = 0
    count, = struct.unpack_from('<I', data, o); o += 4
    actors = []          # [actor, head bytes after the name (rd[, unk]), [instance bytes]]
    index = {}
    head_size = 8 if version >= 7 else 4
    kept = 0
    for _ in range(count):
        end = data.index(b'\0', o)
        actor = data[o:end]
        o = end + 1
        head = data[o:o + head_size]
        n, = struct.unpack_from('<I', data, o + head_size)
        o += head_size + 4
        rows = []
        for _ in range(n):
            stop = _instance_end(data, o, version)
            x, y, z = struct.unpack_from('<3f', data, o)
            if keep(x, z):
                row = bytearray(data[o:stop])
                dy = shift(x, z) if shift else 0.0
                if dy or dx or dz:
                    struct.pack_into('<3f', row, 0, x + dx, y + dy, z + dz)
                rows.append(bytes(row))
            o = stop
        index[actor.lower()] = len(actors)
        actors.append([actor, head, rows])
        kept += len(rows)
    if o != len(data):
        raise ValueError(f'objects section parsed to {o} of {len(data)} bytes')
    next_id = 0x7f000000
    for item in extra:
        name = item['actor'].encode('latin-1')
        x, y, z = item['pos']
        x, z = x + dx, z + dz
        yaw, pitch, roll = (list(item.get('rot') or [0, 0, 0]) + [0, 0, 0])[:3]
        sx, sy, sz = (list(item.get('scale') or [1, 1, 1]) + [1, 1, 1])[:3]
        row = struct.pack('<12f', x, y, z, 1.0, yaw, pitch, roll, 0.0, sx, sy, sz, 1.0)
        row += struct.pack('<I', next_id) + (b'\0\0\0\0' if version >= 7 else b'')
        row += b'\0' + struct.pack('<f', 1.0) + struct.pack('<4I', 0, 0, 0, 0) + b'\0\0\0\0\x01'
        next_id += 1
        key = name.lower()
        if key not in index:
            index[key] = len(actors)
            actors.append([name, struct.pack('<f', 1500.0) + (b'\0\0\0\0' if version >= 7 else b''), []])
        actors[index[key]][2].append(row)
    out = bytearray()
    used = [a for a in actors if a[2]]
    out += struct.pack('<I', len(used))
    for actor, head, rows in used:
        out += actor + b'\0' + head + struct.pack('<I', len(rows)) + b''.join(rows)
    return bytes(out), kept, len(extra)


# ------------------------------------------------------------------ a whole zone
# Coarser chunk levels cover more tiles: CNK0/1 4 tiles, CNK2 8, CNK3 16, CNK4 32, CNK5 64
# (Z2 has 4096/4096/1024/256/64/16 of them). A chunk is named Zone_<tile x>_<tile y>_<lod>,
# where tile x runs along world Z and tile y along world X (terrain.tile_origin).
LOD_SPAN = {0: 4, 1: 4, 2: 8, 3: 16, 4: 32, 5: 64}
TOME_SPAN = 8          # Z2_<i>_<j>.tome: one per 8x8 tiles (LoginZone's -8..8 grid has tomes -1..0)


def _window(area, align=16):
    """The tile window, aligned to `align` tiles, that holds the area: (a0, b0, size)
    with a = tile x (world Z) and b = tile y (world X). Square, so the zone header's
    two axes cannot be read the wrong way round."""
    x0, z0 = area['origin']
    x1, z1 = x0 + area['size'], z0 + area['size']
    a0, a1 = int(z0 // TILE), int(-(-z1 // TILE))
    b0, b1 = int(x0 // TILE), int(-(-x1 // TILE))
    lo_a, lo_b = (a0 // align)*align, (b0 // align)*align
    size = max(-(-(a1 - lo_a) // align), -(-(b1 - lo_b) // align))*align
    return lo_a, lo_b, size


def _area_sampler(area):
    """The area as two lookups at world (x, z): new_height(x, z, old_units) -> new
    height in 1/32 m units or None outside the area, and eco(x, z) -> ground type or
    None where the area leaves the ground type alone.

    An area copied from the map carries the heights and ground types it started
    from (aseline, groundBase). Only the difference is written back, so
    ground nobody touched keeps the game's full detail rather than the
    medium-detail copy the editor worked on. An area started flat has no
    baseline and replaces the ground outright."""
    import base64
    n, res, size = area['n'], area['res'], area['size']
    ox, oz = area['origin']
    grid = lambda key: struct.unpack(f'<{n*n}f', base64.b64decode(area[key]))
    heights = grid('heights')
    base = grid('baseline') if area.get('baseline') else None
    side = size + 1
    ground = base64.b64decode(area['ground'])
    ground_base = base64.b64decode(area['groundBase']) if area.get('groundBase') else None

    def lerp(H, x, z):
        fx, fz = (x - ox)/res, (z - oz)/res
        if not (0 <= fx <= n - 1 and 0 <= fz <= n - 1):
            return None
        i, j = min(n - 2, int(fx)), min(n - 2, int(fz))
        u, v = fx - i, fz - j
        return ((H[j*n + i]*(1-u) + H[j*n + i + 1]*u)*(1-v)
                + (H[(j+1)*n + i]*(1-u) + H[(j+1)*n + i + 1]*u)*v)

    def new_height(x, z, old):
        h = lerp(heights, x, z)
        if h is None:
            return None
        if base is None:
            return int(round(h*32))
        delta = h - lerp(base, x, z)
        return old + int(round(delta*32)) if abs(delta) > 1e-4 else old

    def eco(x, z):
        i, j = int(round(x - ox)), int(round(z - oz))
        if not (0 <= i < side and 0 <= j < side):
            return None
        k = j*side + i
        if ground_base is not None and ground[k] == ground_base[k]:
            return None
        return ground[k]
    return new_height, eco


def apply_area(chunk, area_height, area_eco, eco_layers):
    """Rewrite a parsed CNK0 so its ground follows the area where they overlap.

    Heights live in three places and all move together, by the change in the
    ground under each point: the 65x65 samples of every tile (1/32 m), the draw
    mesh vertices (two heights each, for detail blending), and the per-tile
    coarse mesh in `vectors` (metres). Shifting by the change rather than
    overwriting keeps the offsets the game's own mesh builder put in.
    Ground types are the tile's eco slots; a type the tile has no slot for gets
    one appended. Returns the number of samples changed."""
    P = chunk.get('verts_per_tile') or 65
    samples = [list(s) for s in chunk['height_samples']]
    tiles = list(chunk['tiles'])
    deltas = []
    changed = 0
    for ti, t in enumerate(tiles):
        ox, oz = terrain.tile_origin(t)
        d = [0]*(P*P)
        slots = {e.id: k for k, e in enumerate(t.ecos)}
        ecos = list(t.ecos)
        for a in range(P):
            for b in range(P):
                x, z = ox + a*TILE/(P-1), oz + b*TILE/(P-1)
                k = ti*P*P + a*P + b
                new = area_height(x, z, samples[k][0])
                if new is None:
                    continue
                new = max(-32768, min(32767, new))
                d[a*P + b] = new - samples[k][0]
                if d[a*P + b]:
                    changed += 1
                samples[k][0] = new
                e = area_eco(x, z)
                if e is not None:
                    if e not in slots and len(ecos) < 255:
                        slots[e] = len(ecos)
                        ecos.append(terrain.Eco(e, [[] for _ in range(eco_layers.get(e, 0))]))
                    if e in slots:
                        samples[k][1] = samples[k][2] = slots[e]
        if len(ecos) != len(t.ecos):
            tiles[ti] = t._replace(ecos=ecos)
        deltas.append(d)
    # draw mesh: one vertex group per tile, in tile order
    groups = sorted({(b.vertex_offset, b.vertex_count) for b in chunk['batches']})
    vertices = list(chunk['vertices'])
    if len(groups) == len(tiles):
        for ti, (vo, vc) in enumerate(groups):
            d = deltas[ti]
            for k in range(vo, vo + vc):
                v = vertices[k]
                if 0 <= v.x < P and 0 <= v.y < P:
                    dd = d[v.x*P + v.y]
                    if dd:
                        vertices[k] = v._replace(hf=max(-32768, min(32767, v.hf + dd)),
                                                 hn=max(-32768, min(32767, v.hn + dd)))
    # coarse per-tile mesh: tileinfo u32[2..3] = vector count and first vector
    vectors = list(chunk['vectors'])
    for ti, info in enumerate(chunk['tileinfo']):
        count, first = struct.unpack_from('<2I', info, 8)
        d = deltas[ti]
        for k in range(first, min(len(vectors), first + count)):
            a, y, b = vectors[k]
            if y == -1.0:
                continue
            ia, ib = max(0, min(P - 2, int(a))), max(0, min(P - 2, int(b)))
            fa, fb = a - ia, b - ib
            dd = ((d[ia*P + ib]*(1-fb) + d[ia*P + ib + 1]*fb)*(1-fa)
                  + (d[(ia+1)*P + ib]*(1-fb) + d[(ia+1)*P + ib + 1]*fb)*fa)
            if dd:
                vectors[k] = (a, y + dd/32.0, b)
    chunk.update(height_samples=[tuple(s) for s in samples], tiles=tiles, vertices=vertices, vectors=vectors)
    return changed


def relocate_cnk0(chunk, da, db):
    """Move a parsed CNK0 by (da, db) tiles: every tile's own coordinates shift.
    Heights, meshes and the coarse mesh are tile-local and stay as they are."""
    chunk['tiles'] = [t._replace(x=t.x + da, y=t.y + db) for t in chunk['tiles']]
    return chunk


def build_dev_zone(packs, area, zone_name, template='Z2', progress=None):
    """Every file of a new zone holding `area`, shaped like the game's own LoginZone.

    What the KotK client accepts, found by loading test zones in it:
    - a renamed zone loads, and so do stored-LZHAM chunks, a zone without a .vnfo,
      and a zone with no objects;
    - a zone whose tile grid is a window somewhere off-centre makes the client log
      out within seconds, and a full-map grid with only some of its chunk files
      leaves it on the loading screen for good.
    So the template's 1 km window around the area is moved to a grid centred on the
    origin (tiles -8..8, like LoginZone), with every file for that grid present.
    Chunk names and each ground chunk's tile coordinates move with it; objects in
    the window come along, shifted the same way. Returns ({file: bytes}, report)."""
    import re
    from zone_format import decode_zone
    say = progress or (lambda *_: None)
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{2,23}', zone_name):
        raise ValueError('Zone name: 3-24 letters and digits, starting with a letter')
    zone_bytes = packs.read(template + '.zone')
    zone = decode_zone(zone_bytes)
    eco_layers = {e['index']: len(e.get('flora_layers', [])) for e in zone.get('ecos', [])}
    a0, b0, size = _window(area)
    half = size // 2
    da, db = -half - a0, -half - b0                  # tile shift into the centred grid
    dx, dz = db*TILE, da*TILE                        # the same shift in world metres (x follows tile y)
    report = {'window': {'tile_x': a0, 'tile_y': b0, 'tiles': size,
                         'world_x': [b0*TILE, (b0 + size)*TILE], 'world_z': [a0*TILE, (a0 + size)*TILE]},
              'offset': [dx, dz], 'copied': 0, 'rewritten': [], 'missing': 0}
    files = {}
    height, eco = _area_sampler(area)
    baseline = bool(area.get('baseline'))
    ax0, az0 = area['origin']
    ax1, az1 = ax0 + area['size'], az0 + area['size']
    say('Collecting objects in the window', 0.03)
    wx0, wz0 = b0*TILE, a0*TILE
    inside = lambda x, z: wx0 <= x < wx0 + size*TILE and wz0 <= z < wz0 + size*TILE
    shift = (lambda x, z: (height(x, z, 0) or 0)/32.0) if baseline else None
    objects, kept, added = objects_section(zone_bytes, inside, shift, area.get('objects') or [], dx, dz)
    report['objects'] = {'kept': kept, 'added': added}
    empty = {k: EMPTY for k in ('invisible_walls', 'lights', 'unknowns', 'decals')}
    files[zone_name + '.zone'] = build_zone(zone_bytes, -half, -half, size, size, {**empty, 'objects': objects})
    names = [(lod, a, b) for lod, span in LOD_SPAN.items() if span <= size
             for a in range(a0, a0 + size, span) for b in range(b0, b0 + size, span)]
    for i, (lod, a, b) in enumerate(names):
        for ext in ('cnk', 'ctg_pc'):
            src = f'{template}_{a}_{b}_{lod}.{ext}'
            if not packs.find(src):
                report['missing'] += 1
                continue
            data = packs.read(src)
            dst = f'{zone_name}_{a + da}_{b + db}_{lod}.{ext}'
            if ext == 'cnk' and lod == 0:
                # every full-detail chunk is re-written: its tiles carry their coordinates
                say(f'Writing ground {dst}', 0.1 + 0.8*i/len(names))
                magic, version, payload = unpack_file(data)
                chunk = terrain.parse_chunk(payload, src, magic)
                span = LOD_SPAN[lod]
                under = not (b*TILE >= ax1 or (b + span)*TILE <= ax0 or a*TILE >= az1 or (a + span)*TILE <= az0)
                changed = apply_area(chunk, height, eco, eco_layers) if under else 0
                relocate_cnk0(chunk, da, db)
                data = pack_file(magic, version, serialize_cnk0(chunk))
                report['rewritten'].append({'file': dst, 'samples_changed': changed})
            else:
                report['copied'] += 1
            files[dst] = data
    say('Copying occlusion data', 0.92)
    for i in range(a0 // TOME_SPAN, -(-(a0 + size) // TOME_SPAN)):
        for j in range(b0 // TOME_SPAN, -(-(b0 + size) // TOME_SPAN)):
            src = f'{template}_{i}_{j}.tome'
            if packs.find(src):
                files[f'{zone_name}_{i + da // TOME_SPAN}_{j + db // TOME_SPAN}.tome'] = packs.read(src)
    # no .vnfo: the template's describes its own full map and objects, and a zone
    # loads without one
    report['files'] = len(files)
    report['bytes'] = sum(len(v) for v in files.values())
    return files, report
