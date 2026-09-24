"""DDS base-colour preview: BC1/2/3 and masked RGB, encoded as PNG with stdlib."""
import struct
import zlib


def png(width, height, rgba):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    scanlines = b''.join(b'\0' + rgba[y*width*4:(y+1)*width*4] for y in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(scanlines, 6)) + chunk(b'IEND', b'')


def decode_dds(data, maximum=1024):
    if len(data) < 128 or data[:4] != b'DDS ' or struct.unpack_from('<I', data, 4)[0] != 124:
        raise ValueError('Invalid DDS header')
    height, width, pitch = struct.unpack_from('<3I', data, 12)
    mip_count, = struct.unpack_from('<I', data, 28)
    if not (0 < width <= 16384 and 0 < height <= 16384):
        raise ValueError('Unsupported DDS dimensions')
    fourcc = data[84:88]
    bits, *masks = struct.unpack_from('<5I', data, 88)
    offset = 128
    block = 8 if fourcc == b'DXT1' else 16
    if fourcc not in (b'DXT1', b'DXT3', b'DXT5', b'\0'*4):
        raise ValueError(f'DDS format {fourcc!r} has no preview decoder; original DDS can still be exported')
    compressed = fourcc != b'\0'*4
    if not compressed and bits not in (24, 32):
        raise ValueError('Only 24/32-bit RGB DDS previews are supported')
    for _ in range(max(0, min(mip_count, 16)-1)):
        if max(width, height) <= maximum or not compressed:
            break
        offset += max(1, (width+3)//4) * max(1, (height+3)//4) * block
        width, height = max(1, width//2), max(1, height//2)
    if width * height > 4_194_304:
        raise ValueError('DDS lacks a small enough mip for preview; original DDS remains available')
    out = bytearray(width * height * 4)
    if not compressed:
        row_size = max(width * (bits//8), pitch)
        if row_size > len(data) or offset + row_size * height > len(data):
            raise ValueError('Truncated RGB DDS')
        for y in range(height):
            for x in range(width):
                at = offset + y * row_size + x * (bits//8)
                value = int.from_bytes(data[at:at+bits//8], 'little')
                channels = []
                for i, mask in enumerate(masks):
                    if mask:
                        shift = (mask & -mask).bit_length()-1
                        channels.append(((value & mask) >> shift)*255//(mask >> shift))
                    else:
                        channels.append(255 if i == 3 else 0)
                out[(y*width+x)*4:(y*width+x+1)*4] = bytes(channels)
        return width, height, bytes(out)
    expected = max(1, (width+3)//4) * max(1, (height+3)//4) * block
    if offset + expected > len(data):
        raise ValueError('Truncated block-compressed DDS')
    def rgb565(value):
        return ((value >> 11)*255//31, ((value >> 5) & 63)*255//63, (value & 31)*255//31)
    for by in range((height+3)//4):
        for bx in range((width+3)//4):
            alpha = [255]*16
            if fourcc == b'DXT3':
                packed = int.from_bytes(data[offset:offset+8], 'little')
                alpha = [((packed >> (i*4)) & 15)*17 for i in range(16)]
                offset += 8
            elif fourcc == b'DXT5':
                a, b = data[offset:offset+2]
                palette = [a, b]
                if a > b:
                    palette += [((7-i)*a+i*b)//7 for i in range(1, 7)]
                else:
                    palette += [((5-i)*a+i*b)//5 for i in range(1, 5)] + [0, 255]
                packed = int.from_bytes(data[offset+2:offset+8], 'little')
                alpha = [palette[(packed >> (i*3)) & 7] for i in range(16)]
                offset += 8
            c0, c1, selectors = struct.unpack_from('<HHI', data, offset)
            offset += 8
            a, b = rgb565(c0), rgb565(c1)
            colors = [(*a, 255), (*b, 255)]
            if c0 > c1 or fourcc != b'DXT1':
                colors += [(*( (2*a[i]+b[i])//3 for i in range(3)), 255),
                           (*( (a[i]+2*b[i])//3 for i in range(3)), 255)]
            else:
                colors += [(*( (a[i]+b[i])//2 for i in range(3)), 255), (0, 0, 0, 0)]
            for i in range(16):
                x, y = bx*4+i%4, by*4+i//4
                if x < width and y < height:
                    color = colors[(selectors >> (i*2)) & 3]
                    at = (y*width+x)*4
                    out[at:at+4] = bytes((*color[:3], color[3]*alpha[i]//255))
    return width, height, bytes(out)
