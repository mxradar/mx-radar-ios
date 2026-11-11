"""Timestamp-driven replay. Seeking rebuilds protocol state from the beginning.
Every input event is consumed; only publishing is coalesced to 30 Hz.
"""
from array import array
import bisect
import json
from pathlib import Path
import threading
import time


class Timeline:
    def __init__(self, engine, source, stop):
        self.engine, self.source, self.stop = engine, Path(source), stop
        self.lock = threading.Lock()
        self.times, self.offsets = array('d'), array('Q')
        self.position = 0.0
        self.duration = 0.0
        self.speed = 1.0
        self.paused = True
        self.indexing = True
        self.seeking = False
        self.seek_request = None
        self.current = 0
        self.error = None

    def status(self):
        with self.lock:
            return dict(position=self.position, duration=self.duration, speed=self.speed,
                        paused=self.paused, indexing=self.indexing, seeking=self.seeking,
                        consumed=self.current, events=len(self.times), error=self.error)

    def control(self, query):
        with self.lock:
            if 'pause' in query:
                self.paused = query['pause'][0] != '0'
            if 'speed' in query:
                speed = float(query['speed'][0])
                if not 0.1 <= speed <= 16:
                    raise ValueError('回放倍速须在 0.1 至 16 之间')
                self.speed = speed
            if 'seek' in query:
                fraction = float(query['seek'][0]) / 1000
                if not 0 <= fraction <= 1:
                    raise ValueError('回放位置无效')
                self.seek_request = fraction * self.duration
        return {'ok': True, 'replay': self.status()}

    def run(self):
        try:
            first = None
            last = 0.0
            with self.source.open('rb') as f:
                while not self.stop.is_set():
                    offset = f.tell()
                    line = f.readline(2 * 1024 * 1024 + 1)
                    if not line:
                        break
                    if len(line) > 2 * 1024 * 1024:
                        raise ValueError('回放记录行超过上限')
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    value = (event.get('data') or {}).get('timestampMs', event.get('timestampMs'))
                    if value is not None:
                        value = float(value) / 1000
                        if first is None:
                            first = value
                        last = max(last, value - first)
                    self.offsets.append(offset)
                    self.times.append(last)
            with self.lock:
                self.duration = last
                self.indexing = False
            self.engine.flush(force=True)
            anchor = last_publish = time.monotonic()
            with self.source.open('rb') as f:
                while not self.stop.is_set():
                    now = time.monotonic()
                    with self.lock:
                        seek = self.seek_request
                        self.seek_request = None
                        if seek is not None:
                            self.position = seek
                            self.seeking = True
                        elif not self.paused:
                            self.position = min(self.duration, self.position + (now - anchor) * self.speed)
                        target = self.position
                    anchor = now
                    if seek is not None:
                        self.engine.restart_replay()
                        self.current = 0
                    boundary = bisect.bisect_right(self.times, target)
                    budget_start = time.monotonic()
                    while self.current < boundary and not self.stop.is_set():
                        f.seek(self.offsets[self.current])
                        self.engine.accept(json.loads(f.readline()))
                        self.current += 1
                        if time.monotonic() - budget_start > 0.02:
                            break
                    with self.lock:
                        if self.current >= boundary:
                            self.seeking = False
                        if self.current >= len(self.times) and not self.seeking:
                            self.paused = True
                    if now - last_publish >= 1 / 30:
                        self.engine.flush()
                        last_publish = now
                    self.stop.wait(0.002)
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
                self.paused = True
            self.engine.fatal_error = '回放错误：' + str(exc)
            self.engine.flush(force=True)
