"""In-process replacement for the server's multiprocessing notice scanner.

Android has no usable fork/ProcessPool for this workload, so the same pure
parsing runs in a worker thread instead.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import time


class NoticeWorker:
    def __init__(self, *args, **kwargs):
        self._slots = asyncio.Semaphore(1)
        self._cache = OrderedDict()
        self._pending = {}
        # One retained 4 MiB native scratch buffer, rather than one per thread
        # of asyncio's shared executor. Raw-key checks keep a separate lane.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='notice-scan')
        self.calls = self.cache_hits = self.fast_matches = self.full_scans = 0
        self.queue_ms = self.work_ms = 0.0

    def close(self) -> None:
        for task in self._pending.values():
            task.cancel()
        self._cache.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def status(self):
        return dict(calls=self.calls, cacheHits=self.cache_hits, fastMatches=self.fast_matches,
                    fullScans=self.full_scans, pending=len(self._pending),
                    queueMs=round(self.queue_ms,2), workMs=round(self.work_ms,2))

    async def parse(self, command: int, plain: bytes):
        self.calls += 1
        key = command, hashlib.sha256(plain).digest()
        if key in self._cache:
            self.cache_hits += 1
            self._cache.move_to_end(key)
            return self._cache[key]
        task = self._pending.get(key)
        if task is None:
            task = self._pending[key] = asyncio.create_task(self._parse(command, plain))
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._pending.pop(key, None)
                if not task.cancelled() and task.exception() is None:
                    self._cache[key] = task.result()
                    self._cache.move_to_end(key)
                    while len(self._cache) > 256:
                        self._cache.popitem(last=False)

    async def _parse(self, command, plain):
        from .seasonbridge import match_credentials_raw, match_credentials_combined
        # Verified raw credentials have their own fast lane, ahead of queued
        # speculative compressed scans. The original strict validation is used.
        result = await asyncio.to_thread(match_credentials_raw, command, plain)
        if result is not None:
            self.fast_matches += 1
            return result
        queued = time.monotonic()
        async with self._slots:
            self.queue_ms = (time.monotonic()-queued)*1000
            self.full_scans += 1
            def work():
                began = time.monotonic()
                try:
                    return match_credentials_combined(command, plain)
                finally:
                    self.work_ms = (time.monotonic()-began)*1000
            return await asyncio.get_running_loop().run_in_executor(self._executor, work)
