"""Retain the complete running VPN session until it is explicitly stopped.

Admission ACKs do not make an in-memory decoder durable. Its next incarnation
must be able to request the session again, including already acknowledged input.
One background writer owns this bounded journal; forwarding never waits for ACKs.
"""
from array import array
import json
from pathlib import Path
import queue
import struct
import threading
import time


PROTOCOL = 'radar-event-replay/v1'


class SessionReplay:
    def __init__(self, directory, session_id, max_bytes=1024*1024*1024):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        # Previous relay processes have lost their game crypto state too. Only
        # the current relay session may be replayed to a restarted parser.
        for old in directory.glob('session-*.bin'):
            old.unlink(missing_ok=True)
        self.path = directory / ('session-'+session_id+'.bin')
        self._file = self.path.open('x+b')
        self._offsets = array('Q')
        self._bytes = 0
        self._limit = max_bytes
        self._closed = False
        self._condition = threading.Condition()

    def append(self, event):
        body = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        frame = struct.pack('<I', len(body))+body
        with self._condition:
            if self._closed:
                raise RuntimeError('Replay session closed')
            if event['sequence'] != len(self._offsets)+1:
                raise ValueError('Non-contiguous replay journal')
            if self._bytes+len(frame) > self._limit:
                raise OSError('Session replay storage limit reached; reconnect radar before next match')
            self._file.seek(self._bytes)
            self._file.write(frame)
            self._file.flush()
            self._offsets.append(self._bytes)
            self._bytes += len(frame)
            self._condition.notify_all()

    def read(self, sequence, timeout=.5):
        deadline = time.monotonic()+timeout
        with self._condition:
            while len(self._offsets) < sequence:
                if self._closed:
                    raise queue.Empty
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            if sequence < 1:
                raise ValueError('Invalid replay sequence')
            self._file.seek(self._offsets[sequence-1])
            header = self._file.read(4)
            if len(header) != 4:
                raise OSError('Truncated replay header')
            size = struct.unpack('<I', header)[0]
            body = self._file.read(size)
            if len(body) != size:
                raise OSError('Truncated replay event')
            return body

    def status(self):
        with self._condition:
            return dict(events=len(self._offsets), bytes=self._bytes, limitBytes=self._limit)

    def close(self):
        with self._condition:
            if not self._closed:
                self._closed = True
                self._file.close()
                self.path.unlink(missing_ok=True)
                self._condition.notify_all()
