"""Christmas Z2: turn the KotK depot's own map into a snow globe, as an overlay pack.

Everything here uses machinery the 2017 client already has; nothing the game ships is
changed. Decoded statically from the depot's H1Z1.exe and data (no game launched):

Snow cover (ground, roofs, cars - everything facing up)
  The weather simulator (H1Z1.exe 0x141813b10) turns UpdateWeatherData into two material
  parameters every frame, looked up by Jenkins one-at-a-time name hash:
      f    = clamp(0.2 * (temperature - 30), 0, 1)          temperature in Fahrenheit
      Rain = precipitation (ramped over rainRampupTimeSeconds) * f      -> "Rain"  0xbe574b3d
      Snow = 1 - f                                                      -> "Snow"  0x0eac480a
  so snow cover is full at 30F and below and grows between 35F and 30F. The screen-space
  passes that draw it (pipeline_N.xml SnowReadGBuffers / SnowWriteGBuffers, material
  "Snow", Rain.fxo ReadGBuffersSnow / WriteGBuffersSnow) ship with Enabled="false" and the
  client only ever switches on the rain pair (0x141813c25 / 0x141813c7e) - so the overlay
  enables the snow pair and the server drives the temperature (launcher/server/season.js).
  Lowering it slowly from 38F to 28F is the "snow gathering".

Snow on the ground and the grass
  The Z2 ground types (Z2.zone ecosystems) are textured by <eco>_cnx / _sbny maps; soft
  ground takes the game's own z2_snow maps under those names, rock/gravel/asphalt are
  frosted. The grass tufts' flora textures are frosted too (frost_dxt5: colour endpoints
  only, so the cut-out alpha stays). This holds whatever the weather does.

Falling snow and fireworks
  Z2Areas.xml RandomEffect properties keep spawning a one-shot composite effect near the
  camera while it is inside the area - Z2's own map-wide "blowing_grass" sphere spawns
  EFX_Ash_Area that way. One map-wide sphere spawns snow bursts (Snow_Falling_01.xml made
  one-shot) around the camera; each town gets fireworks (PFX_Seasonal_Fireworks_TripleBurst,
  5551) launched 90-260 m in front of it. Client-only: no server packet involved.

Campfires
  Z2's burnt-out campfire props get the stock looping EFX_Fire_Campfire (1207) and a warm
  flickering light, as placed area effects.

Christmas lights
  <Zone>Areas.xml places ambient effects: an AreaDefinition with a CompositeEffect property
  plays that composite at the area while the player is inside it (Z2 uses 3,900 of them
  for falling leaves, insects, fires). Each light string, tree or lamp is one sphere area
  whose composite effect holds a PARTICLE (the bulbs: glow sprites on a Line / Cylinder
  shape, ActorParticleEmitterDefinitions.xml -> its own .xml) and optionally one LIGHT
  (ActorLightEmitterDefinitions.xml, a small shadowless point light). The sphere radius is
  the draw distance, so only lights near the player cost anything.

Packs
  One overlay per feature, numbered straight after the reserved 261: 262 ground snow, 263
  sky/snow shader/effect tables, 264 the Christmas effect files. The client searches
  overlay packs (256+) in ascending signed path-hash order and the first holding a name
  wins (pack_slots.path_key), so a replacement must sit in a pack searched before the
  shipped pack that holds that name; layout() checks every file against its real owner.
  The few textures whose owners are searched before 262-264 go to 287, the only number
  up to 299 searched before every shipped pack. Chunks keep each file list within the
  8 KB the client reads (pack1tool). The loader stops at the third missing number, so
  gaps are filled with placeholder packs (pack_slots.py).
"""
import datetime
import json
import math
import re
import shutil
from pathlib import Path

import pack1tool
import pack_slots
from pack_slots import path_key, PACK_DIR_PREFIX

# One overlay pack per feature, straight after the reserved 261, not one big pack at the end.
# Each feature lists the numbers it may use, first choice first; layout() puts every file
# in the first one the client searches before the shipped pack that holds that name.
PACKS = {
    'ground': {'numbers': (262, 263), 'label': 'Snow on the ground and the grass'},
    'sky': {'numbers': (263,), 'label': 'Snow shader, sky and the effect tables'},
    'effects': {'numbers': (264,), 'label': 'Christmas lights, fireworks, campfires and falling snow'},
}
STRAGGLER = 287          # the only number up to 299 searched before every shipped pack
PACK_NUMBERS = sorted({n for p in PACKS.values() for n in p['numbers']} | {STRAGGLER})


def feature_of(name):
    if name.lower().endswith('.dds'):
        return 'ground'
    if name.startswith('Xmas_'):
        return 'effects'
    return 'sky'          # pipelines, lighting, the Actor*Definitions tables, Z2Areas.xml


def pack_key(number):
    return path_key(f'{PACK_DIR_PREFIX}{number:03d}.pack')


def chunk_headers(path):
    """Byte size of every chunk's file list in a .pack (the client reads 8 KB of each)."""
    import struct as st
    sizes, off = [], 0
    with open(path, 'rb') as f:
        while True:
            f.seek(off)
            head = f.read(0x40000)
            nxt, count = st.unpack_from('>II', head, 0)
            at = 8
            for _ in range(count):
                at += 4 + st.unpack_from('>I', head, at)[0] + 12
            sizes.append(at)
            if not nxt:
                return sizes
            off = nxt
ZONE = 'Z2'
PIPELINES = ('pipeline_1.xml', 'pipeline_2.xml', 'pipeline_3.xml')
SNOW_STAGES = ('SnowReadGBuffers', 'SnowWriteGBuffers')
LIGHTING_SOURCE = 'Lighting_Z2.txt'
LIGHTING_NAME = 'Lighting_Christmas.txt'

# Bulb colours (linear RGB). A string gets one emitter per colour per segment, so schemes
# stay at four colours or fewer.
COLOURS = {
    'red': (1.00, 0.10, 0.08), 'green': (0.10, 1.00, 0.22), 'gold': (1.00, 0.72, 0.18),
    'blue': (0.20, 0.45, 1.00), 'warm': (1.00, 0.86, 0.62), 'white': (0.85, 0.92, 1.00),
    'pink': (1.00, 0.35, 0.75), 'purple': (0.62, 0.30, 1.00),
}
SCHEMES = {
    'multi': ('red', 'green', 'gold', 'blue'),
    'candy': ('red', 'white'),
    'classic': ('red', 'green', 'gold'),
    'warm': ('warm',), 'white': ('white',), 'red': ('red',), 'green': ('green',),
    'gold': ('gold',), 'blue': ('blue',), 'frost': ('white', 'blue'),
}
SNOWFALL = {'off': 0, 'light': 5000, 'steady': 9000, 'heavy': 14000}
# Falling snow as bursts spawned around the camera by one map-wide area (the way Z2's own
# "blowing_grass" area spawns EFX_Ash_Area): particles per burst for each setting.
SNOW_BURST = {'off': 0, 'light': 350, 'steady': 700, 'heavy': 1200}
SNOW_BURST_XML = 'Xmas_SnowBurst.xml'
# The play map as Z2's own map-wide area covers it (blowing_grass), a little larger.
MAP_CENTRE, MAP_RADIUS = (-151.45, 23.41, -1202.24), 6500.0
FIREWORKS_EFFECT = 5551          # PFX_Seasonal_Fireworks_TripleBurst (Fireworks_Burst_Triple.fxd, one-shot)
CAMPFIRE_EFFECT = 1207           # EFX_Fire_Campfire (Fire_Campfire.fxd + crackle), loops
FIREWORK_RATES = {'off': None, 'rare': (12.0, 30.0), 'normal': (5.0, 14.0), 'party': (1.5, 5.0)}

# Ground types (Z2.zone ecosystems) under snow. Soft ground takes the game's own z2_snow
# textures outright; hard ground is frosted, keeping some of its own detail.
SNOW_TEXTURE = ('z2_snow_cnx.dds', 'z2_snow_sbny.dds')
SOFT_GROUND = re.compile(r'grass|field|deciduous|pineforest|pine_needles|dirt|river|puddle|rubble|debris', re.I)
HARD_GROUND = {'rock': 0.55, 'gravel': 0.7, 'asphalt': 0.6}
# Flora (grass tufts, ferns, shrubs) drawn from these textures: frosted white, alpha kept.
FLORA_FROST = {'Z_Flora_Radial_Grass01_C.dds': 0.92, 'Z2_Flora_Radial_Grass01_C.dds': 0.92,
               'Z2_Flora_Radial_Forests_C.dds': 0.85, 'Z2_Flora_Radial_WildGrass01_C.dds': 0.92,
               'Radial_Fern_Leaf_C.dds': 0.8, 'LeafGroundCover01_C.dds': 0.9, 'z2_deciduous_dirt_c.dds': 0.85,
               'Flora_Shrub_Radial_Boxwood_C.dds': 0.6, 'Z2_Flora_Radial_Shrub01_C.dds': 0.6}

DEFAULT_PROJECT = {
    'name': 'Christmas', 'zone': ZONE, 'version': 2,
    'snow': {'cover': True, 'snowfall': 'steady', 'gather_minutes': 20, 'start_temp': 38.0, 'end_temp': 27.0,
             'fog': 0.35, 'overcast': 0.75, 'wind': 1.5, 'flake_size': 1.0,
             'ground': True, 'flora': True, 'fireworks': 'normal',
             # the engine's screen-space snow pass: OFF - enabling its stages made the Z2 ground
             # vanish in game (2026-09-24), so it is opt-in until it is proven to render
             'shader': False},
    'lighting': {'mood': 'frosty', 'lights_always_on': True, 'time': 17.5},
    'lights': {'range': 180, 'bulb_size': 1.0, 'brightness': 1.0, 'real_lights': True, 'night_only': False},
    'decor': [],
}


def _f(v):
    return f'{float(v):.6f}'


def _vec(v):
    return ', '.join(f'{float(x):.2f}' for x in v)


def _clamp(v, a, b):
    return max(a, min(b, v))


def frost_dxt5(dds, amount, snow=(0.93, 0.955, 1.0), detail=0.35):
    """A DXT5 texture pushed toward snow white, every mip at once, without re-encoding.

    Only each block's two colour endpoints change; the map applied is affine, so the two
    in-between colours DXT interpolates move exactly with them, and the alpha block (the
    flora cut-out, or a normal channel in terrain cnx maps) is untouched."""
    import struct as st
    if dds[:4] != b'DDS ' or dds[84:88] != b'DXT5':
        raise ValueError('expected a DXT5 texture')
    out = bytearray(dds)
    k = 1.0 - amount
    sr, sg, sb = snow

    def remap(c):
        r, g, b = ((c >> 11) & 31) / 31.0, ((c >> 5) & 63) / 63.0, (c & 31) / 31.0
        lum = 0.3 * r + 0.59 * g + 0.11 * b
        t = (1.0 - detail) + detail * 2.0 * lum          # keeps light/dark variation
        r = r * k + sr * t * amount
        g = g * k + sg * t * amount
        b = b * k + sb * t * amount
        return (min(31, max(0, round(r * 31))) << 11) | (min(63, max(0, round(g * 63))) << 5) | min(31, max(0, round(b * 31)))
    cache = {}
    for at in range(128, len(out) - 15, 16):
        c0, c1 = st.unpack_from('<HH', out, at + 8)
        n0 = cache.get(c0)
        if n0 is None:
            n0 = cache[c0] = remap(c0)
        n1 = cache.get(c1)
        if n1 is None:
            n1 = cache[c1] = remap(c1)
        st.pack_into('<HH', out, at + 8, n0, n1)
    return bytes(out)


def area_id(name):
    """A stable positive u32 for an area or property id (the client stores them as u32)."""
    return path_key('Xmas/' + name) & 0x7FFFFFFF or 1


# Z2 props the auto-decorator hangs lights from, with the height (m above the prop's
# origin) a garland is tied at.
LAMP_ACTORS = {'City_Props_StreetLight.adr': 7.2, 'City_Props_StreetLightParking.adr': 6.0,
               'City_Props_GasLamp.adr': 3.1}
CITY_TREES = {'City_Props_Tree01_Base01.adr'}
CAMPFIRES = {'Common_Props_CampfireBurntOut.adr'}
FENCES = {'Residential_Props_Fence_Tall01_Side01.adr': 1.75}


def auto_decorate(objects, opts=None, bounds=None):
    """Lights for Z2 generated from the map's own props.

    - streets: garlands between neighbouring street lamps (each lamp tied to at most two
      neighbours 10-45 m away), hung at the lamp posts' height with a gentle sag;
    - trees: the city trees wrapped in lights;
    - towns: a tall cone of lights (with a star) where street lamps cluster;
    - fences: residential fence panels outlined (off by default: there are thousands).
    `objects` is the zone's decoded object list ([{actor, instances:[{pos}]}]).
    """
    o = {'streets': True, 'trees': True, 'towns': True, 'fences': False, 'campfires': True, 'fireworks': True,
         'scheme': 'multi', 'max': 1500, 'twinkle': True}
    o.update(opts or {})
    scheme = o['scheme'] if o['scheme'] in SCHEMES else 'multi'
    inside = (lambda p: bounds[0] <= p[0] < bounds[2] and bounds[1] <= p[2] < bounds[3]) if bounds else (lambda p: True)
    by_actor = {}
    for obj in objects:
        by_actor.setdefault(obj['actor'], []).extend(i['pos'][:3] for i in obj['instances'] if inside(i['pos']))
    out = []
    lamps = [(p, h) for actor, h in LAMP_ACTORS.items() for p in by_actor.get(actor, [])]
    if o['streets'] and lamps:
        cell = 45.0
        grid = {}
        for k, (p, _) in enumerate(lamps):
            grid.setdefault((int(p[0] // cell), int(p[2] // cell)), []).append(k)
        links, degree = set(), [0] * len(lamps)
        for k, (p, _) in enumerate(lamps):
            gx, gz = int(p[0] // cell), int(p[2] // cell)
            near = []
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in grid.get((gx + dx, gz + dz), []):
                        if j != k:
                            q = lamps[j][0]
                            d = math.hypot(q[0] - p[0], q[2] - p[2])
                            if 10 <= d <= 45 and abs(q[1] - p[1]) < 6:
                                near.append((d, j))
            for d, j in sorted(near)[:2]:
                key = (min(k, j), max(k, j))
                if key in links or degree[k] >= 2 or degree[j] >= 2:
                    continue
                links.add(key)
                degree[k] += 1
                degree[j] += 1
        for n, (k, j) in enumerate(sorted(links)):
            (p, hp), (q, hq) = lamps[k], lamps[j]
            # every garland glows; one in three also casts real light (lights cost the most)
            out.append({'kind': 'string', 'a': [round(p[0], 2), round(p[1] + hp, 2), round(p[2], 2)],
                        'b': [round(q[0], 2), round(q[1] + hq, 2), round(q[2], 2)], 'colour': scheme,
                        'twinkle': o['twinkle'], 'sag': 0.5, 'light': n % 3 == 0, 'auto': 'streets'})
    if o['trees']:
        for p in by_actor.get(next(iter(CITY_TREES)), []):
            out.append({'kind': 'tree', 'at': [round(p[0], 2), round(p[1] + 0.5, 2), round(p[2], 2)], 'height': 6.5,
                        'radius': 1.9, 'colour': scheme, 'twinkle': o['twinkle'], 'auto': 'trees'})
    if o['towns'] and lamps:
        cell = 500.0
        clusters = {}
        for p, _ in lamps:
            clusters.setdefault((int(p[0] // cell), int(p[2] // cell)), []).append(p)
        for pts in clusters.values():
            if len(pts) < 20:
                continue
            cx = sum(p[0] for p in pts) / len(pts)
            cz = sum(p[2] for p in pts) / len(pts)
            # stand it beside the lamp nearest the middle of town (lamps line the streets)
            p = min(pts, key=lambda q: math.hypot(q[0] - cx, q[2] - cz))
            out.append({'kind': 'tree', 'at': [round(p[0] + 4, 2), round(p[1], 2), round(p[2] + 4, 2)], 'height': 14,
                        'radius': 4.5, 'colour': scheme, 'twinkle': o['twinkle'], 'auto': 'towns'})
            if o['fireworks']:
                spread = max(250.0, max(math.hypot(q[0] - cx, q[2] - cz) for q in pts) + 150)
                out.append({'kind': 'fireworks', 'at': [round(cx, 2), round(p[1], 2), round(cz, 2)],
                            'radius': round(min(spread, 700.0), 1), 'auto': 'fireworks'})
    if o['campfires']:
        for p in by_actor.get(next(iter(CAMPFIRES)), []):
            out.append({'kind': 'campfire', 'at': [round(p[0], 2), round(p[1], 2), round(p[2], 2)], 'auto': 'campfires'})
    if o['fences']:
        for actor, h in FENCES.items():
            pts = by_actor.get(actor, [])
            for k in range(0, len(pts) - 1, 2):
                p, q = pts[k], pts[k + 1]
                if math.dist(p, q) < 12:
                    out.append({'kind': 'string', 'a': [round(p[0], 2), round(p[1] + h, 2), round(p[2], 2)],
                                'b': [round(q[0], 2), round(q[1] + h, 2), round(q[2], 2)], 'colour': scheme,
                                'twinkle': o['twinkle'], 'sag': 0.05, 'light': False, 'auto': 'fences'})
    # town trees first, then the city trees (few, in the middle of town), then street garlands
    rank = {'fireworks': 0, 'towns': 0, 'campfires': 1, 'trees': 1, 'streets': 2, 'fences': 3}
    out.sort(key=lambda d: rank.get(d.get('auto'), 9))
    # the cap is for lights; campfires and fireworks areas cost next to nothing
    free = [d for d in out if d['kind'] in ('campfire', 'fireworks')]
    lights = [d for d in out if d['kind'] not in ('campfire', 'fireworks')]
    return free + lights[:int(o['max'])]


class SeasonBuild:
    """Builds the Christmas overlay from a season project (see DEFAULT_PROJECT)."""

    def __init__(self, packs, assets_dir, data_root, server_dir):
        self.packs = packs                      # asset_studio.packs1.Packs1 over the depot
        self.assets = Path(assets_dir)
        self.data = Path(data_root)
        self.server_dir = Path(server_dir)
        self.out = self.data / 'projects' / 'season-builds' / 'christmas'
        self.record = self.data / 'projects' / 'season-install.json'

    # ------------------------------------------------------------ project
    @staticmethod
    def normalise(project):
        p = json.loads(json.dumps(DEFAULT_PROJECT))
        project = project or {}
        for key in ('snow', 'lighting', 'lights'):
            p[key].update({k: v for k, v in (project.get(key) or {}).items() if k in p[key]})
        p['decor'] = [d for d in (project.get('decor') or []) if isinstance(d, dict) and d.get('kind') in
                      ('string', 'tree', 'lamp', 'campfire', 'fireworks')][:5000]
        p['name'] = str(project.get('name') or p['name'])[:60]
        s = p['snow']
        s['snowfall'] = s['snowfall'] if s['snowfall'] in SNOWFALL else 'steady'
        s['gather_minutes'] = _clamp(float(s['gather_minutes']), 0, 240)
        s['start_temp'] = _clamp(float(s['start_temp']), 20, 60)
        s['end_temp'] = _clamp(float(s['end_temp']), 0, 30)       # at or below 30F = full cover
        s['fog'] = _clamp(float(s['fog']), 0, 1)
        s['overcast'] = _clamp(float(s['overcast']), 0, 1)
        s['wind'] = _clamp(float(s['wind']), 0, 10)
        s['flake_size'] = _clamp(float(s['flake_size']), 0.4, 3)
        s['ground'], s['flora'], s['shader'] = bool(s['ground']), bool(s['flora']), bool(s['shader'])
        s['fireworks'] = s['fireworks'] if s['fireworks'] in FIREWORK_RATES else 'normal'
        L = p['lights']
        L['range'] = _clamp(float(L['range']), 40, 600)
        L['bulb_size'] = _clamp(float(L['bulb_size']), 0.3, 4)
        L['brightness'] = _clamp(float(L['brightness']), 0.2, 4)
        p['lighting']['time'] = _clamp(float(p['lighting'].get('time', 17.5)), 0, 24)
        return p

    # ------------------------------------------------------------ stock files
    def stock(self, name):
        """A file as the game ships it - never a copy an overlay (ours included) put first."""
        row = self.packs.find(name)
        if row is None:
            raise ValueError(f'{name} is not in the KotK depot')
        m = re.search(r'Assets_(\d+)\.pack$', str(row.pack), re.I)
        if not m or int(m.group(1)) >= 256:
            raise ValueError(f'{name} was found in {row.pack.name}, not a shipped pack; uninstall overlays and retry')
        return self.packs.read(name)

    @staticmethod
    def _vocab_walk(root, add):
        for el in root.iter():
            add(('tag', el.tag))
            for a in el.attrib:
                add((f'attr:{el.tag}', a))
            if el.tag in ('Controller', 'Shape'):
                add((el.tag, el.get('Type')))
        for mat in root.iter('Material'):
            for p in mat.iter('Parameter'):
                add((f'param:{mat.get("Name")}', p.get('Name')))

    def particle_vocab(self, definitions_xml):
        """Every element, attribute, controller, shape and per-material parameter the depot's
        own XML particles use. The 1087 client rejects a whole emitter over one unknown
        material parameter ("No such parameter (note: parameters are case sensitive)"), e.g.
        DaylightCompensator, which only its StreakParticle material has."""
        import xml.etree.ElementTree as ET
        vocab = {}
        for name in sorted(set(re.findall(r'definitionName="([^"]+\.xml)"', definitions_xml, re.I))):
            try:
                text = self.stock(name).decode('utf-8', 'replace')
                root = ET.fromstring(re.sub(r'^\s*<\?xml[^>]*\?>', '', text))
            except (ValueError, ET.ParseError):
                continue
            self._vocab_walk(root, lambda k: vocab.setdefault(k[0], set()).add(k[1]))
        return vocab

    def check_particles(self, files, vocab):
        import xml.etree.ElementTree as ET
        unknown = {}
        for name, data in files.items():
            if not (name.startswith('Xmas_') and name.endswith('.xml')):
                continue
            root = ET.fromstring(re.sub(r'^\s*<\?xml[^>]*\?>', '', data.decode('utf-8')))

            def check(k, name=name):
                if k[1] not in vocab.get(k[0], ()):
                    unknown.setdefault(k, name)
            self._vocab_walk(root, check)
        if unknown:
            raise ValueError('Generated particles use names the KotK client does not know: ' +
                             ', '.join(f'{k[0]} {k[1]} (in {n})' for k, n in list(unknown.items())[:8]))

    def next_id(self, xml, floor):
        ids = [int(v) for v in re.findall(r'<EffectDefinition\b[^>]*?\sid="(\d+)"', xml)]
        return max([floor - 1] + ids) + 1

    # ------------------------------------------------------------ pieces
    def pipelines(self):
        out = {}
        for name in PIPELINES:
            text = self.stock(name).decode('utf-8')
            count = 0
            for stage in SNOW_STAGES:
                pattern = re.compile(r'(<Object Class="PipelineStage" Name="' + stage + r'"[^>]*?)Enabled="false"')
                text, n = pattern.subn(r'\1Enabled="true"', text)
                count += n
            if count != len(SNOW_STAGES):
                raise ValueError(f'{name}: expected the two disabled snow stages, found {count}')
            out[name] = text.encode('utf-8')
        return out

    def lighting(self, project):
        text = self.stock(LIGHTING_SOURCE).decode('latin-1')
        opts = project['lighting']
        if opts.get('lights_always_on'):
            text = re.sub(r'LightsDisabled=[\d.]+', 'LightsDisabled=0.000000', text)
        if opts.get('mood') == 'frosty':
            # A colder, softer daylight: blue-white sun, paler sky, a little less glare.
            def section(name, fn):
                nonlocal text
                m = re.search(r'(\[' + name + r'\]\r?\n)(.*?)(?=\r?\n\[|\Z)', text, re.S)
                if m:
                    text = text[:m.start(2)] + fn(m.group(2)) + text[m.end(2):]

            def day(body):
                body = re.sub(r'SunColor=[^\r\n]*', 'SunColor=236.000000 243.000000 255.000000', body)
                body = re.sub(r'SkyColor=[^\r\n]*', 'SkyColor=58.000000 74.000000 102.000000', body)
                body = re.sub(r'SunBrightness=[^\r\n]*', 'SunBrightness=13.500000', body)
                return body

            def night(body):
                body = re.sub(r'MoonColor=[^\r\n]*', 'MoonColor=120.000000 150.000000 215.000000', body)
                body = re.sub(r'MoonBrightness=[^\r\n]*', 'MoonBrightness=4.800000', body)
                return body
            section('Day', day)
            section('Night', night)
            section('Twilight', night)
        return text.encode('latin-1')

    def snow_burst_particle(self, project):
        """One puff of falling snow, from the stock Snow_Falling_01.xml text.

        The map-wide area spawns one every second or so around the camera, the way Z2's
        own blowing_grass area spawns EFX_Ash_Area (Ash_area.xml: Continuous="false",
        EndTime 1, a few seconds of life). So the stock looping snowfall is made one-shot:
        Continuous false, all its flakes released over one second, each falling for 6-9 s.
        Only numbers and the two Continuous flags change."""
        s = project['snow']
        text = self.stock('Snow_Falling_01.xml').decode('utf-8')
        size, wind = s['flake_size'], s['wind']
        swaps = [
            (r'(<ParticleEntity [^>]*?Continuous=")true(")', r'\g<1>false\2'),
            (r'(<ParticleEntity [^>]*?EndTime=")[\d.]+(")', r'\g<1>1.000000\2'),
            (r'(<SpriteEmitter Name="Snow" Count=")\d+(" Continuous=")true(")', rf'\g<1>{SNOW_BURST[s["snowfall"]]}\2false\3'),
            (r'(<Shape Type="Cylinder" Radius=")[\d.]+(")', r'\g<1>26.000000\2'),
            (r'(<LifetimeRange Min=")[\d.]+(" Max=")[\d.]+(")', r'\g<1>6.000000\g<2>9.000000\3'),
            (r'(<SpeedRange Min=")[-\d.]+(" Max=")[-\d.]+(")', r'\g<1>-1.300000\g<2>-1.900000\3'),
            (r'(<Controller Type="VelocityFromShape" Bias=")[^"]*(")', rf'\g<1>{wind * 0.25:.2f}, 0.00, {wind * 0.1:.2f}\2'),
            (r'(<Controller Type="SizeRange" Min=")[\d.]+(" Max=")[\d.]+(")', rf'\g<1>{0.012 * size:.6f}\g<2>{0.03 * size:.6f}\3'),
            (r'(Position=")[^"]*(" Direction)', r'\g<1>0.000000, 12.000000, 0.000000\2'),
            (r'(VisibleDistance=")[\d.]+(")', r'\g<1>120.000000\2'),
        ]
        for pattern, repl in swaps:
            text, n = re.subn(pattern, repl, text, count=1)
            if n != 1:
                raise ValueError(f'Snow_Falling_01.xml changed shape; could not apply {pattern}')
        return text.encode('utf-8')

    def zone_ecos(self):
        if getattr(self, '_ecos', None) is None:
            import zone_format
            self._ecos = zone_format.decode_zone(self.stock(f'{ZONE}.zone')).get('ecos', [])
        return self._ecos

    def ground_textures(self):
        """Every Z2 ground type under snow: soft ground (grass, fields, forest floor, dirt,
        river beds) wears the game's own z2_snow maps; rock, gravel and asphalt are frosted
        so cliffs and roads still read through."""
        snow_c, snow_s = (self.stock(n) for n in SNOW_TEXTURE)
        files, notes = {}, []
        for eco in self.zone_ecos():
            cnx, sbny = eco.get('colour_nx_map') or '', eco.get('spec_blend_ny_map') or ''
            name = eco.get('name') or ''
            if not cnx or cnx.lower() in ('default.dds', SNOW_TEXTURE[0]) or 'cleanroom' in cnx.lower() or cnx in files:
                continue
            hard = next((a for k, a in HARD_GROUND.items() if k in name.lower()), None)
            if hard is not None:
                try:
                    files[cnx] = frost_dxt5(self.stock(cnx), hard)
                    notes.append(f'{name}: frosted {round(hard * 100)}%')
                except ValueError as error:
                    notes.append(f'{name}: left as is ({error})')
            elif SOFT_GROUND.search(name):
                files[cnx] = snow_c
                if sbny and sbny.lower() != SNOW_TEXTURE[1]:
                    files[sbny] = snow_s
                notes.append(f'{name}: snow')
        return files, notes

    def flora_textures(self):
        out = {}
        for name, amount in FLORA_FROST.items():
            try:
                out[name] = frost_dxt5(self.stock(name), amount, detail=0.25)
            except ValueError:
                continue
        return out

    @staticmethod
    def random_area(name, at, radius, effect, dmin, dmax, fmin, fmax, front, ground):
        """An area that keeps spawning a one-shot composite effect somewhere dmin-dmax m
        from the camera every fmin-fmax s while the camera is inside it (Z2Areas.xml
        RandomEffect, as the stock blowing_grass / BoxOfDestiny areas do). The stock flags
        are always non-zero bytes (32, 64, 96...); 0 is used for "no"."""
        return (f'<AreaDefinition id="{area_id(name)}" name="{name}" shape="sphere" x1="{_f(at[0])}" y1="{_f(at[1])}" '
                f'z1="{_f(at[2])}" radius="{_f(radius)}">\n'
                f'    <Property type="RandomEffect" id="{area_id(name + "/random")}" CompositeEffectDefId="{effect}" '
                f'MinDistance="{_f(dmin)}" MaxDistance="{_f(dmax)}" MinFrequency="{_f(fmin)}" MaxFrequency="{_f(fmax)}" '
                f'OnlyPlaceInFrontOfCamera="{96 if front else 0}" PlaceOnGround="{96 if ground else 0}" AreaRelative="0" />\n'
                '</AreaDefinition>')

    @staticmethod
    def placed_area(name, at, radius, effects):
        """An area playing composite effects at its centre while the camera is within
        `radius` (Z2Areas.xml CompositeEffect properties, as Z2's fires and insects)."""
        props = ''.join(
            f'    <Property type="CompositeEffect" id="{area_id(f"{name}/{tag}")}" CompositeEffectDefId="{cid}" '
            'effectLocOffsetX="0.000000" effectLocOffsetY="0.000000" effectLocOffsetZ="0.000000" '
            'effectRotationH="0.000000" effectRotationP="0.000000" effectRotationR="0.000000" effectScale="1.000000" />\n'
            for cid, tag in effects)
        return (f'<AreaDefinition id="{area_id(name)}" name="{name}" shape="sphere" x1="{_f(at[0])}" y1="{_f(at[1])}" '
                f'z1="{_f(at[2])}" radius="{_f(radius)}">\n{props}</AreaDefinition>')
    @staticmethod
    def bulb_emitter(name, colour, count, shape, opts, twinkle, size=1.0):
        r, g, b = COLOURS[colour]
        bright = 10.0 * opts['brightness']
        # sprite size in the particle system's units (metres; the stock snowflakes are 0.03-0.08)
        s = 0.12 * opts['bulb_size'] * size
        if twinkle:
            life, curve = ('1.800000', '4.500000'), ('1.00, 1.00, 1.00, 0.15', '1.00, 1.00, 1.00, 1.00', '1.00, 1.00, 1.00, 0.15')
        else:
            life, curve = ('3600.000000', '3600.000000'), ('1.00, 1.00, 1.00, 1.00', '1.00, 1.00, 1.00, 1.00', '1.00, 1.00, 1.00, 1.00')
        return f'''    <SpriteEmitter Name="{name}" Count="{count}" Continuous="true" TimeDistribution="1.000000" FullRes="false" OutdoorOnly="false" ApexControlled="false" ApexOption="0" FaceCamera="true">
        <LifetimeRange Min="{life[0]}" Max="{life[1]}" />
        {shape}
        <Controller Type="PositionFromShape" Offset="0.000000" />
        <Material Name="SpriteParticle">
            <Parameter Name="ColorTexture" File="glow_spikedot_bright.dds" />
            <Parameter Name="BumpTexture" File="glow_spikedot_bright_cbt.dds" />
            <Parameter Name="FrameCount" Value="1" />
            <Parameter Name="Softness" Value="0.200000" />
            <Parameter Name="DepthBias" Value="0.150000" />
            <Parameter Name="HideFlatness" Value="1.000000" />
            <Parameter Name="Scattering" Value="0.000000" />
            <Parameter Name="Brightness" Value="{_f(bright)}" />
            <Parameter Name="LitMode" Value="0" />
            <Parameter Name="BlackPoint" Value="0.000000" />
            <Parameter Name="ScalePower" Value="1" />
            <Parameter Name="Scaler" Value="1.000000" />
            <Parameter Name="ScaleStart" Value="1.000000" />
            <Parameter Name="ScaleEnd" Value="1.000000" />
        </Material>
        <Controller Type="ConstantSize" Size="{_f(s)}" />
        <Controller Type="ConstantColor" Color="{r:.2f}, {g:.2f}, {b:.2f}, 1.00" />
        <Controller Type="ColorCurve" PreHSV="false">
            <Curve Start="{curve[0]}" Mid="{curve[1]}" End="{curve[2]}" Tightness="1.000000" />
            <Domain Min="0.000000" Max="1.000000" />
        </Controller>
        <Controller Type="ConstantFrame" Frame="0" />
    </SpriteEmitter>
'''

    @staticmethod
    def instance(name, visible):
        return (f'    <Instance Name="{name}_Instance" Emitter="{name}" ParticleCoordinates="Emitter" TrailCoordinates="Emitter" '
                'DetailLevel="Low" Spin="0.000000, 0.000000, 1.000000, 0.000000" TimeDilation="0.000000" '
                'Position="0.000000, 0.000000, 0.000000" Direction="0.000000, 1.000000, 0.000000" '
                'Orientation="0.000000, 0.000000, 0.000000, 0.000000" Scale="1.000000, 1.000000, 1.000000" '
                f'VisibleDistance="{_f(visible)}" UseOptionalDistance="false" OptionalDistance="{_f(visible)}" Active="true" />\n')

    def decor_piece(self, d, opts):
        """One decoration -> (centre, particle xml, light spec or None, bulbs)."""
        colours = SCHEMES.get(d.get('colour'), SCHEMES['multi'])
        twinkle = bool(d.get('twinkle', True))
        visible = opts['range']
        emitters, bulbs = [], 0
        if d['kind'] == 'string':
            a, b = [list(map(float, d['a'])), list(map(float, d['b']))]
            sag = float(d.get('sag', 0.35))
            centre = [(a[i] + b[i]) / 2 for i in range(3)]
            length = math.dist(a, b)
            spacing = _clamp(float(d.get('spacing', 0.5)), 0.2, 3)
            n = max(2, min(400, int(length / spacing) + 1))
            # A hanging string: split into segments that follow a gentle catenary-like sag.
            # (each segment costs one emitter per colour, so a few are plenty)
            segs = 1 if sag <= 0.01 or length < 8 else 2
            pts = []
            for k in range(segs + 1):
                t = k / segs
                pts.append([a[i] + (b[i] - a[i]) * t - (sag * length * 0.08 * 4 * t * (1 - t) if i == 1 else 0) - centre[i]
                            for i in range(3)])
            per_seg = max(1, n // segs)
            for ci, colour in enumerate(colours):
                for k in range(segs):
                    p0, p1 = pts[k], pts[k + 1]
                    # colours interleave: emitter ci starts ci/len(colours) of a spacing along
                    off = ci / len(colours)
                    steps = max(1, per_seg // len(colours))
                    d0 = [p0[i] + (p1[i] - p0[i]) * off / max(1, steps) for i in range(3)]
                    shape = (f'<Shape Type="Line" Start="{_vec(d0)}" End="{_vec(p1)}" RegularSpacing="true" '
                             f'SpacingSteps="{steps}" />')
                    name = f'B{ci}_{k}'
                    emitters.append((name, self.bulb_emitter(name, colour, steps, shape, opts, twinkle), [0.0, 0.0, 0.0]))
                    bulbs += steps
            light_at, light_reach = [0.0, 0.0, 0.0], max(4.0, length / 2)
        elif d['kind'] == 'tree':
            at = list(map(float, d['at']))
            h = _clamp(float(d.get('height', 7)), 2, 30)
            r = _clamp(float(d.get('radius', 2.4)), 0.5, 10)
            tiers = max(3, min(5, int(h / 1.6)))
            centre = [at[0], at[1] + h / 2, at[2]]
            for t in range(tiers):
                frac = (t + 0.5) / tiers
                radius = r * (1 - frac) + 0.25
                y = -h / 2 + h * frac * 0.92
                count = max(4, int(2 * math.pi * radius / 0.45))
                for ci, colour in enumerate(colours):
                    c = max(2, count // len(colours))
                    shape = (f'<Shape Type="Cylinder" Radius="{_f(radius)}" InnerRadius="{_f(radius * 0.92)}" '
                             f'Height="{_f(h / tiers * 0.5)}" RegularSpacing="false" SpacingSteps="{c}" LaunchAngle="0.000000" />')
                    name = f'T{t}_{ci}'
                    # each tier sits at its own height through its emitter instance's position
                    emitters.append((name, self.bulb_emitter(name, colour, c, shape, opts, twinkle), [0.0, y, 0.0]))
                    bulbs += c
            shape = '<Shape Type="Sphere" Radius="0.050000" InnerRadius="0.000000" />'
            emitters.append(('S', self.bulb_emitter('S', 'gold', 1, shape, opts, False, size=4.0), [0.0, h / 2 + 0.3, 0.0]))
            bulbs += 1
            light_at, light_reach = [0.0, 0.0, 0.0], max(6.0, h)
        else:  # lamp: one warm glow and its light, e.g. on a porch or a street lamp
            at = list(map(float, d['at']))
            centre = at
            shape = '<Shape Type="Sphere" Radius="0.050000" InnerRadius="0.000000" />'
            emitters.append(('L', self.bulb_emitter('L', colours[0], 1, shape, opts, False, size=5.0), [0.0, 0.0, 0.0]))
            bulbs += 1
            light_at, light_reach = [0.0, 0.0, 0.0], 8.0
        body = ''
        insts = ''
        for name, xml, pos in emitters:
            body += xml
            insts += self.instance(name, visible).replace('Position="0.000000, 0.000000, 0.000000"',
                                                          f'Position="{_f(pos[0])}, {_f(pos[1])}, {_f(pos[2])}"')
        particle = ('<?xml version="1.0" encoding="utf-8" ?>\n'
                    '<ParticleEntity Version="1" MinimumRadius="0.200000" Continuous="true" EndTime="3600.000000" '
                    f'BoundingRadius="{_f(max(2.0, light_reach))}" OrientToNormal="false">\n' + body + insts +
                    '</ParticleEntity>\n').encode('utf-8')
        light = None
        if opts['real_lights'] and d.get('light', True):
            col = [sum(COLOURS[c][i] for c in colours) / len(colours) for i in range(3)]
            light = {'offset': light_at, 'color': col, 'falloff': light_reach}
        return centre, particle, light, bulbs

    # ------------------------------------------------------------ build
    def build(self, project):
        project = self.normalise(project)
        files, report = {}, {'decor': 0, 'bulbs': 0, 'real_lights': 0, 'warnings': []}
        if project['snow']['shader']:            # experimental: see DEFAULT_PROJECT
            files.update(self.pipelines())
        files[LIGHTING_NAME] = self.lighting(project)

        particles_xml = self.stock('ActorParticleEmitterDefinitions.xml').decode('utf-8')
        composites_xml = self.stock('ActorCompositeEffectDefinitions.xml').decode('utf-8')
        lights_xml = self.stock('ActorLightEmitterDefinitions.xml').decode('utf-8')
        areas_xml = self.stock(f'{ZONE}Areas.xml').decode('utf-8')
        pid, cid, lid = self.next_id(particles_xml, 4100), self.next_id(composites_xml, 6000), self.next_id(lights_xml, 2300)
        new_particles, new_composites, new_lights, new_areas = [], [], [], []

        s = project['snow']
        # snow on the ground types and the grass tufts (textures, so it holds whatever the
        # weather does; the snow pass then adds roofs, cars, fences and the far ground)
        if s['ground']:
            ground, notes = self.ground_textures()
            files.update(ground)
            report['ground_textures'] = notes
        if s['flora']:
            flora = self.flora_textures()
            files.update(flora)
            report['flora_textures'] = sorted(flora)

        # falling snow: bursts around the camera from one map-wide area (client-only)
        snow = None
        if SNOW_BURST[s['snowfall']]:
            files[SNOW_BURST_XML] = self.snow_burst_particle(project)
            new_particles.append(f'\t<EffectDefinition name="Xmas_SnowBurst" id="{pid}" loadType="1" localSpaceDerived="false" '
                                 f'worldOrientation="true" definitionName="{SNOW_BURST_XML}"/>')
            new_composites.append(self.composite('EFX_Xmas_SnowBurst', cid, [('PARTICLE', pid)], None))
            new_areas.append(self.random_area('Xmas_Snowfall', MAP_CENTRE, MAP_RADIUS, cid, 0.0, 14.0, 0.7, 1.3,
                                              front=False, ground=True))
            snow = cid
            pid += 1
            cid += 1
        rate = FIREWORK_RATES.get(s['fireworks'])

        # warm flickering light for lit campfires (the stock campfire effect has none)
        campfire_light = None
        if any(d.get('kind') == 'campfire' for d in project['decor']):
            new_lights.append(self.light('Xmas_Campfire_Light', lid, {'offset': [0, 0.8, 0], 'color': COLOURS['warm'],
                                                                        'falloff': 9.0}, project['lights'], flicker=True))
            new_composites.append(self.composite('EFX_Xmas_Campfire_Light', cid, [('LIGHT', lid)], None))
            campfire_light, lid, cid = cid, lid + 1, cid + 1

        night = project['lights']['night_only']
        for i, d in enumerate(project['decor']):
            name = f'Xmas_{d["kind"]}_{i + 1:04d}'
            if d['kind'] == 'fireworks':
                if rate:
                    new_areas.append(self.random_area(name, d['at'], float(d.get('radius', 450)), FIREWORKS_EFFECT,
                                                      90.0, 260.0, rate[0], rate[1], front=True, ground=True))
                    report['fireworks'] = report.get('fireworks', 0) + 1
                continue
            if d['kind'] == 'campfire':
                props = [(CAMPFIRE_EFFECT, 'fx')] + ([(campfire_light, 'light')] if campfire_light else [])
                new_areas.append(self.placed_area(name, d['at'], 120.0, props))
                report['campfires'] = report.get('campfires', 0) + 1
                continue
            try:
                centre, particle, light, bulbs = self.decor_piece(d, project['lights'])
            except (KeyError, TypeError, ValueError) as error:
                report['warnings'].append(f'decoration {i + 1} skipped: {error}')
                continue
            xml_name = f'Xmas_Decor_{i + 1:04d}.xml'
            files[xml_name] = particle
            new_particles.append(f'\t<EffectDefinition name="Xmas_Decor_{i + 1:04d}" id="{pid}" loadType="1" '
                                 f'localSpaceDerived="false" worldOrientation="true" definitionName="{xml_name}"/>')
            parts = [('PARTICLE', pid)]
            pid += 1
            if light:
                new_lights.append(self.light(f'Xmas_Light_{i + 1:04d}', lid, light, project['lights']))
                parts.append(('LIGHT', lid))
                lid += 1
                report['real_lights'] += 1
            new_composites.append(self.composite(f'EFX_Xmas_Decor_{i + 1:04d}', cid, parts, (16, 8) if night else None))
            new_areas.append(self.placed_area(name, centre, project['lights']['range'], [(cid, 'fx')]))
            cid += 1
            report['decor'] += 1
            report['bulbs'] += bulbs

        self.check_particles(files, self.particle_vocab(particles_xml))
        files['ActorParticleEmitterDefinitions.xml'] = self.append_defs(particles_xml, new_particles)
        files['ActorCompositeEffectDefinitions.xml'] = self.append_defs(composites_xml, new_composites)
        files['ActorLightEmitterDefinitions.xml'] = self.append_defs(lights_xml, new_lights)
        nl = '\r\n' if '\r\n' in areas_xml else '\n'
        files[f'{ZONE}Areas.xml'] = (areas_xml.rstrip() + nl + nl.join(a.replace('\n', nl) for a in new_areas) + nl).encode('utf-8')

        server = {
            'enabled': True, 'zone': ZONE, 'name': project['name'],
            # falling snow is client-side now (the map-wide area); nothing to attach to players
            'lighting': LIGHTING_NAME, 'snowfallEffect': None,
            'snowCover': bool(project['snow']['cover'] and project['snow']['shader']),
            'weather': {k: project['snow'][k] for k in ('gather_minutes', 'start_temp', 'end_temp', 'fog', 'overcast', 'wind')},
            'time': project['lighting']['time'],
            'built_at': datetime.datetime.now().isoformat(timespec='seconds'),
        }
        report.update({'files': len(files), 'snowfall_effect': snow,
                       'ids': {'particles_to': pid - 1, 'composites_to': cid - 1, 'lights_to': lid - 1}})
        return files, server, report, project

    @staticmethod
    def composite(name, cid, parts, hours):
        times = f' startTimeOfDay="{hours[0]}" stopTimeOfDay="{hours[1]}"' if hours else ''
        effects = ''.join(f'\t\t<Effect triggerName="" effectType="{kind}" effectId="{eid}" contextScope="ALL">\n'
                          '\t\t\t<Trigger time="0.000000" eventSlot="0"/>\n\t\t</Effect>\n' for kind, eid in parts)
        return f'\t<EffectDefinition name="{name}" id="{cid}"{times} effectCount="{len(parts)}">\n{effects}\t</EffectDefinition>'

    @staticmethod
    def light(name, lid, spec, opts, flicker=False):
        r, g, b = spec['color']
        o = spec['offset']
        intensity = 0.35 * opts['brightness']
        # flicker: the stock Fire_Barrel_loop light's animation (type 8, rate 6, decay 2)
        anim = 'animationType="8" animationRate="6.000000" animationDecay="2.000000"' if flicker else \
            'animationType="0" animationRate="1.000000" animationDecay="1.000000"'
        return (f'\t<EffectDefinition name="{name}" id="{lid}" offsetX="{_f(o[0])}" offsetY="{_f(o[1])}" offsetZ="{_f(o[2])}" '
                f'color="{_f(r)} {_f(g)} {_f(b)} 1.000000" color1="1.000000 1.000000 1.000000 1.000000" gelName="" '
                f'intensity="{_f(intensity)}" intensity1="1.000000" arealIntensity="{_f(intensity)}" arealIntensity1="1.000000" '
                f'falloff="{_f(spec["falloff"])}" falloff1="10.000000" nearClip="-1.000000" nearClip1="-1.000000" '
                'inner="45.000000" inner1="45.000000" outer="90.000000" outer1="90.000000" power="1.000000" power1="1.000000" '
                'duration="0.000000" easeInTime="0.500000" easeOutTime="0.500000" lightType="0" easeColor="1" easeFalloff="0" '
                f'worldOrientation="0" castShadows="0" {anim}/>')

    @staticmethod
    def append_defs(xml, rows):
        if not rows:
            return xml.encode('utf-8')
        end = xml.rfind('</Definitions>')
        if end < 0:
            raise ValueError('definition file has no </Definitions>')
        nl = '\r\n' if '\r\n' in xml else '\n'
        return (xml[:end].rstrip() + nl + nl.join(r.replace('\n', nl) for r in rows) + nl + xml[end:]).encode('utf-8')

    # ------------------------------------------------------------ packs
    def owner(self, name):
        """The shipped pack (0-255) holding `name`, or None for a new file."""
        row = self.packs.find(name)
        m = re.search(r'Assets_(\d+)\.pack$', str(row.pack), re.I) if row is not None else None
        return int(m.group(1)) if m and int(m.group(1)) < 256 else None

    def layout(self, files):
        """{pack number: {name: bytes}}, one pack per feature (PACKS), every file in the
        first of its feature's numbers that the client searches before the shipped pack
        holding that name (pack order = ascending signed path key, pack_slots.path_key).
        A replacement nothing earlier can beat goes to STRAGGLER (287), the only number up
        to 299 that sorts before every shipped pack."""
        out = {}
        for name, data in files.items():
            feature = feature_of(name)
            own = self.owner(name)
            for number in PACKS[feature]['numbers'] + (STRAGGLER,):
                if own is None or pack_key(number) < pack_key(own):
                    out.setdefault(number, {})[name] = data
                    break
            else:
                raise ValueError(f'no overlay number sorts before Assets_{own:03d}.pack for {name}')
        return dict(sorted(out.items()))

    # ------------------------------------------------------------ write / install
    def write(self, project, install=False):
        files, server, report, project = self.build(project)
        self.out.mkdir(parents=True, exist_ok=True)
        for old in self.out.glob('Assets_*.pack'):
            old.unlink()
        packs, total = [], 0
        for number, part in self.layout(files).items():
            built = self.out / f'Assets_{number:03d}.pack'
            size = pack1tool.write_archive(built, sorted(part.items()))
            arch = pack1tool.read_archive(str(built))
            index = {e['name']: e for e in arch['entries']}
            with open(built, 'rb') as handle:
                for name, data in part.items():
                    if pack1tool.read_asset(handle, index[name]) != data:
                        raise ValueError(f'{name} did not read back from {built.name} intact')
            headers = chunk_headers(built)
            if max(headers) > pack1tool.CHUNK_HEADER_LIMIT:
                raise ValueError(f'{built.name}: a chunk header is {max(headers)} bytes; the client reads 8 KB')
            beats = all(pack_key(number) < pack_key(o) for o in {self.owner(n) for n in part} if o is not None)
            packs.append({'pack': built.name, 'number': number, 'feature': PACKS[feature_of(next(iter(part)))]['label']
                          if number != STRAGGLER else 'Snow on the ground (textures only 287 can replace)',
                          'files': len(part), 'bytes': size, 'chunks': len(headers), 'searched_first': beats,
                          'built': str(built)})
            total += size
        (self.out / 'season.json').write_text(json.dumps(server, indent=1), encoding='utf-8')
        result = {'packs': packs, 'bytes': total, 'report': report, 'built': str(self.out), 'installed': None,
                  'order': {'searched_first': all(p['searched_first'] for p in packs)}}
        if install:
            result['installed'] = self.install([Path(p['built']) for p in packs], server)
        return result

    def load_record(self):
        try:
            rec = json.loads(self.record.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return {}
        if rec.get('pack') and not rec.get('packs'):        # one-pack installs before the split
            rec['packs'] = [rec['pack']]
        return rec

    def status(self):
        rec = self.load_record()
        packs = rec.get('packs') or []
        return {'installed': bool(packs) and all(Path(p).is_file() for p in packs), 'record': rec,
                'packs': [Path(p).name for p in packs],
                'server_file': str(self.server_dir / 'data' / 'season.json'),
                'server_enabled': (self.server_dir / 'data' / 'season.json').is_file()}

    def install(self, built, server):
        rec = self.load_record()
        ours = {str(Path(p)).lower() for p in rec.get('packs', [])}
        dests = [self.assets / b.name for b in built]
        for dest in dests:       # check them all before writing any
            if dest.exists() and str(dest).lower() not in ours and not pack_slots.is_placeholder(dest):
                raise ValueError(f'{dest} exists and was not written by the dev hub\'s Christmas build; not overwriting it')
        # an earlier layout's packs that this one does not use
        for old in rec.get('packs', []):
            if Path(old).name not in {d.name for d in dests} and Path(old).is_file():
                Path(old).unlink()
        for b, dest in zip(built, dests):
            shutil.copyfile(b, dest)
        slots = pack_slots.repair(self.assets)
        server_file = (self.server_dir / 'data' / 'season.json').resolve()
        server_file.parent.mkdir(parents=True, exist_ok=True)
        server_file.write_text(json.dumps(server, indent=1), encoding='utf-8')
        rec = {'packs': [str(d) for d in dests], 'server': str(server_file), 'placeholders': slots['written'],
               'installed_at': datetime.datetime.now().isoformat(timespec='seconds')}
        self.record.parent.mkdir(parents=True, exist_ok=True)
        self.record.write_text(json.dumps(rec, indent=1), encoding='utf-8')
        return {**rec, 'restart': 'Restart the Z1BR launcher\'s servers and the game to see it.'}

    def uninstall(self):
        rec = self.load_record()
        removed = []
        for p in list(rec.get('packs', [])) + [rec.get('server')]:
            if p and Path(p).is_file():
                Path(p).unlink()
                removed.append(str(p))
        removed += pack_slots.release_unneeded(self.assets)
        self.record.unlink(missing_ok=True)
        return {'removed': removed}