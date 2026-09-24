"""Many LZHAM files at once: every core, and a cache so nothing is decoded twice.

Pure Python decodes a busy 2.8 MB terrain texture file in ~1.5 s and compresses one in
~15 s. One file is as fast as it gets; a folder of them is not - the work is independent
per file, so it spreads over processes (this PC: 12 cores), and decoded payloads are
kept in a cache keyed by the compressed bytes' SHA-1, so re-running a job is instant.

    from lzham.batch import decode_files, encode_files
    for path, payload in decode_files(paths, cache=Path('cache/lzham')):   # generator
        ...

On Windows, call these from under `if __name__ == '__main__':` (process spawn).
"""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

__all__ = ['decode_files', 'encode_files', 'default_workers']


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def _cache_path(cache, raw):
    return Path(cache) / f'{hashlib.sha1(raw).hexdigest()}.bin' if cache else None


def _decode_one(path, cache):
    try:
        return _decode_one_inner(path, cache)
    except Exception as error:               # reported per file; the batch carries on
        return str(path), error, False


def _decode_one_inner(path, cache):
    from .container import read_container
    raw = Path(path).read_bytes()
    hit = _cache_path(cache, raw)
    if hit and hit.is_file():
        return str(path), hit.read_bytes(), True
    c = read_container(raw)
    payload = c.decode()
    if hit:
        # identical files (the many empty edge tiles) share a key: each worker writes its
        # own temp file, and losing the race to an identical copy is fine
        hit.parent.mkdir(parents=True, exist_ok=True)
        tmp = hit.with_suffix(f'.{os.getpid()}.tmp')
        tmp.write_bytes(payload)
        try:
            tmp.replace(hit)
        except OSError:
            tmp.unlink(missing_ok=True)
    return str(path), payload, False


def decode_files(paths, workers=None, cache=None, progress=None, on_error=None):
    """Yield (path, payload) for .cnk/.ctg files, decoded in parallel (order not kept).
    A file that fails goes to on_error(path, exception) and is skipped; without on_error
    its exception is raised."""
    paths = [str(p) for p in paths]
    workers = workers or default_workers()

    def results():
        if workers == 1:
            for p in paths:
                yield _decode_one(p, cache)
            return
        with ProcessPoolExecutor(workers) as pool:
            for f in as_completed([pool.submit(_decode_one, p, cache) for p in paths]):
                yield f.result()
    done = 0
    for path, payload, cached in results():
        done += 1
        if progress:
            progress(done, len(paths), path, cached)
        if isinstance(payload, Exception):
            if not on_error:
                raise payload
            on_error(path, payload)
            continue
        yield path, payload


def _encode_one(job):
    from .container import write_container
    out, magic, version, payload, chain = job
    data = write_container(magic, version, payload, compress=True, chain=chain)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_bytes(data)
    return out, len(payload), len(data)


def encode_files(jobs, workers=None, chain=24, progress=None):
    """Write containers in parallel. jobs: iterable of (out_path, magic, version, payload).
    Yields (out_path, payload_bytes, file_bytes)."""
    jobs = [(str(o), m, v, p, chain) for o, m, v, p in jobs]
    workers = workers or default_workers()
    done = 0
    if workers == 1:
        for j in jobs:
            r = _encode_one(j)
            done += 1
            if progress:
                progress(done, len(jobs), r[0], False)
            yield r
        return
    with ProcessPoolExecutor(workers) as pool:
        for f in as_completed([pool.submit(_encode_one, j) for j in jobs]):
            r = f.result()
            done += 1
            if progress:
                progress(done, len(jobs), r[0], False)
            yield r
