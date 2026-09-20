import json
import threading
import time

from curie import caches
from curie.caches import TTLCache


def test_ttl_and_counters():
    c = TTLCache("t_ttl", ttl=0.05, max_entries=10)
    assert c.get("a") is None
    c.put("a", 1)
    assert c.get("a") == 1
    time.sleep(0.06)
    assert c.get("a") is None            # expired on access
    s = c.stats()
    assert s["hits"] == 1 and s["misses"] == 2 and s["entries"] == 0
    c.put("b", 2)
    assert c.expire() == 0
    time.sleep(0.06)
    assert c.expire() == 1 and len(c) == 0


def test_lru_eviction_keeps_recently_used():
    c = TTLCache("t_lru", ttl=60, max_entries=3)
    for k in "abc":
        c.put(k, k)
    assert c.get("a") == "a"             # a becomes most recently used
    c.put("d", "d")                      # evicts b, the least recently used
    assert c.get("b") is None
    assert c.get("a") == "a" and c.get("c") == "c" and c.get("d") == "d"
    assert c.stats()["evictions"] == 1


def test_zero_ttl_never_hits():
    c = TTLCache("t_zero", ttl=0, max_entries=5)
    c.put("k", "v")
    assert c.get("k") is None


def test_registry_stats_and_flush_all():
    a = TTLCache("t_reg_a", ttl=60)
    b = TTLCache("t_reg_b", ttl=60)
    a.put(1, 1)
    b.put(1, 1)
    b.put(2, 2)
    assert caches.stats()["t_reg_a"]["entries"] == 1
    assert caches.stats()["t_reg_b"]["entries"] == 2
    flushed = caches.flush_all()
    assert flushed["t_reg_a"] == 1 and flushed["t_reg_b"] == 2
    assert len(a) == 0 and len(b) == 0
    assert caches.get("t_reg_a") is a


def test_thread_safety_smoke():
    c = TTLCache("t_threads", ttl=60, max_entries=50)
    errors = []

    def worker(n):
        try:
            for i in range(500):
                c.put((n, i % 70), i)
                c.get((n, (i * 7) % 70))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(c) <= 50


# -- persistence ----------------------------------------------------------------- #

def test_persistent_cache_round_trip(tmp_path, monkeypatch):
    from curie import caches, config
    monkeypatch.setattr(config, "CACHE_PERSIST", True)
    monkeypatch.setattr(config, "CACHE_PERSIST_TTL", 3600.0)
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    c = caches.TTLCache("t_structures", ttl=1.0, persist=True)
    assert c.ttl == 3600.0 and c.persist
    c.put("MARA", {"fields": [{"name": "MATNR"}]})
    c.put("OLD", {"fields": []}, age=7200)            # already older than the TTL
    assert c.save() and (tmp_path / "t_structures.json").exists()
    assert c.save() is False                          # nothing changed since

    fresh = caches.TTLCache("t_structures", ttl=1.0, persist=True)
    assert fresh.load() == 1 and fresh.get("MARA") == {"fields": [{"name": "MATNR"}]}
    assert fresh.get("OLD") is None and fresh.stats()["loaded"] == 1
    assert fresh.stats()["oldest_age"] >= 0 and fresh.stats()["persist"]
    disk = caches.disk_stats(tmp_path)
    assert disk["t_structures"]["entries"] == 2 and disk["t_structures"]["same_system"]

    # another system's file is ignored
    monkeypatch.setitem(config.SAP_RFC, "client", "999")
    other = caches.TTLCache("t_structures", ttl=1.0, persist=True)
    assert other.load() == 0 and caches.disk_stats(tmp_path)["t_structures"]["same_system"] is False
    monkeypatch.setitem(config.SAP_RFC, "client", config.SAP_RFC["client"])

    # a flush followed by a save removes the file (flush_all does both); purge_disk removes named files
    fresh.put("X", 1)
    fresh.save()
    assert fresh.flush() >= 1 and fresh.save()
    assert not (tmp_path / "t_structures.json").exists()
    fresh.put("Y", 2)
    fresh.save()
    assert caches.purge_disk(tmp_path, ["nope"]) == [] and caches.purge_disk(tmp_path) == ["t_structures"]
    # a non-persistent cache never touches the disk
    plain = caches.TTLCache("t_results", ttl=1.0)
    plain.put("k", "v")
    assert plain.save() is False and plain.load() == 0 and not plain.persist


def test_persist_switch_off(tmp_path, monkeypatch):
    from curie import caches, config
    monkeypatch.setattr(config, "CACHE_PERSIST", False)
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    c = caches.TTLCache("t_off", ttl=5.0, persist=True)
    assert not c.persist and c.ttl == 5.0
    c.put("a", 1)
    assert c.save() is False and list(tmp_path.glob("*.json")) == []


def test_persistence_is_strict_and_codecs_round_trip(tmp_path, monkeypatch, caplog):
    from dataclasses import asdict
    from curie import caches, config
    from curie.sap_tools.ddic import decode_structure, encode_structure
    from curie.sap_tools.read_table import FieldInfo
    monkeypatch.setattr(config, "CACHE_PERSIST", True)
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    # a dataclass without a codec is refused, not turned into its repr
    plain = caches.TTLCache("t_strict", ttl=10, persist=True)
    plain.put("X", {"fields": [FieldInfo(name="MATNR")]})
    assert plain.save() is False and not (tmp_path / "t_strict.json").exists()
    assert "not JSON-native" in caplog.text
    # the structure cache's codec keeps FieldInfo objects in memory and dicts on disk
    c = caches.TTLCache("t_ddic", ttl=10, persist=True, encode=encode_structure, decode=decode_structure)
    info = {"name": "MARA", "text": "Material", "fields": [FieldInfo(name="MATNR", key=True, intlen=18)]}
    c.put("MARA", info)
    assert c.save()
    on_disk = json.loads((tmp_path / "t_ddic.json").read_text(encoding="utf-8"))
    assert on_disk["entries"]["MARA"]["v"]["fields"][0] == asdict(info["fields"][0])
    back = caches.TTLCache("t_ddic", ttl=10, persist=True, encode=encode_structure, decode=decode_structure)
    assert back.load() == 1
    field = back.get("MARA")["fields"][0]
    assert isinstance(field, FieldInfo) and field.name == "MATNR" and field.key and field.width
    # an entry that cannot be decoded (the old repr strings) is skipped with a warning
    on_disk["entries"]["BAD"] = {"t": on_disk["entries"]["MARA"]["t"], "v": {"name": "BAD", "fields": ["FieldInfo(name='X')"]}}
    (tmp_path / "t_ddic.json").write_text(json.dumps(on_disk), encoding="utf-8")
    again = caches.TTLCache("t_ddic", ttl=10, persist=True, encode=encode_structure, decode=decode_structure)
    assert again.load() == 1 and again.get("BAD") is None and "could not be decoded" in caplog.text
