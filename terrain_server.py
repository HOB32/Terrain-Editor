"""H1Z1 Terrain Tool - the Dev Hub's Terrain tool as a program of its own.

Explore a zone's real ground and 3D objects, build rooms, make new areas and
build them into a zone for the KotK depot client, and dress Z2 for Christmas.
Same pages and the same /api/terrain/* routes as the hub, without the rest of
the hub: nothing but the terrain.

    python terrain_server.py [--port 8766] [--steam-assets DIR] [--kotk-assets DIR]
                             [--server-data DIR] [--data-root DIR] [--launcher-server DIR]

Every folder is found on its own when not given (see README.md). Projects are
kept under the data root, which is the Dev Hub's own one when the hub is on this
PC, so rooms, areas and zone builds made in either program show up in both.
"""
import argparse
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import steam_paths

HERE = Path(__file__).resolve().parent
WEB = HERE/'web'
APP = 'h1z1-terrain-tool'
PAGES = {'/terrain/': ('index.html', 'text/html; charset=utf-8'),
         '/terrain/app.js': ('app.js', 'text/javascript; charset=utf-8'),
         '/terrain/area.js': ('area.js', 'text/javascript; charset=utf-8'),
         '/terrain/season.js': ('season.js', 'text/javascript; charset=utf-8'),
         '/ui-frame.css': ('ui-frame.css', 'text/css; charset=utf-8')}
# overlay packs other tools add to the Steam install (zz_*.pack2) are not the game's world
STEAM_OVERLAYS = 'zz_*.pack2'


# ------------------------------------------------------------ where things are
def find_data_root(explicit=None):
    """Projects, caches and builds. The Dev Hub's data root when it has one on
    this PC (so both programs share areas, rooms and the chunk cache), else a
    folder of this tool's own in the user profile."""
    chosen = explicit or os.environ.get('H1Z1_TERRAIN_DATA') or os.environ.get('H1Z1_DEVHUB_DATA')
    if not chosen:
        pointers = [Path(os.environ.get('LOCALAPPDATA') or Path.home())/'h1z1-devhub'/'data-root.txt',
                    HERE.parent/'h1z1_devhub'/'data-root.txt']
        for pointer in pointers:
            try:
                text = pointer.read_text(encoding='utf-8').strip()
            except OSError:
                continue
            if text and Path(os.path.expandvars(text)).is_dir():
                chosen = text
                break
    root = Path(os.path.expandvars(os.path.expanduser(chosen))) if chosen else Path.home()/'h1z1-terrain-data'
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def find_server_data(explicit=None):
    """The emulator's data/2016 (its zoneData holds spawns, POIs and trees), or
    None - the tool works without it, those map layers just stay empty."""
    candidates = [Path(explicit)] if explicit else []
    if os.environ.get('H1Z1_SERVER_DATA'):
        candidates.append(Path(os.environ['H1Z1_SERVER_DATA']))
    parents = [HERE.parent, Path.home()/'Projects', Path.home()/'Desktop'/'Projects',
               Path.home()/'OneDrive'/'Desktop'/'Projects', Path.home()/'OneDrive'/'Projects', Path.home()]
    parents += [Path(f'{d}:/') for d in 'CDEFG'] + [Path(f'{d}:/Projects') for d in 'CDEFG']
    for parent in parents:
        for repo in ('h1z1-server', 'local-test-h1z1', 'server'):
            candidates += [parent/repo/'data'/'2016', parent/repo/'node_modules'/'h1z1-server'/'data'/'2016']
    for folder in candidates:
        try:
            if (folder/'zoneData').is_dir():
                return folder.resolve()
        except OSError:
            continue
    return None


def find_launcher_server(explicit=None):
    """The Z1BR launcher's Node server, whose data/season.json switches the
    Christmas weather on. The Dev Hub keeps it in launcher/server."""
    for folder in [Path(explicit)] if explicit else [HERE.parent/'h1z1_devhub'/'launcher'/'server']:
        if folder.is_dir():
            return folder.resolve()
    return None


# ------------------------------------------------------------ the tool
class TerrainTool:
    """Everything the Terrain page talks to: one TerrainService per game, saved
    areas and rooms, zone builds for the KotK client and the Christmas season."""

    GAMES = {'steam': 'H1Z1 (Steam install)', 'kotk': 'KotK depot (the playable local client)'}

    def __init__(self, steam_assets, kotk_assets, data, server_data=None, launcher_server=None):
        self.assets = {'steam': Path(steam_assets) if steam_assets else None,
                       'kotk': Path(kotk_assets) if kotk_assets else None}
        self.data = Path(data)
        self.server_data = server_data
        self.launcher_server = Path(launcher_server) if launcher_server else self.data/'launcher-server'
        self.game = 'steam' if self._available('steam') or not self._available('kotk') else 'kotk'
        # lets the page save; it is handed out only to same-origin pages
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self._packs, self._services = {}, {}
        self._areas = self._rooms = None

    def _available(self, game):
        return bool(self.assets[game]) and self.assets[game].is_dir()

    def packs(self, game):
        with self.lock:
            if game not in self._packs:
                if not self._available(game):
                    flag = '--steam-assets' if game == 'steam' else '--kotk-assets'
                    raise ValueError(f'{self.GAMES[game]} was not found on this PC. Start the tool with '
                                     f'{flag} pointing at its Resources/Assets folder.')
                if game == 'kotk':
                    from asset_studio.packs1 import Packs1
                    self._packs[game] = Packs1(self.assets[game])
                else:
                    from asset_studio.packs import Packs
                    self._packs[game] = Packs(self.assets[game],
                                              exclude=[p.name for p in self.assets[game].glob(STEAM_OVERLAYS)])
            return self._packs[game]

    def terrain(self, game=None):
        from terrain_api import TerrainService
        game = game or self.game
        with self.lock:
            if game not in self._services:
                # same cache folders as the Dev Hub, so a chunk decoded in one is ready in the other
                cache = self.data/'cache'/'kotk' if game == 'kotk' else self.data/'cache'
                server_data = None if game == 'kotk' else self.server_data
                self._services[game] = TerrainService(lambda: self.packs(game), cache, lambda: server_data)
            return self._services[game]

    def games(self):
        rows = [{'key': key, 'label': label, 'available': self._available(key), 'buildable': key == 'kotk'}
                for key, label in self.GAMES.items()]
        return {'games': rows, 'current': self.game}

    def areas(self):
        if self._areas is None:
            from terrain_api import Areas
            self._areas = Areas(self.data)
        return self._areas

    def rooms(self):
        if self._rooms is None:
            from terrain_api import Rooms
            self._rooms = Rooms(self.data)
        return self._rooms

    # ---------------------------------------------------------- zone builds for the KotK client
    def _builds_path(self):
        return self.data/'projects'/'zone-builds.json'

    def zone_builds(self):
        try:
            rows = json.loads(self._builds_path().read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            rows = {}
        for row in rows.values():
            row['installed_now'] = bool(row.get('installed')) and Path(row['installed']).is_file()
        return rows

    def _save_builds(self, rows):
        path = self._builds_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, indent=1), encoding='utf-8')

    def build_zone(self, body):
        """Build a saved area into a new zone for the KotK client, and optionally
        install it as its own overlay pack. Nothing the game ships is overwritten:
        the pack takes the first free Assets_NNN name at 256 or above, and every
        file this writes is recorded so Uninstall can remove exactly that."""
        import pack1tool
        import zone_build
        area = self.areas().get(body.get('name'))
        if area.get('game') != 'kotk' or area.get('zone') != 'Z2':
            raise ValueError('Only areas made on the KotK depot\'s Z2 can be built for the game. Switch the Terrain '
                             'tool to "KotK depot", make the area there, and save it.')
        zone = str(body.get('zone') or '').strip()
        packs = self.packs('kotk')
        rows = self.zone_builds()
        previous = rows.get(zone)
        if not (previous and previous.get('installed_now')) and packs.find(zone + '.zone'):
            raise ValueError(f'{zone} already exists in the client; pick another zone name')
        files, report = zone_build.build_dev_zone(packs, area, zone)
        out_dir = self.data/'projects'/'zone-builds'/zone
        out_dir.mkdir(parents=True, exist_ok=True)
        assets = self.assets['kotk']
        if previous and previous.get('pack'):
            pack_name = previous['pack']
        else:
            taken = {path.name.lower() for path in assets.glob('*.pack*')}
            taken |= {row.get('pack', '').lower() for row in rows.values()}
            # numbers other overlays need for their pack order (261 is reserved, Christmas 262-264 + 287)
            taken |= {f'assets_{n}.pack' for n in (261, 262, 263, 264, 287)}
            pack_name = next(f'Assets_{i:03d}.pack' for i in range(256, 1000) if f'assets_{i:03d}.pack' not in taken)
        built = out_dir/pack_name
        size = pack1tool.write_archive(built, sorted(files.items()))
        # read it back: every file must come out of the pack exactly as written
        index = {e['name']: e for e in pack1tool.read_archive(built)['entries']}
        with open(built, 'rb') as handle:
            for name, data in files.items():
                if pack1tool.read_asset(handle, index[name]) != data:
                    raise ValueError(f'{name} did not read back from the pack intact')
        row = {'zone': zone, 'area': area['name'], 'pack': pack_name, 'built': str(built), 'bytes': size,
               'files': sorted(files), 'report': report, 'installed': None,
               'lighting': 'Lighting_Z2.txt', 'built_at': time.strftime('%Y-%m-%d %H:%M:%S')}
        if body.get('install'):
            dest = assets/pack_name
            if dest.exists() and not (previous and previous.get('installed') == str(dest)):
                raise ValueError(f'{dest} exists and was not written by the terrain tool; not overwriting it')
            shutil.copyfile(built, dest)
            row['installed'] = str(dest)
        rows[zone] = row
        self._save_builds(rows)
        row['installed_now'] = bool(row['installed'])
        row['launch'] = {'env': {'Z1BR_ZONE': zone, 'Z1BR_LIGHTING': 'Lighting_Z2.txt', 'Z1BR_MENU_VIEWS': '1'},
                         # the zone is moved to a centred grid: the area's middle, in the new zone's coordinates
                         'camera': [area['origin'][0] + area['size']/2 + report['offset'][0], None,
                                    area['origin'][1] + area['size']/2 + report['offset'][1]]}
        return row

    def uninstall_zone(self, body):
        rows = self.zone_builds()
        row = rows.get(str(body.get('zone') or ''))
        if not row:
            raise ValueError('No zone build by that name')
        removed = None
        if row.get('installed') and Path(row['installed']).is_file():
            Path(row['installed']).unlink()
            removed = row['installed']
        row['installed'] = None
        self._save_builds(rows)
        return {'zone': row['zone'], 'removed': removed}

    # ---------------------------------------------------------- Christmas season for Z2
    def season(self):
        from season_build import SeasonBuild
        return SeasonBuild(self.packs('kotk'), self.assets['kotk'], self.data, self.launcher_server)

    def _season_path(self):
        return self.data/'projects'/'seasons'/'christmas.json'

    def season_project(self):
        from season_build import SeasonBuild
        try:
            raw = json.loads(self._season_path().read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raw = {}
        return SeasonBuild.normalise(raw)

    def season_save(self, body):
        from season_build import SeasonBuild
        project = SeasonBuild.normalise(body)
        path = self._season_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(project, indent=1), encoding='utf-8')
        tmp.replace(path)
        return {'saved': str(path), 'decor': len(project['decor']), 'project': project}

    def season_info(self):
        import season_build as sb
        try:
            status = self.season().status()
        except ValueError as error:
            status = {'installed': False, 'error': str(error)}
        return {'project': self.season_project(), 'status': status, 'schemes': list(sb.SCHEMES),
                'colours': sb.COLOURS, 'snowfall': list(sb.SNOWFALL),
                'packs': [f'Assets_{n:03d}.pack' for n in sb.PACK_NUMBERS], 'token': self.token}

    def season_decorate(self, body):
        import season_build as sb
        objects = self.terrain('kotk')._zone('Z2').get('objects', [])
        bounds = body.get('bounds')
        bounds = [float(v) for v in bounds] if isinstance(bounds, list) and len(bounds) == 4 else None
        return {'decor': sb.auto_decorate(objects, body.get('options') or {}, bounds)}

    def season_build(self, body):
        saved = self.season_save(body.get('project') or self.season_project())
        result = self.season().write(saved['project'], install=bool(body.get('install')))
        result['status'] = self.season().status()
        return result

    # ---------------------------------------------------------- routes
    def get(self, endpoint, q):
        if endpoint == 'games':
            return self.games()
        if endpoint == 'game':
            self.game = q.get('set') if q.get('set') in self.GAMES else 'steam'
            return self.games()
        if endpoint == 'builds':
            return {'builds': self.zone_builds()}
        if endpoint == 'season':
            return self.season_info()
        if endpoint == 'rooms':
            return {'rooms': self.rooms().list(), 'token': self.token}
        if endpoint == 'room':
            return self.rooms().get(q['name'])
        if endpoint == 'areas':
            return {'areas': self.areas().list(), 'token': self.token}
        if endpoint == 'area':
            return self.areas().get(q['name'])
        t = self.terrain()
        zone = q.get('zone', 'Z1')
        box = lambda: [float(q[k]) for k in ('x0', 'z0', 'x1', 'z1')]
        routes = {'zones': lambda: {'zones': t.zones()},
                  'overview': lambda: t.overview(zone),
                  'chunk': lambda: t.chunk(q['name'], q.get('detail', 'medium')),
                  'objects': lambda: t.objects(zone, *box()),
                  'palette': lambda: t.palette(zone),
                  'layers': lambda: {'layers': t.layers(zone)},
                  'markers': lambda: t.markers(zone, *box()),
                  'trees': lambda: t.trees(zone, *box()),
                  'places': lambda: t.places(zone),
                  'texture': lambda: t.texture(q['name'], int(q.get('size', 256))),
                  'actors': lambda: {'actors': t.actors(zone)},
                  'mesh': lambda: t.mesh(q['actor'], q.get('far') == '1')}
        if endpoint not in routes:
            raise LookupError(endpoint)
        return routes[endpoint]()

    POSTS = {'areas/build': 32, 'builds/uninstall': 1, 'season': 16, 'season/build': 16, 'season/uninstall': 1,
             'season/decorate': 16, 'areas': 32, 'areas/delete': 1, 'rooms': 4, 'rooms/delete': 1}   # MiB

    def post(self, endpoint, body):
        return {'areas/build': lambda: self.build_zone(body),
                'builds/uninstall': lambda: self.uninstall_zone(body),
                'season': lambda: self.season_save(body),
                'season/build': lambda: self.season_build(body),
                'season/uninstall': lambda: self.season().uninstall(),
                'season/decorate': lambda: self.season_decorate(body),
                'areas': lambda: self.areas().save(body),
                'areas/delete': lambda: self.areas().delete(body.get('name')),
                'rooms': lambda: self.rooms().save(body),
                'rooms/delete': lambda: self.rooms().delete(body.get('name'))}[endpoint]()


class Forbidden(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    tool = None            # set by serve()
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype='application/json'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _local(self, mutation=False):
        """Same-origin local pages only; saving also needs the page's token."""
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in hosts or self.headers.get('Origin') not in (None, *('http://'+h for h in hosts)):
            raise Forbidden('Local same-origin access only')
        if mutation and not secrets.compare_digest(self.headers.get('X-Studio-Token', ''), self.tool.token):
            raise Forbidden('Reload the Terrain page: its save token is out of date')

    def _answer(self, work):
        try:
            result = work()
        except Forbidden as error:
            self._send(403, {'error': str(error)})
        except KeyError as error:        # a query parameter the route needs
            self._send(400, {'error': f'Missing {error}'})
        except LookupError as error:     # no such route
            self._send(404, {'error': f'Not found: {error}'})
        except ValueError as error:
            self._send(400, {'error': str(error)})
        except Exception as error:   # a chunk the beta decoder cannot read yet
            self._send(500, {'error': f'{type(error).__name__}: {error}'})
        else:
            if isinstance(result, bytes):
                self._send(200, result, 'image/png')
            else:
                self._send(200, result)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ('/', '/terrain', '/index.html'):
            self.send_response(302)
            self.send_header('Location', '/terrain/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if u.path == '/api/health':
            return self._send(200, {'app': APP, 'pid': os.getpid()})
        if u.path in PAGES:
            return self._page(*PAGES[u.path])
        if u.path.startswith('/api/terrain/'):
            endpoint = u.path.removeprefix('/api/terrain/')
            return self._answer(lambda: (self._local(), self.tool.get(endpoint, q))[1])
        self._send(404, {'error': 'Not found'})

    def _page(self, name, ctype):
        try:
            self._local()
        except Forbidden as error:
            return self._send(403, {'error': str(error)})
        self._send(200, (WEB/name).read_bytes(), ctype)

    def do_POST(self):
        u = urlparse(self.path)
        endpoint = u.path.removeprefix('/api/terrain/') if u.path.startswith('/api/terrain/') else None
        # A body left unread would be parsed as the next request on this
        # keep-alive connection, so any refusal before reading it closes it.
        self.close_connection = True
        if endpoint not in TerrainTool.POSTS:
            return self._send(404, {'error': 'Not found'})

        def work():
            self._local(mutation=True)
            length = int(self.headers.get('Content-Length', 0) or 0)
            if not 0 < length <= TerrainTool.POSTS[endpoint]*1024*1024:
                raise ValueError(f'Expected a JSON body up to {TerrainTool.POSTS[endpoint]} MiB')
            raw = self.rfile.read(length)
            self.close_connection = False
            body = json.loads(raw.decode('utf-8'))
            if not isinstance(body, dict):
                raise ValueError('Expected a JSON object')
            return self.tool.post(endpoint, body)
        self._answer(work)


class TerrainServer(ThreadingHTTPServer):
    """Holds its port alone: on Windows SO_REUSEADDR would let a second copy bind
    the same port and split the page's requests between two processes."""
    allow_reuse_address = os.name != 'nt'
    daemon_threads = True

    def server_bind(self):
        if os.name == 'nt' and hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def already_running(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=2) as reply:
            return json.load(reply).get('app') == APP
    except (OSError, ValueError):
        return False


def serve(tool, port, open_browser=True):
    Handler.tool = tool
    url = f'http://127.0.0.1:{port}/terrain/'
    try:
        server = TerrainServer(('127.0.0.1', port), Handler)
    except OSError:
        if already_running(port):
            print(f'The Terrain Tool is already running: {url}')
            if open_browser:
                webbrowser.open(url)
            return 0
        print(f'Port {port} is in use by another program. Start with --port <another number>.', file=sys.stderr)
        return 1
    print(f'H1Z1 Terrain Tool: {url}  (Ctrl+C to stop)')
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=8766, help='default 8766 (the Dev Hub uses 8765)')
    ap.add_argument('--steam-assets', help="H1Z1 (Steam) Resources/Assets folder of .pack2 archives")
    ap.add_argument('--kotk-assets', help='KotK depot Resources/Assets folder of .pack archives (the playable client)')
    ap.add_argument('--server-data', help="the emulator's data/2016 folder, for spawns, POIs and trees")
    ap.add_argument('--data-root', help='folder for areas, rooms, caches and builds')
    ap.add_argument('--launcher-server', help="the Z1BR launcher's server folder, for the Christmas weather switch")
    ap.add_argument('--no-browser', action='store_true')
    args = ap.parse_args(argv)
    tool = TerrainTool(args.steam_assets or steam_paths.find_assets(steam_paths.H1Z1),
                       args.kotk_assets or steam_paths.find_assets(steam_paths.KOTK_DEPOT),
                       find_data_root(args.data_root), find_server_data(args.server_data),
                       find_launcher_server(args.launcher_server))
    for key, label in tool.GAMES.items():
        print(f'  {label}: {tool.assets[key] if tool._available(key) else "not found"}')
    print(f'  Emulator data: {tool.server_data or "not found (spawn, POI and tree layers stay empty)"}')
    print(f'  Projects and cache: {tool.data}')
    return serve(tool, args.port, not args.no_browser)


if __name__ == '__main__':
    sys.exit(main())
