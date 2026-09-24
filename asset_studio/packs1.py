"""Read-only access to first-generation ``.pack`` archives, shaped like ``Packs``.

Asset Studio is built on :class:`asset_studio.packs.Packs`: give it something
with the same handful of methods over a different container and the catalogue,
the model reader, the texture decoder and the source exporter all work unchanged.
That is what this is - the console-era ``.pack`` depot presented the way the
pack2 reader presents an installation.

Two differences are worth knowing:

* A ``.pack`` has no name hashes; the stored name is the identity. Entries are
  keyed here by the same pack2 name hash anyway, so every caller that already
  looks assets up by hash keeps working, and nothing downstream has to care.
* Every entry in a ``.pack`` is named by construction, so the content scan that
  recovers names from a pack2 installation has nothing to do. ``declared_names``
  hands the catalogue the real list instead, which is both faster and exact.

A real install can mix the two: Just Survive ships 256 ``.pack`` archives beside
one ``data_x64_0.pack2``. Both are read, and which one a file is gets decided by
its header rather than its extension, so nothing in the folder stays invisible.
"""
from pathlib import Path

from .packs import Entry, basename, name_hash
import pack1tool as p1
import pack2read as p2

MAX_ASSET = 512 * 1024 * 1024


class Packs1:
    """Every ``.pack`` in one folder, indexed by name and by pack2 name hash."""

    format = 'pack'

    def __init__(self, root, exclude=()):
        self.root = Path(root).resolve()
        skip = {name.lower() for name in exclude}
        self.archives = [path for path in sorted(self.root.glob('*.pack*'))
                         if path.name.lower() not in skip and path.is_file()]
        if not self.archives:
            raise ValueError(f'No pack archives found in {self.root}. Point this studio at a Resources/Assets '
                             f'folder holding .pack or .pack2 files.')
        self.entries = {}
        self.names = {}
        self.unreadable = []
        self.formats = {}
        for path in self.archives:
            try:
                rows = self._read_pack2(path) if self._is_pack2(path) else self._read_pack1(path)
            except (p1.NotPack1, ValueError, OSError) as error:
                self.unreadable.append({'name': path.name, 'reason': str(error)})
                continue
            self.formats[path.name] = 'pack2' if self._is_pack2(path) else 'pack'
            for name, entry in rows:
                try:
                    self.entries.setdefault(name_hash(name), entry)
                except UnicodeEncodeError:
                    continue
                self.names.setdefault(name.lower(), name)
                # A name stored with folders is also reachable by its leaf, the way
                # every reference inside an ADR or DME writes it.
                leaf = basename(name).lower()
                if leaf != name.lower():
                    self.names.setdefault(leaf, name)
                    self.entries.setdefault(name_hash(basename(name)), entry)
        if not self.entries:
            raise ValueError(f'No readable pack archives in {self.root}')
        self.archive_count = len(self.archives)

    @staticmethod
    def _is_pack2(path):
        with path.open('rb') as source:
            return source.read(4) == b'PAK'

    @staticmethod
    def _read_pack1(path):
        return [(row['name'], Entry(path, row['offset'], row['length'], 0, row['crc']))
                for row in p1.read_archive(str(path))['entries']]

    @staticmethod
    def _read_pack2(path):
        info = p2.read_archive(str(path))
        with path.open('rb') as source:
            names = {p2.name_hash(n): n for n in p2.load_names(source, info['entries'])}
        out = []
        for entry in info['entries']:
            name = names.get(entry['hash'])
            if not name:
                continue
            # zip_flag is unreliable; Packs1.read decides by the payload magic.
            out.append((name, Entry(path, entry['offset'], entry['length'], 1, entry['data_hash'])))
        return out

    def declared_names(self):
        """``{hash: name}`` for everything in the depot, in place of a content scan."""
        return {f'{name_hash(name):016x}': name for name in self.names.values()}

    def find(self, name):
        for candidate in (name, basename(name)):
            try:
                row = self.entries.get(name_hash(candidate))
            except UnicodeEncodeError:
                continue
            if row:
                return row
        return None

    def read(self, name):
        row = self.find(name)
        if row is None:
            raise ValueError(f'Asset is absent: {basename(name)}')
        if row.size > MAX_ASSET:
            raise ValueError('Asset exceeds the 512 MiB extraction limit')
        with row.pack.open('rb') as source:
            source.seek(row.offset)
            raw = source.read(row.size)
        if len(raw) != row.size:
            raise ValueError('Truncated asset')
        # A .pack payload is stored raw; a .pack2 one may be zlib framed. The magic
        # says which, so a mixed folder needs no per-archive bookkeeping here.
        return p2.decode_payload(raw)
