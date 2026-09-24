"""Terrain [beta] for the hub: zone overviews and ground meshes from the client.

Decoding a chunk is slow - the LZHAM decompressor is pure Python, about two
and a half seconds a chunk - so every decoded mesh is cached on disk and a
chunk is only ever decoded once per PC. The overview needs no chunk at all: it
is drawn from where the zone file places its 190 000 objects, which traces the
towns and roads well enough to pick an area.
"""
import base64
import gzip
import json
import struct
import threading
from pathlib import Path

import zone_format as fmts
import terrain

ZONES = ('Z1', 'Z2', 'PracticeZone', 'LoginZone', 'TreePlayground', 'Nexus', 'BattleRoyale')
# Placed actors the player never sees: loot spawn points, collision helpers and
# door/vehicle proxies. They are half of Z1's 192 000 instances.
HIDDEN = ('itemspawner', 'invisible', 'proxy', 'spawner', 'collision', 'trigger', 'volume', 'audio', 'sound', 'light_')
# What the zone's invisible actors actually are: loot spawners, vehicle spawn
# proxies and doors. Shown as markers instead of being hidden.
MARKERS = (
    ('weapons', 'Weapon spawns', '#f25f5c', ('itemspawner', 'weapon')),
    ('gear', 'Gear spawns', '#f0c04a', ('itemspawner', 'gear')),
    ('backpack', 'Backpack spawns', '#b07cff', ('itemspawner', 'backpack')),
    ('medical', 'Medical spawns', '#3fcf8e', ('itemspawner', 'firstaid')),
    ('crate', 'Military crates', '#ff8a3d', ('itemspawner', 'militarycrate')),
    ('fuel', 'Fuel cans', '#62b3ff', ('itemspawner', 'gascan')),
    ('vehicle', 'Vehicle spawn points (zone)', '#27d3ff', ('dpo_vehicle',)),
    ('door', 'Doors', '#9aa3b2', ('doorproxy',)),
)
# Points the server emulator keeps in data/2016/zoneData rather than the client.
SERVER_MARKERS = (
    ('player', 'Player spawns', '#39ff9f'),
    ('vehicleSpawn', 'Vehicle spawns (server)', '#00e0ff'),
    ('grid', 'Grid spawns', '#d6dbe3'),
)
TREE_TYPES = {10: 'pine', 7: 'tall pine', 14: 'broadleaf', 12: 'bush'}
TILE = terrain.TILE_SIZE
STEPS = {'low': 8, 'medium': 4, 'high': 2}
POINT_LIMIT = 60000


def chunk_bounds(name):
    """World (x0, z0) of a chunk from its name. Zone_<a>_<b>_<lod>: a is Z, b is X.

    terrain.chunk_origin() reads the two the other way round; chunk_name() and the
    decoded tile positions agree with this reading (Z1_0_-64_0 spans x -4096..-3840).
    """
    parts = name.rsplit('.', 1)[0].split('_')
    return int(parts[-2])*TILE, int(parts[-3])*TILE


def _short(actor):
    import re
    name = re.sub(r'(?i)\.adr$|^common_dpo_vehicle_|^itemspawner_(kotk_)?|_proxy.*$|^common_dpo_doorproxy_', '', actor)
    return name.replace('_', ' ')


class TerrainService:
    def __init__(self, packs_of, cache_dir, server_data_of=None):
        self.packs_of = packs_of          # callable -> Packs of the installed client
        self.server_data_of = server_data_of or (lambda: None)   # callable -> emulator data/2016
        self.cache = Path(cache_dir)/'terrain'
        self.lock = threading.Lock()
        self.busy = {}                   # chunk name -> Lock, so one decode per chunk
        self.zone_cache = {}
        self.marker_cache = {}
        self.tree_cache = {}

    def _server_file(self, zone, name):
        root = self.server_data_of()
        path = Path(root)/'zoneData'/f'{zone}_{name}.json' if root else None
        if not path or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None

    def _markers(self, zone):
        """Every marker in the zone by category, as [x, y, z, yaw, label] rows."""
        with self.lock:
            if zone in self.marker_cache:
                return self.marker_cache[zone]
        z = self._zone(zone)
        out = {key: [] for key, *_ in MARKERS}
        for obj in z.get('objects', []):
            name = obj['actor'].lower()
            key = next((k for k, _, _, words in MARKERS if all(w in name for w in words)), None)
            if not key:
                continue
            label = _short(obj['actor'])
            for inst in obj['instances']:
                p, r = inst['pos'], inst.get('rot') or [0, 0, 0]
                out[key].append([round(p[0], 2), round(p[1], 2), round(p[2], 2), round(r[0], 3), label])
        spawns = self._server_file(zone, 'spawnLocations') or []
        out['player'] = [[round(s['position'][0], 2), round(s['position'][1], 2), round(s['position'][2], 2),
                          0, s.get('name', 'Spawn')] for s in spawns if s.get('position')]
        vehicles = self._server_file(zone, 'vehicleLocations') or []
        out['vehicleSpawn'] = [[round(v['position'][0], 2), round(v['position'][1], 2), round(v['position'][2], 2),
                                round(v.get('orientation') or 0, 3), v.get('name', 'Vehicle')] for v in vehicles if v.get('position')]
        grid = self._server_file(zone, 'gridSpawns') or []
        out['grid'] = [[round(g[0], 2), round(g[1], 2), round(g[2], 2), 0, 'Grid spawn'] for g in grid if isinstance(g, list) and len(g) >= 3]
        with self.lock:
            self.marker_cache[zone] = out
        return out

    def layers(self, zone):
        m = self._markers(zone)
        rows = [{'key': k, 'label': label, 'colour': colour, 'count': len(m.get(k, [])), 'source': 'zone'}
                for k, label, colour, _ in MARKERS]
        rows += [{'key': k, 'label': label, 'colour': colour, 'count': len(m.get(k, [])), 'source': 'server'}
                 for k, label, colour in SERVER_MARKERS]
        trees = self._trees(zone)
        rows.append({'key': 'trees', 'label': 'Trees', 'colour': '#4f8a3a', 'count': sum(len(v) for v in trees.values()), 'source': 'server'})
        return [r for r in rows if r['count']]

    def markers(self, zone, x0, z0, x1, z1):
        m = self._markers(zone)
        return {k: [r for r in rows if x0 <= r[0] < x1 and z0 <= r[2] < z1] for k, rows in m.items()}

    def _trees(self, zone):
        with self.lock:
            if zone in self.tree_cache:
                return self.tree_cache[zone]
        cells = {}
        for t in self._server_file(zone, 'speedTrees') or []:
            p = t.get('position')
            if not p:
                continue
            key = (int(p[0]//256), int(p[2]//256))
            cells.setdefault(key, []).append([round(p[0], 2), round(p[1], 2), round(p[2], 2), t.get('id', 0),
                                              (t.get('uniqueId') or 0) % 997])
        with self.lock:
            self.tree_cache[zone] = cells
        return cells

    def trees(self, zone, x0, z0, x1, z1):
        cells = self._trees(zone)
        out = []
        for cx in range(int(x0//256), int(x1//256)+1):
            for cz in range(int(z0//256), int(z1//256)+1):
                out += [t for t in cells.get((cx, cz), []) if x0 <= t[0] < x1 and z0 <= t[2] < z1]
        return {'trees': out, 'types': TREE_TYPES}

    def places(self, zone):
        """Named places (towns, landmarks) with outlines, and the spawn points to
        mark on the overview map."""
        pois = self._server_file(zone, 'POIs') or []
        m = self._markers(zone)
        return {'places': [{'name': p.get('POIname'), 'tags': p.get('tags', []), 'position': p.get('position'),
                            'bounds': p.get('bounds') or []} for p in pois],
                'player': [[r[0], r[2], r[4]] for r in m.get('player', [])],
                'vehicleSpawn': [[r[0], r[2], r[4]] for r in m.get('vehicleSpawn', [])],
                'weapons': [[r[0], r[2]] for r in m.get('weapons', [])][::2],
                'vehicle': [[r[0], r[2], r[4]] for r in m.get('vehicle', [])]}

    def packs(self):
        return self.packs_of()

    def zones(self):
        p = self.packs()
        return [z for z in ZONES if p.find(z+'.zone')]

    def _zone(self, zone):
        if zone not in self.zones():
            raise ValueError(f'No {zone}.zone in this client')
        with self.lock:
            if zone not in self.zone_cache:
                self.zone_cache[zone] = fmts.decode_zone(self.packs().read(zone+'.zone'))
            return self.zone_cache[zone]

    def overview(self, zone):
        path = self.cache/f'{zone}.overview.json.gz'
        try:
            return json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError):
            pass
        z = self._zone(zone)
        packs = self.packs()
        # Chunk names are probed rather than listed: the archives store name hashes.
        chunks = []
        span = max(abs(z['start_x']), abs(z['start_y']), 64)
        for a in range(-span, span+1, 4):
            for b in range(-span, span+1, 4):
                name = f'{zone}_{a}_{b}_0.cnk'
                if packs.find(name):
                    x0, z0 = chunk_bounds(name)
                    chunks.append({'name': name, 'x': x0, 'z': z0, 'size': TILE*4})
        points, total = [], 0
        for obj in z.get('objects', []):
            total += len(obj['instances'])
        stride = max(1, total // POINT_LIMIT)
        n = 0
        for obj in z.get('objects', []):
            for inst in obj['instances']:
                if n % stride == 0:
                    points += [round(inst['pos'][0], 1), round(inst['pos'][2], 1)]
                n += 1
        if chunks:
            xs = [c['x'] for c in chunks]+[c['x']+c['size'] for c in chunks]
            zs = [c['z'] for c in chunks]+[c['z']+c['size'] for c in chunks]
            bounds = [min(xs), min(zs), max(xs), max(zs)]
        else:
            bounds = [z['start_x']*TILE, z['start_y']*TILE, -z['start_x']*TILE, -z['start_y']*TILE]
        result = {'zone': zone, 'chunks': chunks, 'points': points, 'objects': total,
                  'actors': len(z.get('objects', [])), 'ecos': len(z.get('ecos', [])), 'bounds': bounds}
        self.cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(json.dumps(result).encode()))
        return result

    def chunk(self, name, detail='medium'):
        step = STEPS.get(detail, 4)
        if not name.endswith('_0.cnk') or '/' in name or '\\' in name:
            raise ValueError('Only full-detail chunks (…_0.cnk) can be shown')
        path = self.cache/f'{name}.s{step}.v3.json.gz'   # v3: per-vertex ground types + 1 m ground grid
        try:
            return json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError):
            pass
        with self.lock:
            gate = self.busy.setdefault(name, threading.Lock())
        with gate:
            try:                         # decoded by another request while waiting
                return json.loads(gzip.decompress(path.read_bytes()))
            except (OSError, ValueError):
                pass
            packs = self.packs()
            if not packs.find(name):
                raise ValueError(f'{name} is not in this client')
            ch = terrain.load_chunk(packs, name)
            positions, indices = terrain.surface_mesh(ch, step)
            ecos = sample_ecos(ch, step)
            ys = positions[1::3]
            result = {'name': name, 'detail': detail, 'tiles': len(ch['tiles']),
                      'min': min(ys), 'max': max(ys), 'vertices': len(positions)//3,
                      'ecoLayers': sorted({e.id for t in ch['tiles'] for e in t.ecos}),
                      'positions': base64.b64encode(struct.pack(f'<{len(positions)}f', *positions)).decode(),
                      'indices': base64.b64encode(struct.pack(f'<{len(indices)}I', *indices)).decode(),
                      'ecos': base64.b64encode(bytes(ecos)).decode()}
            gx, gz, side, grid = eco_grid(ch)
            result['grid'] = {'x': gx, 'z': gz, 'side': side, 'data': base64.b64encode(bytes(grid)).decode()}
            self.cache.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip.compress(json.dumps(result).encode()))
            return result

    def objects(self, zone, x0, z0, x1, z1, limit=40000):
        """Visible placed objects in an area, grouped by actor for instanced drawing.

        Each instance is [x, y, z, yaw, pitch, roll, sx, sy, sz]: radians, applied
        as Ry(yaw)·Rx(pitch)·Rz(roll) - the convention under which consecutive
        road pieces join end to end (64 joins against 2 the other way round).
        """
        z = self._zone(zone)
        groups, count = {}, 0
        for obj in z.get('objects', []):
            actor = obj['actor']
            if any(word in actor.lower() for word in HIDDEN):
                continue
            for inst in obj['instances']:
                x, y, zz = inst['pos'][:3]
                if x0 <= x < x1 and z0 <= zz < z1:
                    rot = inst.get('rot') or [0, 0, 0]
                    scale = inst.get('scale') or [1, 1, 1]
                    groups.setdefault(actor, {'actor': actor, 'renderDistance': obj.get('render_distance'),
                                              'instances': []})['instances'].append(
                        [round(x, 3), round(y, 3), round(zz, 3), round(rot[0], 4), round(rot[1], 4),
                         round(rot[2], 4), round(scale[0], 3), round(scale[1], 3), round(scale[2], 3)])
                    count += 1
                    if count >= limit:
                        return {'groups': list(groups.values()), 'count': count, 'truncated': True}
        return {'groups': list(groups.values()), 'count': count, 'truncated': False}

    # Semantic hashes of the textures a world model's material takes (ADR aliases).
    SEM_COLOUR = -1295769314 & 0xffffffff     # _C: a plain colour map
    SEM_LAYER = -1310850161 & 0xffffffff      # _L: the colour of the layered structure shader
    SEM_TINT_OVERLAY = 339833724              # _TC: a grey overlay the tint colours
    SEM_TINT = 1716414136                     # the tint itself: a tiny flat swatch (light_coral.dds, grey.dds)

    def _mean(self, packs, name):
        """Average RGB (0..1) of a texture, cached."""
        from asset_studio.textures import decode_dds
        cache = self.__dict__.setdefault('_means', {})
        if name not in cache:
            try:
                w, h, px = decode_dds(packs.read(name), 16)
                cache[name] = [sum(px[k::4])/(w*h)/255 for k in range(3)]
            except Exception:
                cache[name] = None
        return cache[name]

    def _layered_colour(self, packs, textures):
        """The colour texture of a model that has no _C map.

        Most H1Z1 buildings and props use the layered structure shader: a shared
        material atlas, grunge, an overlay and a per-model _L map, which for
        these models IS the colour (the planks of a fence, the concrete of a
        barrier). Props made to be tinted instead have a black _L; their colour
        is a flat tint swatch (light_coral.dds) over a grey overlay (_TC), which
        is composed here as a texture of its own ("tint~<overlay>~<rrggbb>.dds")."""
        found = lambda n: n and n.lower() not in ('grey.dds', 'white.dds', 'black.dds') and packs.find(n)
        # other shaders (the _RM props use 1735042216 for their _L) keep the same file suffixes
        by_suffix = lambda suffix: next((n for n in textures.values() if n.lower().endswith(suffix)), None)
        colour = textures.get(self.SEM_COLOUR) or by_suffix('_c.dds')
        if found(colour):
            return colour
        layer = textures.get(self.SEM_LAYER) or by_suffix('_l.dds')
        if found(layer):
            mean = self._mean(packs, layer)
            if mean and sum(mean)/3 > 0.08:          # a real colour map, not a black lighting layer
                return layer
        overlay, tint = textures.get(self.SEM_TINT_OVERLAY) or by_suffix('_tc.dds'), textures.get(self.SEM_TINT)
        if found(overlay):
            swatch = self._mean(packs, tint) if tint and packs.find(tint) else None
            if swatch:
                return 'tint~%s~%02x%02x%02x.dds' % (overlay, *(max(0, min(255, round(c*255))) for c in swatch))
            return overlay
        return layer if found(layer) else None

    def texture(self, name, size=256):
        """A game texture as a PNG of at most `size` pixels, cached on disk.
        'tint~<overlay>~<rrggbb>.dds' is an overlay multiplied by a tint colour."""
        import re
        from asset_studio.textures import decode_dds, png
        size = max(16, min(1024, int(size)))
        if '/' in name or '\\' in name or not name.lower().endswith('.dds'):
            raise ValueError('Not a texture name')
        path = self.cache/'tex'/f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.{size}.png"
        try:
            return path.read_bytes()
        except OSError:
            pass
        packs = self.packs()
        tint = None
        source = name
        if name.startswith('tint~'):
            parts = name[:-4].split('~')
            if len(parts) != 3 or not re.fullmatch(r'[0-9a-fA-F]{6}', parts[2]):
                raise ValueError('Bad tinted texture name')
            source, tint = parts[1], [int(parts[2][i:i+2], 16)/255 for i in (0, 2, 4)]
        if not packs.find(source):
            raise ValueError(f'{source} is not in this client')
        w, h, px = decode_dds(packs.read(source), size)
        if tint:
            px = bytearray(px)
            # the overlay is mid-grey around 0.75: scale so it keeps the swatch's own brightness
            for k in range(0, len(px), 4):
                for c in range(3):
                    px[k + c] = min(255, int(px[k + c]*tint[c]*1.25))
        data = png(w, h, bytes(px))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    def actors(self, zone):
        """Visible actors this zone places, most used first - the build palette."""
        z = self._zone(zone)
        rows = [{'actor': o['actor'], 'count': len(o['instances'])} for o in z.get('objects', [])
                if not any(word in o['actor'].lower() for word in HIDDEN)]
        rows.sort(key=lambda r: -r['count'])
        return rows

    def palette(self, zone):
        """Each eco's average colour, from its own colour map - what the ground
        type looks like from a distance (asphalt, dirt tracks, grass, rock)."""
        path = self.cache/f'{zone}.palette.v2.json'
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        from asset_studio.textures import decode_dds
        packs = self.packs()
        out = {}
        for eco in self._zone(zone).get('ecos', []):
            colour = [0.35, 0.4, 0.3]
            try:
                w, h, px = decode_dds(packs.read(eco['colour_nx_map']), 32)
                n = w*h
                colour = [round(sum(px[k::4])/n/255, 4) for k in range(3)]
            except Exception:
                pass
            out[str(eco['index'])] = {'name': eco['name'], 'colour': colour, 'texture': eco['colour_nx_map'],
                                      'repeat': eco.get('detail_repeat') or 16}
        self.cache.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out), encoding='utf-8')
        return out

    def mesh(self, actor, far=False):
        """One actor's geometry for instancing: positions, normals, indices and an
        average colour. `far` takes its lowest level of detail."""
        import re
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', actor)
        path = self.cache/'mesh'/f'{safe}.{"far" if far else "near"}.v3.json.gz'
        try:
            return json.loads(gzip.decompress(path.read_bytes()))
        except (OSError, ValueError):
            pass
        import xml.etree.ElementTree as ET
        from asset_studio.dme import Model, Layouts
        from asset_studio.textures import decode_dds
        packs = self.packs()
        if not hasattr(self, '_layouts'):
            self._layouts = Layouts(packs.read('materials_3.xml'))
        root = ET.fromstring(packs.read(actor))
        base = root.find('Base')
        files = [base.get('fileName')] if base is not None else []
        lods = [lod.get('fileName') for lod in root.findall('./Lods/Lod') if lod.get('fileName')]
        chosen = next((f for f in (reversed(lods) if far else []) if packs.find(f)), None) or (files[0] if files else None)
        if not chosen:
            raise ValueError(f'{actor} has no mesh')
        model = Model(packs.read(chosen), self._layouts)
        # Each sub-mesh draws with its own colour map: the material bindings of
        # the DME, overridden by the ADR's texture aliases (as the game does).
        bindings = []
        try:
            from asset_studio.dmat import texture_bindings, ROLES
            bindings = texture_bindings(model.dmat)
            for alias in root.iter('Alias'):
                if alias.get('modelType', '0') != '0':
                    continue
                role = ROLES.get(int(alias.get('semanticHash', 0)) & 0xffffffff)
                index = int(alias.get('materialIndex', 0))
                if role and 0 <= index < len(bindings):
                    bindings[index][role] = alias.get('textureName', '').rsplit('/', 1)[-1]
        except Exception:
            bindings = []
        fallback = next((n for n in model.textures if n.lower().endswith(('_c.dds', '_d.dds')) and packs.find(n)), None)
        # every texture the ADR gives each material, by semantic
        by_material = {}
        for alias in root.iter('Alias'):
            if alias.get('modelType', '0') != '0':
                continue
            index = int(alias.get('materialIndex', 0))
            name = alias.get('textureName', '').rsplit('/', 1)[-1]
            by_material.setdefault(index, {})[int(alias.get('semanticHash', 0)) & 0xffffffff] = name
        positions, normals, uvs, indices, parts = [], [], [], [], []
        for number, m in enumerate(model.meshes):
            first = len(positions)//3
            for p in m.positions:
                positions += p[:3]
            for n in m.normals:
                normals += n[:3]
            for i in range(len(m.positions)):
                u = m.uvs[i] if i < len(m.uvs) else (0.0, 0.0)
                uvs += [u[0], u[1]]
            start_index = len(indices)
            indices += [first + i for i in m.indices]
            texture = bindings[number].get('color') if number < len(bindings) else None
            if not texture or not packs.find(texture):
                texture = self._layered_colour(packs, by_material.get(number) or by_material.get(0) or {}) or fallback
            if texture and not texture.startswith('tint~') and not packs.find(texture):
                texture = fallback
            parts.append({'first': start_index, 'count': len(indices) - start_index, 'texture': texture})
        colour = [0.6, 0.6, 0.58]
        fallback = fallback or next((p['texture'] for p in parts if p['texture'] and not p['texture'].startswith('tint~')), None)
        if fallback:
            try:
                w, h, px = decode_dds(packs.read(fallback), 16)
                colour = [round(sum(px[k::4])/(w*h)/255, 4) for k in range(3)]
            except Exception:
                pass
        result = {'actor': actor, 'lod': chosen, 'colour': colour, 'triangles': len(indices)//3, 'parts': parts,
                  'positions': base64.b64encode(struct.pack(f'<{len(positions)}f', *positions)).decode(),
                  'normals': base64.b64encode(struct.pack(f'<{len(normals)}f', *normals)).decode(),
                  'uvs': base64.b64encode(struct.pack(f'<{len(uvs)}f', *uvs)).decode(),
                  'indices': base64.b64encode(struct.pack(f'<{len(indices)}I', *indices)).decode()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(json.dumps(result).encode()))
        return result


class Rooms:
    """Custom rooms built in the Terrain tool: a zone, a spawn point and placed
    objects, stored as plain JSON under the data root so a server (the Solo
    route of the emulator) can spawn the player into them.

    File format, version 1:
      {"version": 1, "name": str, "zone": "Z1", "spawn": [x, y, z, yaw],
       "objects": [{"actor": "X.adr", "pos": [x,y,z], "rot": [yaw,pitch,roll], "scale": [sx,sy,sz]}]}
    Positions are world units and rotations radians, exactly as the zone file
    places its own objects (Ry(yaw)·Rx(pitch)·Rz(roll)).
    """
    LIMIT = 5000

    def __init__(self, data_dir):
        self.dir = Path(data_dir)/'projects'/'rooms'

    @staticmethod
    def safe(name):
        import re
        name = re.sub(r'[^A-Za-z0-9 _-]', '', str(name or '')).strip()[:60]
        if not name:
            raise ValueError('Give the room a name (letters, numbers, spaces, - and _)')
        return name

    def list(self):
        out = []
        for path in sorted(self.dir.glob('*.json')):
            try:
                room = json.loads(path.read_text(encoding='utf-8'))
                out.append({'name': room['name'], 'zone': room.get('zone'), 'objects': len(room.get('objects', [])),
                            'spawns': len(room.get('spawns', [])),
                            'spawn': room.get('spawn'), 'file': str(path)})
            except (OSError, ValueError, KeyError):
                continue
        return out

    def get(self, name):
        path = self.dir/f'{self.safe(name)}.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def save(self, body):
        name = self.safe(body.get('name'))
        objects = body.get('objects') or []
        if not isinstance(objects, list) or len(objects) > self.LIMIT:
            raise ValueError(f'A room holds up to {self.LIMIT} objects')
        def vec(v, n, default):
            v = v if isinstance(v, list) and len(v) == n else default
            return [round(float(x), 4) for x in v]
        clean = []
        for o in objects:
            actor = str(o.get('actor', ''))
            if not actor.lower().endswith('.adr') or '/' in actor or '\\' in actor:
                raise ValueError(f'Not an actor: {actor!r}')
            clean.append({'actor': actor, 'pos': vec(o.get('pos'), 3, [0, 0, 0]),
                          'rot': vec(o.get('rot'), 3, [0, 0, 0]), 'scale': vec(o.get('scale'), 3, [1, 1, 1])})
        spawn = body.get('spawn')
        # Typed spawn points: where players, loot and vehicles appear. `kind`
        # picks the spawner (player, weapons, gear, backpack, medical, crate,
        # fuel, vehicle) and `detail` narrows it (e.g. the vehicle: ATV01).
        kinds = {'player', 'weapons', 'gear', 'backpack', 'medical', 'crate', 'fuel', 'vehicle'}
        spawns = []
        for s in body.get('spawns') or []:
            if s.get('kind') not in kinds:
                raise ValueError(f"Unknown spawn type {s.get('kind')!r}")
            spawns.append({'kind': s['kind'], 'detail': str(s.get('detail') or '')[:60],
                           'pos': vec(s.get('pos'), 3, [0, 0, 0]), 'yaw': round(float(s.get('yaw') or 0), 4)})
        if len(spawns) > self.LIMIT:
            raise ValueError(f'A room holds up to {self.LIMIT} spawn points')
        room = {'version': 2, 'name': name, 'zone': str(body.get('zone') or 'Z1'),
                'spawn': vec(spawn, 4, [0, 0, 0, 0]) if spawn else None, 'objects': clean, 'spawns': spawns}
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir/f'{name}.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(room, indent=1), encoding='utf-8')
        tmp.replace(path)
        return {'saved': str(path), 'room': room}

    def delete(self, name):
        path = self.dir/f'{self.safe(name)}.json'
        if path.exists():
            path.unlink()
        return {'deleted': name}


class Areas:
    """New areas made in the Terrain tool's Create tab: a height field, a ground
    type per metre, water, roads and bridges, stored as JSON under the data root.

    File format, version 1:
      {"version": 1, "name": str, "zone": "Z1", "origin": [x0, z0], "size": metres,
       "res": metres between height samples, "n": height samples per side,
       "heights": base64 float32[n*n] (row = z), "water": base64 float32[n*n] (NaN = dry),
       "ground": base64 uint8[(size+1)^2] (the zone's ground type ids, 1 m apart),
       "roads": [{"points": [[x, z], ...], "width": m, "surface": id}],
       "bridges": [{"a": [x, y, z], "b": [x, y, z], "style": str, "width": m}],
       "objects": [{"actor", "pos", "rot", "scale"}]}   # bridge pieces, as a room places them
    World coordinates are the zone's own, so the area sits where it was made.
    """
    SIZES = (128, 256, 512, 1024)

    def __init__(self, data_dir):
        self.dir = Path(data_dir)/'projects'/'areas'

    @staticmethod
    def safe(name):
        import re
        name = re.sub(r'[^A-Za-z0-9 _-]', '', str(name or '')).strip()[:60]
        if not name:
            raise ValueError('Give the area a name (letters, numbers, spaces, - and _)')
        return name

    def list(self):
        out = []
        for path in sorted(self.dir.glob('*.json')):
            try:
                area = json.loads(path.read_text(encoding='utf-8'))
                out.append({'name': area['name'], 'zone': area.get('zone'), 'game': area.get('game', 'steam'), 'size': area.get('size'),
                            'roads': len(area.get('roads', [])), 'bridges': len(area.get('bridges', [])),
                            'file': str(path)})
            except (OSError, ValueError, KeyError):
                continue
        return out

    def get(self, name):
        path = self.dir/f'{self.safe(name)}.json'
        if not path.exists():
            raise ValueError(f'No saved area called {name!r}')
        return json.loads(path.read_text(encoding='utf-8'))

    def save(self, body):
        name = self.safe(body.get('name'))
        size = int(body.get('size') or 0)
        if size not in self.SIZES:
            raise ValueError(f'Area size must be one of {", ".join(map(str, self.SIZES))} m')
        n = int(body.get('n') or 0)
        res = float(body.get('res') or 0)
        if n < 2 or abs((n - 1)*res - size) > 1e-6:
            raise ValueError('The height grid does not match the area size')

        def blob(key, itemsize, count):
            try:
                data = base64.b64decode(body.get(key) or '', validate=True)
            except ValueError:
                raise ValueError(f'{key} is not valid base64')
            if len(data) != itemsize*count:
                raise ValueError(f'{key} has {len(data)} bytes; expected {itemsize*count}')
            return body[key]
        area = {'version': 1, 'name': name, 'zone': str(body.get('zone') or 'Z1')[:40],
                'game': 'kotk' if body.get('game') == 'kotk' else 'steam',
                'origin': [round(float(v), 3) for v in (body.get('origin') or [0, 0])[:2]],
                'size': size, 'res': res, 'n': n,
                'heights': blob('heights', 4, n*n), 'water': blob('water', 4, n*n),
                'ground': blob('ground', 1, (size + 1)**2)}
        # An area copied from the map keeps what it was copied from, so building it
        # for the game writes back only the changes (see zone_build._area_sampler).
        if body.get('baseline'):
            area['baseline'] = blob('baseline', 4, n*n)
        if body.get('groundBase'):
            area['groundBase'] = blob('groundBase', 1, (size + 1)**2)

        def point(v, count):
            if not isinstance(v, list) or len(v) != count:
                raise ValueError('Bad point in a road or bridge')
            return [round(float(x), 3) for x in v]
        area['roads'] = [{'points': [point(p, 2) for p in r.get('points', [])][:2000],
                          'width': round(float(r.get('width') or 8), 2), 'surface': int(r.get('surface') or 0)}
                         for r in (body.get('roads') or [])[:500]]
        area['bridges'] = [{'a': point(b.get('a'), 3), 'b': point(b.get('b'), 3),
                            'style': str(b.get('style') or 'simple')[:20], 'width': round(float(b.get('width') or 8), 2)}
                           for b in (body.get('bridges') or [])[:200]]
        objects = []
        for o in (body.get('objects') or [])[:Rooms.LIMIT]:
            actor = str(o.get('actor', ''))
            if not actor.lower().endswith('.adr') or '/' in actor or '\\' in actor:
                raise ValueError(f'Not an actor: {actor!r}')
            objects.append({'actor': actor, 'pos': point(o.get('pos'), 3), 'rot': point(o.get('rot'), 3),
                            'scale': point(o.get('scale'), 3)})
        area['objects'] = objects
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir/f'{name}.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(area), encoding='utf-8')
        tmp.replace(path)
        return {'saved': str(path), 'name': name, 'roads': len(area['roads']), 'bridges': len(area['bridges'])}

    def delete(self, name):
        path = self.dir/f'{self.safe(name)}.json'
        if path.exists():
            path.unlink()
        return {'deleted': name}


def eco_grid(chunk):
    """The ground type at every metre of a chunk, as a square grid.

    Height samples are 1 m apart (65 per 64 m tile, the last shared with the
    next tile), so a 4x4-tile chunk is a 257x257 grid starting at its lowest
    corner. The viewer blends the ground textures across it."""
    per_tile = chunk.get('verts_per_tile') or 65
    span = per_tile - 1
    origins = [terrain.tile_origin(t) for t in chunk['tiles']]
    x0 = min(o[0] for o in origins)
    z0 = min(o[1] for o in origins)
    extent = max(max(o[0] for o in origins) - x0, max(o[1] for o in origins) - z0) + TILE
    side = int(round(extent/TILE*span)) + 1
    grid = bytearray([255])*(side*side)
    for i, tile in enumerate(chunk['tiles']):
        ox, oz = origins[i]
        cx, cz = int(round((ox-x0)/TILE*span)), int(round((oz-z0)/TILE*span))
        base = i*per_tile*per_tile
        palette = tile.image_data
        for a in range(per_tile):
            row = cx + a
            for b in range(per_tile):
                index = chunk['height_samples'][base + a*per_tile + b][1]
                grid[(cz+b)*side + row] = palette[index] if index < len(palette) else 255
    return x0, z0, side, grid


def sample_ecos(chunk, step):
    """The ground type (eco id) under each vertex of surface_mesh(chunk, step).

    A height sample's second byte indexes the tile's own eco list (its 'image
    data'), so roads, dirt tracks and plots come out of the chunk directly: the
    asphalt ecos trace the street grid with the kerb objects sitting on its edge.
    """
    per_tile = chunk.get('verts_per_tile') or 65
    span = per_tile - 1
    out = []
    for i, tile in enumerate(chunk['tiles']):
        base = i*per_tile*per_tile
        palette = tile.image_data
        columns = list(range(0, per_tile, step))
        if columns[-1] != span:
            columns.append(span)
        for a in columns:
            for b in columns:
                index = chunk['height_samples'][base + a*per_tile + b][1]
                out.append(palette[index] if index < len(palette) else 255)
    return out
