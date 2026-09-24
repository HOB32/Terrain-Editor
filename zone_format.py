"""The .zone file: its header, terrain grid, eco (ground type) table and every
placed object (actor, position, rotation, scale), for H1Z1's v7 zones and the
KotK depot's v5 Z2. Taken from the Dev Hub's pack2formats.py, zone part only.
"""
import re
import struct


def strings_in(data, minlen=5, limit=400):
    return [s.decode("latin-1") for s in re.findall(rb"[\x20-\x7e]{%d,}" % minlen, data)[:limit]]


def u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def f32s(d, o, n):
    return [round(x, 5) for x in struct.unpack_from("<%df" % n, d, o)]


def _cstr(d, o):
    e = d.index(b"\0", o)
    return d[o:e].decode("latin-1", "replace"), e + 1


def decode_zone(data):
    version = u32(data, 4)
    hdr = struct.unpack_from("<12I", data, 0)
    r = {"format": "ZONE", "version": version, "status": "partial", "header_u32": list(hdr[1:])}
    # Offsets table (H1Z1 v7 has an extra u32 after the version; PS2 v1 has 6 offsets immediately)
    if version >= 4:
        offs = struct.unpack_from("<7I", data, 12)
        keys = ["ecos", "floras", "invisible_walls", "objects", "lights", "unknowns", "decals"]
        po = 40
    else:
        offs = struct.unpack_from("<6I", data, 8)
        keys = ["ecos", "floras", "invisible_walls", "objects", "lights", "unknowns"]
        po = 32
    r["offsets"] = dict(zip(keys, offs))
    r["quads_per_tile"], = struct.unpack_from("<I", data, po)
    r["tile_size"], r["tile_height"] = struct.unpack_from("<2f", data, po + 4)
    r["verts_per_tile"], r["tiles_per_chunk"] = struct.unpack_from("<2I", data, po + 12)
    r["start_x"], r["start_y"], r["chunks_x"], r["chunks_y"] = struct.unpack_from("<4i", data, po + 20)
    secs = {}
    for k, off in r["offsets"].items():
        nxt = min([x for x in offs if x > off] + [len(data)])
        secs[k] = {"offset": off, "length": nxt - off, "strings": strings_in(data[off:nxt], 4, 300)}
    r["sections"] = secs
    # objects (v7 layout, derived from LoginZone.zone): count; per object: actor cstring, f32 render distance,
    # u32 unk, u32 instance count; per instance: pos float4, rot float4 (radians), scale float4, u32 id,
    # u32 unk, u8 unk, f32 unk(1.0), then counted lists: A x u32, B x {u32 n, n x {param hash, f32}},
    # C x (unknown, always 0 seen), D x {u32 hash, float4}, then 5 trailing bytes.
    try:
        o = r["offsets"]["objects"]
        n = u32(data, o)
        o += 4
        objs = []
        total_inst = 0
        for _ in range(n):
            e = data.index(b"\0", o)
            actor = data[o:e].decode("latin-1")
            o = e + 1
            # v7 carries an extra u32 before the instance count; v5 (the KotK depot's
            # Z2, 789 actors / 614 296 instances, parsed to the byte) does not.
            if version >= 7:
                rd, unk, ic = struct.unpack_from("<fII", data, o)
                o += 12
            else:
                rd, ic = struct.unpack_from("<fI", data, o)
                o += 8
            inst = []
            for _ in range(ic):
                pos = f32s(data, o, 4)
                rot = f32s(data, o + 16, 4)
                scl = f32s(data, o + 32, 4)
                iid = u32(data, o + 48)
                o += 57 if version >= 7 else 53  # 48 + u32 id (+ u32 unk in v7) + u8 unk
                one = struct.unpack_from("<f", data, o)[0]
                o += 4
                a = u32(data, o)
                o += 4
                hashes = [{"hash": f"{u32(data, o + 8 * i):08x}", "value": u32(data, o + 8 * i + 4)} for i in range(a)]
                o += 8 * a
                b = u32(data, o)
                o += 4
                overrides = []
                for _ in range(b):
                    ph, val = struct.unpack_from("<If", data, o)
                    overrides.append({"param": f"{ph:08x}", "value": round(val, 5)})
                    o += 8
                c = u32(data, o)
                o += 4
                if c:
                    raise ValueError(f"unexpected list C={c} in instance of {actor}")
                dcount = u32(data, o)
                o += 4
                extra = []
                for _ in range(dcount):
                    extra.append({"hash": f"{u32(data, o):08x}", "vec": f32s(data, o + 4, 4)})
                    o += 20
                o += 5
                d = {"pos": pos[:3], "rot": rot[:3], "scale": scl[:3], "id": iid}
                if hashes:
                    d["hashes"] = hashes
                if overrides:
                    d["material_overrides"] = overrides
                if extra:
                    d["extra"] = extra
                inst.append(d)
            total_inst += ic
            objs.append({"actor": actor, "render_distance": round(rd, 3), "instances": inst})
        r["objects"] = objs
        r["object_count"] = n
        r["instance_count"] = total_inst
        if o == r["offsets"]["lights"]:
            r["status"] = "decoded" if version in (5, 7) else "partial"
        else:
            r["objects_parse_note"] = f"object section ended at {o}, lights start at {r['offsets']['lights']}"
    except Exception as ex:  # noqa: BLE001
        r["objects_error"] = str(ex)
    # ecos: count; per eco: name, colour map, spec map, u32 detail repeat, floats, physics material, ...
    try:
        o = r["offsets"]["ecos"]
        n = u32(data, o)
        o += 4
        ecos = []
        for _ in range(n):
            idx = u32(data, o)
            o += 4
            name, o = _cstr(data, o)
            cnx, o = _cstr(data, o)
            sbny, o = _cstr(data, o)
            detail_repeat = u32(data, o)
            blend, spec_min, spec_max, smooth_min, smooth_max = struct.unpack_from("<5f", data, o + 4)
            o += 24
            phys, o = _cstr(data, o)
            ecos.append({"index": idx, "name": name, "colour_nx_map": cnx, "spec_blend_ny_map": sbny, "detail_repeat": detail_repeat,
                         "blend_strength": round(blend, 4), "spec_min": round(spec_min, 4), "spec_max": round(spec_max, 4),
                         "physics_material": phys})
            # layer block: u32 count of layers, each: f32 density, f32 min scale, f32 max scale, f32 slope peak, f32 slope extent,
            # f32 min elevation, f32 max elevation, u8 min alpha, cstring flora, u32 count of tint entries x (4 bytes rgba + u32)
            lc = u32(data, o)
            o += 4
            layers = []
            for _ in range(lc):
                dens, smin, smax, speak, sext, emin, emax = struct.unpack_from("<7f", data, o)
                o += 28
                min_alpha = data[o]
                o += 1
                flora, o = _cstr(data, o)
                tc = u32(data, o)
                o += 4
                tints = [(data[o + 8 * i:o + 8 * i + 4].hex(), u32(data, o + 8 * i + 4)) for i in range(tc)]
                o += 8 * tc
                layers.append({"flora": flora, "density": round(dens, 4), "scale": [round(smin, 3), round(smax, 3)],
                               "slope": [round(speak, 3), round(sext, 3)], "elevation": [round(emin, 2), round(emax, 2)],
                               "min_alpha": min_alpha, "tints": tints})
            ecos[-1]["flora_layers"] = layers
        r["ecos"] = ecos
        if o != r["offsets"]["floras"]:
            r["ecos_parse_note"] = f"eco section ended at {o}, floras start at {r['offsets']['floras']}"
    except Exception as ex:  # noqa: BLE001
        r["ecos_error"] = str(ex)
    return r
