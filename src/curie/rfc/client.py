"""A pool of client connections and the blocking/async call API on top.

An SDK connection handle may be used by one thread at a time, so the pool
hands out a sapnwrfc720.Client to one caller at a time. Calls are blocking
(ctypes releases the GIL while the SDK waits); the asyncio side goes through
acall(), which runs the call in the pool's executor.

Interface awareness: the exact parameter lists of RFC_READ_TABLE and the
RFC_GET_*_P functions differ between 4.5B and ECC. call_known() drops
parameters the backend does not have so the tools can name every parameter
they would like to use and still run on both.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from curie import config
from curie import sapnwrfc720 as sap
from curie.caches import TTLCache

log = logging.getLogger(__name__)


class PoolExhausted(RuntimeError):
    """No client connection became free within the wait."""


class SapClient:
    """Thread-safe access to the backend through a fixed number of connections."""

    def __init__(self, params: dict, size: int = 2, acquire_timeout: float = 60.0):
        self.params = dict(params)
        self.size = max(1, int(size))
        self.acquire_timeout = acquire_timeout
        self._free: queue.Queue = queue.Queue()
        for _ in range(self.size):
            self._free.put(sap.Client(self.params))
        self._all = list(self._free.queue)
        self._executor = ThreadPoolExecutor(
            max_workers=self.size, thread_name_prefix="rfc-client")
        # Interfaces of the function modules Curie itself calls. Same TTL as
        # the dictionary caches, so a transported interface change is seen
        # within the hour (or after POST /admin/cache/flush).
        self._iface_cache = TTLCache("rfc_interfaces", config.DDIC_CACHE_TTL,
                                     config.CACHE_MAX_ENTRIES, persist=True)

    # -- pool --------------------------------------------------------------- #

    @contextmanager
    def acquire(self):
        try:
            client = self._free.get(timeout=self.acquire_timeout)
        except queue.Empty:
            raise PoolExhausted(
                f"no free RFC connection within {self.acquire_timeout}s") from None
        try:
            yield client
        finally:
            self._free.put(client)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        for client in self._all:
            try:
                client.close()
            except Exception:  # noqa: BLE001 -- best effort on shutdown
                pass

    # -- calls -------------------------------------------------------------- #

    def call(self, func_name: str, **params) -> dict:
        """Blocking call; any thread. ABAPException / RFCError as in Client.call."""
        started = time.monotonic()
        with self.acquire() as client:
            try:
                return client.call(func_name, **params)
            finally:
                log.debug("%s took %.0f ms", func_name.upper(),
                          (time.monotonic() - started) * 1000)

    def call_known(self, func_name: str, **params) -> dict:
        """Like call(), but silently drops parameters the backend lacks."""
        names = self.param_names(func_name)
        known = {k: v for k, v in params.items() if k.upper() in names}
        dropped = sorted(set(params) - set(known))
        if dropped:
            log.debug("%s: backend has no %s, dropped", func_name.upper(),
                      ", ".join(dropped))
        return self.call(func_name, **known)

    async def acall(self, func_name: str, **params) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: self.call(func_name, **params))

    async def acall_known(self, func_name: str, **params) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor, lambda: self.call_known(func_name, **params))

    async def run(self, fn, *args):
        """Run any blocking function in the pool's executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args))

    # -- metadata ----------------------------------------------------------- #

    def interface(self, func_name: str) -> list[dict]:
        """Parameters of a function module as dicts (cached per process)."""
        func_name = func_name.upper()
        cached = self._iface_cache.get(func_name)
        if cached is not None:
            return cached
        with self.acquire() as client:
            iface = client.interface(func_name)
        self._iface_cache.put(func_name, iface)
        return iface

    def param_names(self, func_name: str) -> set[str]:
        return {p["name"] for p in self.interface(func_name)}

    def has_params(self, func_name: str, *names: str) -> bool:
        have = self.param_names(func_name)
        return all(n.upper() in have for n in names)

    def table_line_length(self, func_name: str, param: str) -> int | None:
        """Character length of a TABLES parameter's line, if it is a flat line."""
        for p in self.interface(func_name):
            if p["name"] == param.upper():
                return p["length"] or None
        return None

    # -- health --------------------------------------------------------------- #

    def ping(self) -> None:
        with self.acquire() as client:
            client.ping()

    def system_info(self) -> dict:
        return self.call("RFC_SYSTEM_INFO")["RFCSI_EXPORT"]

    def attributes(self) -> dict:
        with self.acquire() as client:
            return client.attributes()
