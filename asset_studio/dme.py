"""DME v4 inspection, portable geometry export and experimental static-prop builds.

Input layouts are resolved from this installation's materials_3.xml. Opaque
material bytes are retained when building; unsupported layouts are rejected.
"""
from dataclasses import dataclass
import math
import struct
import xml.etree.ElementTree as ET

FORMATS = {'float1': 'f', 'float2': '2f', 'float3': '3f', 'float4': '4f',
           'float16_2': '2e', 'float16_4': '4e', 'short2': '2h', 'short4': '4h',
           'ubyte4n': '4B', 'd3dcolor': '4B'}


def signed(value):
    value &= 0xffffffff
    return value - 0x100000000 if value & 0x80000000 else value


def material_hash(name):
    value = 0
    for byte in name.encode('utf-8'):
        value = signed((value + (byte if byte < 128 else byte - 256)) * 1025)
        value ^= value >> 6
    value = signed(value * 9)
    value ^= value >> 11
    return value * 32769 & 0xffffffff


class Reader:
    def __init__(self, data):
        self.data, self.at = data, 0

    def take(self, size):
        if size < 0 or self.at + size > len(self.data):
            raise ValueError('Truncated model data')
        result = self.data[self.at:self.at + size]
        self.at += size
        return result

    def unpack(self, fmt):
        return struct.unpack('<' + fmt, self.take(struct.calcsize('<' + fmt)))

    def count(self, maximum=1_000_000):
        value, = self.unpack('I')
        if value > maximum:
            raise ValueError('Model count exceeds supported limits')
        return value


class Layouts:
    def __init__(self, data):
        root = ET.fromstring(data)
        self.layouts, self.materials = {}, {}
        for obj in root.iter('Object'):
            if obj.get('Class') == 'InputLayout':
                offsets, fields = {}, []
                valid = True
                for entry in obj.iter('Object'):
                    if entry.get('Class') != 'LayoutEntry':
                        continue
                    stream = int(entry.get('Stream'))
                    kind = entry.get('Type').lower()
                    if kind not in FORMATS:
                        valid = False
                        break
                    offset = offsets.get(stream, 0)
                    fields.append((stream, offset, kind, entry.get('Usage').lower(), int(entry.get('UsageIndex', '0'))))
                    offsets[stream] = offset + struct.calcsize('<' + FORMATS[kind])
                if valid:
                    self.layouts[obj.get('Name')] = (offsets, fields)
            elif obj.get('Class') == 'MaterialDefinition':
                names = list(dict.fromkeys(x.get('InputLayout') for x in obj.iter('Object')
                                           if x.get('Class') == 'DrawStyle' and x.get('InputLayout')))
                self.materials[material_hash(obj.get('Name'))] = names

    def resolve(self, definition, strides):
        # A material can use different layouts for normal/shadow draw styles.
        for name in self.materials.get(definition, []):
            if name not in self.layouts:
                continue
            sizes, fields = self.layouts[name]
            # Instancing streams (usually stream 2) are supplied at runtime.
            if all(sizes.get(i) == stride for i, stride in enumerate(strides)) and all(
                    i < len(strides) or i >= 2 for i in sizes):
                return name, [field for field in fields if field[0] < len(strides)]
        raise ValueError(f'No verified vertex layout for material {definition:08x}, strides {strides}')


def vec_sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def unit(value):
    length = math.sqrt(sum(v*v for v in value))
    return tuple(v/length for v in value) if length > 1e-20 else (0., 1., 0.)


def normals(positions, indices):
    accum = [[0., 0., 0.] for _ in positions]
    for i in range(0, len(indices), 3):
        a, b, c = indices[i:i+3]
        n = cross(vec_sub(positions[b], positions[a]), vec_sub(positions[c], positions[a]))
        for vertex in (a, b, c):
            for axis in range(3):
                accum[vertex][axis] += n[axis]
    return [unit(n) for n in accum]


@dataclass
class Mesh:
    positions: list
    uvs: list
    indices: list
    normals: list
    layout: str
    bone_count: int
    strides: list
    uvs1: list = None          # the decal texcoord set, when the layout carries one


class Model:
    def __init__(self, data, layouts):
        reader = Reader(data)
        if reader.take(4) != b'DMOD' or reader.count() != 4:
            raise ValueError('Only DMOD version 4 is supported')
        size = reader.count(16*1024*1024)
        self.dmat = reader.take(size)
        mat = Reader(self.dmat)
        if mat.take(4) != b'DMAT' or mat.count() != 1:
            raise ValueError('Only DMAT version 1 is supported')
        self.textures = [s.decode('utf-8') for s in mat.take(mat.count()).split(b'\0') if s]
        self.materials = []
        # Which parameters each material declares, by name hash. A shader group
        # can only reach a material through parameters it has: a car interior
        # built on a no-tint material cannot be tinted, whatever its mesh order.
        self.material_parameters = []
        for _ in range(mat.count(1024)):
            start = mat.at
            name, length, definition, count = mat.unpack('4I')
            if count > 4096:
                raise ValueError('Too many material parameters')
            declared = set()
            for _ in range(count):
                param, kind, datatype, length_param = mat.unpack('4I')
                mat.take(length_param)
                declared.add(param)
            if mat.at - start != length + 8:
                raise ValueError('Inconsistent material length')
            self.materials.append(definition)
            self.material_parameters.append(frozenset(declared))
        if mat.at != len(self.dmat):
            raise ValueError('Unexpected material trailer')
        self.bounds = reader.unpack('6f')
        self.meshes = []
        for mesh_index in range(reader.count(256)):
            draw_offset, draw_count, bone_count, marker = reader.unpack('4I')
            stream_count, index_size, index_count, vertex_count = reader.unpack('4I')
            if marker != 0xffffffff or not 1 <= stream_count <= 8 or vertex_count > 500_000 or index_count > 3_000_000:
                raise ValueError('Unsupported or oversized mesh')
            if index_size not in (2, 4, 0x80000004) or index_count % 3:
                raise ValueError('Unsupported triangle index format')
            index_size &= 0xff
            strides, streams = [], []
            for _ in range(stream_count):
                stride = reader.count(1024)
                strides.append(stride)
                streams.append(reader.take(stride * vertex_count))
            indices = [x[0] for x in struct.iter_unpack('<H' if index_size == 2 else '<I', reader.take(index_size * index_count))]
            if any(index >= vertex_count for index in indices):
                raise ValueError('Triangle references a missing vertex')
            # Forgelight faces are clockwise; OBJ/glTF use counter-clockwise.
            indices = [v for i in range(0, len(indices), 3) for v in reversed(indices[i:i+3])]
            if mesh_index >= len(self.materials):
                raise ValueError('Mesh material is missing')
            layout, fields = layouts.resolve(self.materials[mesh_index], strides)
            def field_values(usage, components, index=0):
                found = next((x for x in fields if x[3:] == (usage, index)), None)
                if found is None:
                    return None
                stream, offset, kind, _, _ = found
                fmt = '<' + FORMATS[kind]
                values = [struct.unpack_from(fmt, streams[stream], offset + i * strides[stream])[:components] for i in range(vertex_count)]
                if any(not math.isfinite(v) for row in values for v in row):
                    raise ValueError('Non-finite vertex attribute')
                return values
            positions = field_values('position', 3)
            if positions is None or any(len(p) != 3 for p in positions):
                raise ValueError('Missing position attribute')
            uvs = field_values('texcoord', 2) or [(0., 0.)] * vertex_count
            # The second texcoord set is the decal layout. Every weapon checked has
            # one - AR-15, M16A4, AK-47, riot shotgun all carry texcoord index 1 -
            # and the shipped cosmetics that place stars, stripes or emblems set
            # DecalMainUV false, meaning "use that set, not the main one". Reading
            # only index 0 lost those decals.
            uvs1 = field_values('texcoord', 2, 1)
            self.meshes.append(Mesh(positions, uvs, indices, normals(positions, indices), layout,
                                    bone_count, strides, uvs1))
            # Which bone each vertex follows (first of up to four). Vehicles use it
            # to tell the wheels from the body, so the wheels can turn and steer.
            try:
                blend = field_values('blendindices', 4)
            except (ValueError, struct.error):
                blend = None
            self.meshes[-1].blend = [int(b[0]) for b in blend] if blend else None
            try:
                self.meshes[-1].colors = field_values('color', 4)
            except (ValueError, struct.error):
                self.meshes[-1].colors = None
        self.draws = [reader.unpack('9I') for _ in range(reader.count(4096))]
        self.bone_map = reader.take(reader.count(4096) * 4)
        self.bone_count = reader.count(4096)
        # 76 bytes a bone: a 4x3 inverse-bind matrix, a bounding box (min, max)
        # and the bone's name hash. Kept for vehicles, whose wheel bones give
        # where the wheels are and how big.
        self.bone_data = reader.take(self.bone_count * 76)
        self.trailer = reader.take(len(data) - reader.at)
        if self.trailer not in (b'', b'\0\0\0\0'):
            raise ValueError('Unknown DME trailer')
