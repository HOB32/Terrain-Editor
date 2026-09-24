# H1Z1 Terrain Tool

The Terrain tool from the H1Z1 Dev Hub, as a program of its own. It reads the
game's own files and shows a zone's real ground and every building and prop as
its 3D model, and lets you build on top of it.

- **Explore:** fly over Z1 / Z2 with WASD. The ground uses its real heights and
  ground types, props are textured, and map layers show loot, vehicle and door
  spawners, player spawns, POIs and trees.
- **Rooms:** place objects and typed spawn points, then save them as JSON for a
  server to spawn (`<data root>/projects/rooms/<name>.json`).
- **Create area:** sculpt a new height field with ground types, water, roads
  and bridges. An area made on the KotK depot's Z2 can be **built into a new
  zone** for the playable client, as an overlay pack of its own (Assets_256 and
  up). Nothing the game ships is overwritten, and Uninstall removes exactly what
  was written.
- **Christmas:** snow, falling snow and lights for the KotK client's own Z2, as
  overlay packs plus the launcher server's `season.json`.

## Start it

Double-click **`Start-Terrain.bat`**, or run:

```bash
python terrain_server.py
```

It opens http://127.0.0.1:8766/terrain/. The Dev Hub uses port 8765, so both can
run at once. Python 3.10 or later is the only requirement; there are no packages
to install.

## What it finds by itself

| What | Found at | Override |
|---|---|---|
| H1Z1 (Steam), `.pack2` | Steam's libraries, `common/H1Z1/Resources/Assets` | `--steam-assets DIR` |
| KotK depot, `.pack` (the playable local client) | `content/app_433850/depot_433851/Resources/Assets` | `--kotk-assets DIR` |
| Emulator zone data (spawns, POIs, trees) | `h1z1-server` / `local-test-h1z1` `data/2016` beside this folder or in your Projects folders | `--server-data DIR` or `H1Z1_SERVER_DATA` |
| Projects and chunk cache | The Dev Hub's data root when the hub is on this PC, else `~/h1z1-terrain-data` | `--data-root DIR` or `H1Z1_TERRAIN_DATA` |
| Z1BR launcher server (Christmas weather switch) | `../h1z1_devhub/launcher/server` | `--launcher-server DIR` |

When it shares the Dev Hub's data root, rooms, areas, zone builds and decoded
chunks made in either program show up in both.

Without the emulator data the spawn, POI and tree layers stay empty. Without
the KotK depot you can still explore the Steam install and save rooms, but you
can't build zones or the Christmas season.

## Files

| | |
|---|---|
| `terrain_server.py` | the HTTP server and `/api/terrain/*` routes (local, same-origin only; saving needs the page's token) |
| `terrain_api.py` | zones, chunks, objects, meshes, textures, map layers; saved rooms and areas |
| `terrain.py` | the CNK0 terrain chunk decoder |
| `zone_build.py` | writes an area as a new zone (CNK0 serializer, stored LZHAM, zone writer) |
| `season_build.py`, `pack_slots.py` | the Christmas overlay and the placeholder packs that keep overlay numbers loading |
| `zone_format.py` | the `.zone` file: terrain grid, ground types and every placed object |
| `pack1tool.py`, `pack2read.py`, `asset_studio/` | reading the game: `.pack` / `.pack2` archives, DME models, DMAT materials, DDS textures (and writing `.pack` for zone builds) |
| `lzham/` | the LZHAM codec the terrain chunks use (see `lzham/FORMAT.md`) |
| `web/` | the page |

## Tests

```bash
python -m unittest test_terrain test_terrain_server
```

`test_terrain` checks the decoded ground against where the level designers put
Z1's 6,973 boulders, so it needs H1Z1 installed and is skipped without it.
