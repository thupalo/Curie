"""One cache helper for all metadata and result caches.

Timestamps use perf_counter: time.monotonic() has a ~16 ms resolution on
Windows, which is coarser than the TTLs the tests use.

TTLCache: thread-safe, time-to-live per entry, a maximum number of entries
with least-recently-used eviction, hit/miss counters, flush. Every cache
registers itself by name so /healthz can show the counters and an operator
can flush them all without a restart.

    cache = TTLCache("ddic", ttl=3600, max_entries=500)
    hit = cache.get(key)          # None on miss or expiry
    cache.put(key, value)
    stats() / flush_all()          # across all registered caches

Persistence (CACHE_PERSIST): caches created with persist=True are also
kept as JSON under CACHE_DIR, one file per cache, tagged with the SAP
system they came from. load_all() at startup puts the entries back with
their real age, so a restart does not start with an empty dictionary
cache; save_all() runs every CACHE_SAVE_INTERVAL seconds, at shutdown and
after `curie --cache warm`. Persistent caches use CACHE_PERSIST_TTL (days)
instead of the in-memory hour: dictionary metadata changes with
transports, not with the clock, and `curie --cache purge` or
POST /admin/cache/flush drops it when it did. Result caches (table reads)
never persist.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path
from time import perf_counter
from typing import Any

from curie import config

log = logging.getLogger(__name__)

_registry: dict[str, "TTLCache"] = {}
_registry_lock = threading.Lock()

_MISSING = object()
FORMAT = 1


def system_key() -> str:
    """Which backend the persisted entries belong to."""
    rfc = config.SAP_RFC
    return f"{rfc.get('ashost', '')}:{rfc.get('sysnr', '')}/{rfc.get('client', '')}"


class TTLCache:
    def __init__(self, name: str, ttl: float, max_entries: int = 500, persist: bool = False,
                 encode=None, decode=None):
        self.name = name
        self.persist = bool(persist) and config.CACHE_PERSIST
        # Persistent caches hold JSON-native values, or supply a codec:
        # encode(value) -> JSON-native, decode(json) -> value. Saving is
        # strict: a value JSON cannot carry is a warning and a skipped save,
        # never a silently mangled entry.
        self.encode = encode
        self.decode = decode
        self.ttl = float(ttl)
        if self.persist:
            self.ttl = max(self.ttl, float(config.CACHE_PERSIST_TTL))
        self.max_entries = max(1, int(max_entries))
        # key -> (perf stamp, value, wall-clock stamp)
        self._data: OrderedDict[Any, tuple[float, Any, float]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.loaded = 0
        self.dirty = False
        with _registry_lock:
            _registry[name] = self

    def get(self, key, default=None):
        """The cached value, or default when absent or older than ttl."""
        now = perf_counter()
        with self._lock:
            entry = self._data.get(key, _MISSING)
            if entry is _MISSING:
                self.misses += 1
                return default
            stamp, value, _wall = entry
            if now - stamp >= self.ttl:
                del self._data[key]
                self.misses += 1
                self.dirty = self.dirty or self.persist
                return default
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key, value, age: float = 0.0) -> None:
        """Store a value; age (seconds) is how old it already is (disk loads)."""
        with self._lock:
            self._data[key] = (perf_counter() - age, value, time.time() - age)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
                self.evictions += 1
            self.dirty = self.dirty or self.persist

    def flush(self) -> int:
        with self._lock:
            count = len(self._data)
            self._data.clear()
            self.dirty = self.dirty or self.persist
            return count

    def expire(self) -> int:
        """Drop expired entries now (they are otherwise dropped on access)."""
        now = perf_counter()
        with self._lock:
            dead = [k for k, (stamp, _v, _w) in self._data.items() if now - stamp >= self.ttl]
            for k in dead:
                del self._data[k]
            if dead:
                self.dirty = self.dirty or self.persist
            return len(dead)

    def keys(self) -> list:
        with self._lock:
            return list(self._data.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def stats(self) -> dict:
        with self._lock:
            ages = [time.time() - w for (_s, _v, w) in self._data.values()]
            return {
                "entries": len(self._data), "max_entries": self.max_entries,
                "ttl": self.ttl, "hits": self.hits, "misses": self.misses,
                "evictions": self.evictions, "persist": self.persist, "loaded": self.loaded,
                "oldest_age": int(max(ages)) if ages else 0,
            }

    # -- persistence ------------------------------------------------------------- #

    def path(self, directory: str | Path | None = None) -> Path:
        return Path(directory or config.CACHE_DIR) / f"{self.name}.json"

    def save(self, directory: str | Path | None = None, force: bool = False) -> bool:
        """Write the entries to disk; False when nothing changed or not persistent."""
        if not self.persist or (not self.dirty and not force):
            return False
        path = self.path(directory)
        with self._lock:
            items = [(str(k), w, v) for k, (_s, v, w) in self._data.items()]
            self.dirty = False
        try:
            entries = {}
            for key, wall, value in items:
                entries[key] = {"t": wall, "v": self.encode(value) if self.encode else value}
            document = json.dumps({"format": FORMAT, "cache": self.name, "system": system_key(),
                                   "saved": time.time(), "entries": entries}, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            log.warning("cache %s not saved, a value is not JSON-native: %s", self.name, exc)
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not entries:
                if path.exists():
                    path.unlink()
                return True
            tmp = path.with_suffix(".tmp")
            tmp.write_text(document, encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning("cache %s not saved: %s", self.name, exc)
            return False
        return True

    def load(self, directory: str | Path | None = None) -> int:
        """Put the disk entries back with their real age; returns how many."""
        if not self.persist:
            return 0
        path = self.path(directory)
        if not path.exists():
            return 0
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("cache %s not loaded: %s", self.name, exc)
            return 0
        if doc.get("format") != FORMAT or doc.get("system") != system_key():
            log.info("cache %s on disk is for %s, not %s: ignored", self.name,
                     doc.get("system"), system_key())
            return 0
        now = time.time()
        count = 0
        skipped = 0
        for key, entry in (doc.get("entries") or {}).items():
            age = now - float(entry.get("t", 0))
            if age < 0 or age >= self.ttl:
                continue
            try:
                value = self.decode(entry.get("v")) if self.decode else entry.get("v")
            except Exception as exc:  # noqa: BLE001 -- an old or foreign file: skip the entry
                skipped += 1
                log.debug("cache %s: entry %s not decoded: %s", self.name, key, exc)
                continue
            self.put(key, value, age=age)
            count += 1
        if skipped:
            log.warning("cache %s: %d entr%s on disk could not be decoded and were skipped "
                        "(curie --cache purge --names %s, then warm again)", self.name, skipped,
                        "y" if skipped == 1 else "ies", self.name)
        with self._lock:
            self.dirty = False
            self.loaded = count
        return count


def stats() -> dict[str, dict]:
    with _registry_lock:
        caches = list(_registry.values())
    return {c.name: c.stats() for c in caches}


def flush_all() -> dict[str, int]:
    """Empty every cache, in memory and on disk."""
    with _registry_lock:
        caches = list(_registry.values())
    out = {c.name: c.flush() for c in caches}
    for c in caches:
        c.save()
    return out


def get(name: str) -> "TTLCache | None":
    with _registry_lock:
        return _registry.get(name)


def persistent() -> list["TTLCache"]:
    with _registry_lock:
        return [c for c in _registry.values() if c.persist]


def load_all(directory: str | Path | None = None) -> dict[str, int]:
    return {c.name: c.load(directory) for c in persistent()}


def save_all(directory: str | Path | None = None, force: bool = False) -> dict[str, bool]:
    return {c.name: c.save(directory, force) for c in persistent()}


def disk_stats(directory: str | Path | None = None) -> dict[str, dict]:
    """What is on disk, readable without any cache object (the admin CLI)."""
    out: dict[str, dict] = {}
    directory = Path(directory or config.CACHE_DIR)
    if not directory.exists():
        return out
    now = time.time()
    for path in sorted(directory.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[path.stem] = {"error": "unreadable"}
            continue
        ages = [now - float(e.get("t", now)) for e in (doc.get("entries") or {}).values()]
        out[path.stem] = {
            "entries": len(ages), "system": doc.get("system"), "bytes": path.stat().st_size,
            "saved_age": int(now - float(doc.get("saved", now))),
            "oldest_age": int(max(ages)) if ages else 0, "newest_age": int(min(ages)) if ages else 0,
            "same_system": doc.get("system") == system_key(),
        }
    return out


def purge_disk(directory: str | Path | None = None, names: list[str] | None = None) -> list[str]:
    """Delete the cache files (all, or the named ones)."""
    directory = Path(directory or config.CACHE_DIR)
    removed = []
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        if names and path.stem not in names:
            continue
        path.unlink()
        removed.append(path.stem)
    return removed
