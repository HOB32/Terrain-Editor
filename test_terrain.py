"""The ground of the real map, decoded.

Nothing in a terrain chunk says which way round its axes go or how its samples
are ordered, and every plausible reading produces a surface that looks like
terrain. So the cases that matter are the ones with an outside witness: Z1 places
6,973 boulders, and a boulder sits on the ground, measured by the level designers
and stored in a different file in a different format. Against those, the right
reading correlates at +0.99 and the obvious wrong one at +0.35.
"""
import contextlib
import io
import statistics
import unittest
from collections import defaultdict
from pathlib import Path

import zone_format
import steam_paths
import terrain
from asset_studio.packs import Packs

ASSETS = Path(steam_paths.find_assets(steam_paths.H1Z1) or 'H1Z1 is not installed')
SPAN = terrain.TILE_SIZE*terrain.TILES_PER_CHUNK_SIDE


@unittest.skipUnless(ASSETS.is_dir(), 'installed game assets are not available')
class TheGroundOfZ1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with contextlib.redirect_stdout(io.StringIO()):
            cls.packs = Packs(str(ASSETS), exclude=[p.name for p in ASSETS.glob('zz_*.pack2')])
        cls.zone = zone_format.decode_zone(cls.packs.read('Z1.zone'))
        cls.boulders = defaultdict(list)
        for group in cls.zone['objects']:
            if not any(k in group['actor'].lower() for k in ('boulder', 'rock')):
                continue
            for instance in group['instances']:
                x, y, z = instance['pos'][0], instance['pos'][1], instance['pos'][2]
                cls.boulders[(int(z//SPAN)*4, int(x//SPAN)*4)].append((x, y, z))
        cls.busiest = sorted(cls.boulders.items(), key=lambda kv: -len(kv[1]))[:6]

    def chunks(self):
        for (a, b), items in self.busiest:
            name = f'Z1_{a}_{b}_0.cnk'
            if self.packs.find(name):
                yield name, terrain.load_chunk(self.packs, name), items

    def test_a_chunk_parses_to_its_own_end(self):
        for name, chunk, _ in self.chunks():
            with self.subTest(chunk=name):
                self.assertEqual(len(chunk['tiles']), 16)
                self.assertEqual(chunk['verts_per_tile'], 65)
                self.assertEqual(len(chunk['height_samples']), 16*65*65)
                self.assertEqual(chunk['trailer'], 1.0)

    def test_every_boulder_falls_inside_the_chunk_that_should_hold_it(self):
        """A wrong axis order puts most of them outside, which is how it was caught."""
        for name, chunk, items in self.chunks():
            inside = sum(1 for x, y, z in items if terrain.height_at(chunk, x, z) is not None)
            with self.subTest(chunk=name):
                self.assertEqual(inside, len(items), f'{name}: {inside} of {len(items)} boulders land in it')

    def test_the_decoded_ground_follows_the_boulders(self):
        pairs = []
        for name, chunk, items in self.chunks():
            for x, y, z in items:
                ground = terrain.height_at(chunk, x, z)
                if ground is not None:
                    pairs.append((y, ground))
        self.assertGreater(len(pairs), 100, 'too few boulders to conclude anything')
        r = statistics.correlation([p[0] for p in pairs], [p[1] for p in pairs])
        self.assertGreater(r, 0.95, f'ground and boulders correlate at only {r:+.3f}')
        near = sum(1 for y, g in pairs if abs(y-g) <= 2.0)/len(pairs)
        self.assertGreater(near, 0.4, f'only {near:.0%} of boulders sit within 2 m of the ground')

    def test_swapping_the_tile_axes_makes_it_worse(self):
        """The control: the obvious reading of the tile coordinates is the wrong one."""
        pairs = []
        for name, chunk, items in self.chunks():
            per_tile = chunk['verts_per_tile']
            for x, y, z in items:
                for i, tile in enumerate(chunk['tiles']):
                    ox, oz = tile.x*terrain.TILE_SIZE, tile.y*terrain.TILE_SIZE   # deliberately swapped
                    if ox <= x < ox+terrain.TILE_SIZE and oz <= z < oz+terrain.TILE_SIZE:
                        u, v = int(x-ox), int(z-oz)
                        raw = chunk['height_samples'][i*per_tile*per_tile + u*per_tile + v][0]
                        pairs.append((y, raw*terrain.HEIGHT_SCALE))
                        break
        if len(pairs) > 100:
            r = statistics.correlation([p[0] for p in pairs], [p[1] for p in pairs])
            self.assertLess(r, 0.8, 'the wrong axis order should not correlate as well as the right one')

    def test_heights_are_in_a_sane_range_for_a_map(self):
        for name, chunk, _ in self.chunks():
            _, heights = terrain.height_grid(chunk)
            with self.subTest(chunk=name):
                self.assertGreater(min(heights), -100.0)
                self.assertLess(max(heights), 1000.0)

    def test_the_surface_mesh_covers_the_whole_chunk(self):
        name, chunk, _ = next(self.chunks())
        positions, indices = terrain.surface_mesh(chunk, step=8)
        self.assertTrue(positions and indices)
        self.assertEqual(len(positions) % 3, 0)
        self.assertEqual(len(indices) % 3, 0)
        self.assertLess(max(indices), len(positions)//3)
        xs = positions[0::3]
        zs = positions[2::3]
        self.assertAlmostEqual(max(xs)-min(xs), SPAN, delta=1.0)
        self.assertAlmostEqual(max(zs)-min(zs), SPAN, delta=1.0)

    def test_the_name_of_the_chunk_under_a_point_can_be_worked_out(self):
        name, chunk, items = next(self.chunks())
        x, _, z = items[0]
        self.assertEqual(terrain.chunk_name('Z1', x, z), name)

    def test_a_level_of_detail_chunk_is_refused_rather_than_mis_read(self):
        with self.assertRaises(terrain.ChunkError):
            terrain.parse_chunk(b'\x00'*64, 'Z1_0_0_1.cnk', 'CNK1')


if __name__ == '__main__':
    unittest.main()
