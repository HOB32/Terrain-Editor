"""The standalone server: its routes, and who it lets in.

The page and the API are only for same-origin local pages, and saving needs
the token the page was handed - the same rules the Dev Hub applies, so a web
page open in another tab cannot write rooms or install packs through it.
"""
import http.client
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import steam_paths
import terrain_server

STEAM = steam_paths.find_assets(steam_paths.H1Z1)


class TheServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = Path(tempfile.mkdtemp(prefix='terrain-test-'))
        # no game at all: routes that need none must still work
        cls.tool = terrain_server.TerrainTool(STEAM, None, cls.data)
        terrain_server.Handler.tool = cls.tool
        cls.server = terrain_server.TerrainServer(('127.0.0.1', 0), terrain_server.Handler)
        cls.port = cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.data, ignore_errors=True)

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=300)
        payload = json.dumps(body).encode() if body is not None else None
        c.request(method, path, payload, {'Host': f'127.0.0.1:{self.port}', **(headers or {})})
        r = c.getresponse()
        raw = r.read()
        c.close()
        kind = r.getheader('Content-Type', '')
        return r.status, json.loads(raw) if kind.startswith('application/json') else raw, r

    def test_the_root_opens_the_tool(self):
        status, _, reply = self.call('GET', '/')
        self.assertEqual((status, reply.getheader('Location')), (302, '/terrain/'))
        status, page, _ = self.call('GET', '/terrain/')
        self.assertEqual(status, 200)
        for script in (b'/terrain/app.js', b'/terrain/area.js', b'/terrain/season.js', b'/ui-frame.css'):
            self.assertIn(script, page)
            self.assertEqual(self.call('GET', script.decode())[0], 200)

    def test_it_says_which_games_it_found(self):
        status, games, _ = self.call('GET', '/api/terrain/games')
        self.assertEqual(status, 200)
        rows = {g['key']: g for g in games['games']}
        self.assertFalse(rows['kotk']['available'])
        self.assertTrue(rows['kotk']['buildable'])

    def test_another_site_is_turned_away(self):
        status, reply, _ = self.call('GET', '/api/terrain/rooms', headers={'Origin': 'https://example.com'})
        self.assertEqual(status, 403, reply)
        status, _, _ = self.call('GET', '/terrain/', headers={'Host': 'example.com'})
        self.assertEqual(status, 403)

    def test_saving_needs_the_page_token(self):
        room = {'name': 'server test room', 'zone': 'Z1', 'spawn': [1, 2, 3, 0],
                'objects': [{'actor': 'Common_Props_Crate01.adr', 'pos': [1, 2, 3]}]}
        self.assertEqual(self.call('POST', '/api/terrain/rooms', room)[0], 403)
        status, listing, _ = self.call('GET', '/api/terrain/rooms')
        self.assertEqual(status, 200)
        status, saved, _ = self.call('POST', '/api/terrain/rooms', room, {'X-Studio-Token': listing['token']})
        self.assertEqual(status, 200, saved)
        self.assertTrue(Path(saved['saved']).is_file())
        names = [r['name'] for r in self.call('GET', '/api/terrain/rooms')[1]['rooms']]
        self.assertIn(saved['room']['name'], names)

    def test_a_bad_request_says_why(self):
        self.assertEqual(self.call('GET', '/api/terrain/nothing-here')[0], 404)
        status, reply, _ = self.call('GET', '/api/terrain/room')
        self.assertEqual(status, 400)
        self.assertIn('name', reply['error'])

    def test_a_missing_game_is_explained_not_crashed(self):
        self.call('GET', '/api/terrain/game?set=kotk')
        try:
            status, reply, _ = self.call('GET', '/api/terrain/zones')
        finally:
            self.call('GET', '/api/terrain/game?set=steam')
        self.assertEqual(status, 400)
        self.assertIn('--kotk-assets', reply['error'])

    @unittest.skipUnless(STEAM, 'H1Z1 is not installed')
    def test_it_reads_the_real_map(self):
        status, reply, _ = self.call('GET', '/api/terrain/zones')
        self.assertEqual(status, 200, reply)
        self.assertIn('Z1', [z['name'] if isinstance(z, dict) else z for z in reply['zones']])


if __name__ == '__main__':
    unittest.main()
