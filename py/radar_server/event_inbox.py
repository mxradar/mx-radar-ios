"""Ordered event inbox: bounded RAM, segmented disk overflow, no freshness drops.

Only derived state snapshots may be replaced. UE4 input events must survive
decode stalls because their channel/handshake history cannot be reconstructed
from an arbitrary later packet. Disk capacity exhaustion applies backpressure.
"""
from collections import deque
import json
from pathlib import Path
import queue
import struct
import threading
import time
import uuid


class EventInbox:
    def __init__(self, directory, memory_events=128, max_disk_bytes=256*1024*1024,
                 segment_bytes=4*1024*1024):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        # One owner per private directory. A fresh parser session cannot reuse
        # old session events left by an Android process kill.
        for old in self.directory.glob('inbox-*.bin'):
            old.unlink(missing_ok=True)
        self.memory_events, self.max_disk_bytes = memory_events, max_disk_bytes
        self.segment_bytes = segment_bytes
        self._memory, self._segments = deque(), deque()
        self._condition = threading.Condition()
        self._disk_count = self._disk_bytes = self.spooled = self.high_water = 0
        self._closed = False

    def qsize(self):
        with self._condition:
            return len(self._memory) + self._disk_count

    def empty(self):
        return self.qsize() == 0

    def put(self, event, block=True, timeout=None):
        deadline = None if timeout is None else time.monotonic()+timeout
        with self._condition:
            if self._closed:
                raise RuntimeError('Event inbox closed')
            if not self._segments and len(self._memory) < self.memory_events:
                self._memory.append(event)
            else:
                body = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                frame = struct.pack('<I', len(body)) + body
                if len(frame) > self.max_disk_bytes:
                    raise ValueError('One event exceeds the inbox disk capacity')
                while self._disk_bytes + len(frame) > self.max_disk_bytes:
                    if not block: raise queue.Full
                    remaining = None if deadline is None else deadline-time.monotonic()
                    if remaining is not None and remaining <= 0: raise queue.Full
                    self._condition.wait(remaining)
                    if self._closed: raise RuntimeError('Event inbox closed')
                if not self._segments or self._segments[-1]['end'] >= self.segment_bytes:
                    path = self.directory / ('inbox-'+uuid.uuid4().hex+'.bin')
                    self._segments.append(dict(path=path, file=path.open('x+b'), read=0, end=0, count=0))
                segment = self._segments[-1]
                handle = segment['file']
                handle.seek(segment['end'])
                handle.write(frame)
                handle.flush()
                segment['end'] += len(frame)
                segment['count'] += 1
                self._disk_count += 1
                self._disk_bytes += len(frame)
                self.spooled += 1
            self.high_water = max(self.high_water, len(self._memory)+self._disk_count)
            self._condition.notify_all()

    def put_nowait(self, event):
        self.put(event, block=False)

    def get(self, block=True, timeout=None):
        deadline = None if timeout is None else time.monotonic()+timeout
        with self._condition:
            while not self._memory and not self._segments:
                if not block or self._closed: raise queue.Empty
                remaining = None if deadline is None else deadline-time.monotonic()
                if remaining is not None and remaining <= 0: raise queue.Empty
                self._condition.wait(remaining)
            if self._memory:
                event = self._memory.popleft()
                self._condition.notify_all()
                return event
            segment = self._segments[0]
            handle = segment['file']
            handle.seek(segment['read'])
            header = handle.read(4)
            if len(header) != 4: raise OSError('Truncated event inbox header')
            size = struct.unpack('<I', header)[0]
            body = handle.read(size)
            if len(body) != size: raise OSError('Truncated event inbox record')
            segment['read'] += size+4
            segment['count'] -= 1
            self._disk_count -= 1
            self._disk_bytes -= size+4
            if not segment['count']:
                self._segments.popleft()
                handle.close()
                segment['path'].unlink()
            self._condition.notify_all()
        return json.loads(body)

    def get_nowait(self):
        return self.get(block=False)

    def status(self):
        with self._condition:
            return dict(memory=len(self._memory), diskEvents=self._disk_count,
                        diskBytes=self._disk_bytes, spooled=self.spooled, highWater=self.high_water)

    def close(self):
        with self._condition:
            self._closed = True
            for segment in self._segments:
                segment['file'].close()
                segment['path'].unlink(missing_ok=True)
            self._segments.clear()
            self._memory.clear()
            self._disk_count = self._disk_bytes = 0
            self._condition.notify_all()
