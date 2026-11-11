"""Opt-in, exact-handshake session keys with bounded delayed-key replay.

The private files are written by the runtime collector, never by UDP peers.
No key bytes are emitted. Transport forwarding must not depend on this module.
"""
from __future__ import annotations
import base64
from collections import deque
import hashlib
import itertools
import json
import os
from pathlib import Path
import time
from typing import Any
import uuid

from . import tea
from .xtea import decrypt_packet

MAX_WAIT_MS = 180_000
MAX_HISTORY_BYTES = 8 * 1024 * 1024
MAX_HISTORY_PACKETS = 8192
DRAIN_BATCH = 64


def account_id(account: str) -> str:
    return hashlib.sha256(account.encode()).hexdigest()


def key_id(account: str, remote: str, session_id: str) -> str:
    return hashlib.sha256((account + '\0' + remote + '\0' + session_id).encode()).hexdigest()


def _document(path: Path) -> dict | None:
    try:
        if path.stat().st_size > 4096:
            return None
        value = json.loads(path.read_bytes())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


class SessionKeyStore:
    def __init__(self, directory: Path | None, pending_directory: Path | None):
        self.directory = directory
        self.pending_directory = pending_directory

    def start(self, packet: Any) -> RuntimeSession | None:
        if self.directory is None or self.pending_directory is None or not packet.account:
            return None
        marker = _document(self.directory / ('armed-' + account_id(packet.account) + '.json'))
        now = int(time.time() * 1000)
        if not marker or marker.get('account') != packet.account:
            return None
        expires = marker.get('expiresAtMs')
        if type(expires) is not int or not now < expires <= now + 1_800_000:
            return None
        return RuntimeSession(self.directory, self.pending_directory, packet, now)


class RuntimeSession:
    def __init__(self, directory: Path, pending_directory: Path, first: Any, now: int):
        self.directory, self.pending_directory = directory, pending_directory
        self.account, self.local, self.remote = first.account, first.local, first.remote
        self.initial_sha256 = hashlib.sha256(first.payload).hexdigest()
        self.started_ms = now
        self.session_id = uuid.uuid4().hex
        self.waiting_path = directory / ('waiting-' + self.session_id + '.json')
        metadata = {'sessionId': self.session_id, 'account': self.account,
                    'local': self.local, 'remote': self.remote,
                    'initialSha256': self.initial_sha256, 'startedAtMs': now,
                    'expiresAtMs': now + MAX_WAIT_MS}
        descriptor = os.open(self.waiting_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(metadata, handle)
        self.packet_type = type(first)
        self.pending = deque()
        self.probes = deque(maxlen=5)
        self.arrivals = deque()
        self.history_bytes = self.history_packets = 0
        self.journal_path = None
        self.journal = None
        self.key = None
        self.fingerprint = None
        self.expires_ms = 0
        self.next_poll = 0.0
        self.last_sequence = None
        self.drain_iterator = None
        self.rejected_packets = 0

    @property
    def bound(self):
        return self.key is not None

    def _hold(self, packet):
        self.history_packets += 1
        self.history_bytes += len(packet.payload)
        if self.history_packets > MAX_HISTORY_PACKETS or self.history_bytes > MAX_HISTORY_BYTES:
            raise RuntimeError('session history limit')
        self.pending.append(packet)
        if len(self.pending) > 64:
            if self.journal is None:
                self.pending_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.journal_path = self.pending_directory / ('session-' + uuid.uuid4().hex + '.jsonl')
                descriptor = os.open(self.journal_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self.journal = os.fdopen(descriptor, 'w', encoding='utf-8')
            oldest = self.pending.popleft()
            self.journal.write(json.dumps({'up': oldest.up, 'timestamp_ms': oldest.timestamp_ms,
                                          'payload': base64.b64encode(oldest.payload).decode()}) + '\n')
        if not packet.up and len(packet.payload) >= 10 and not packet.payload[0] & 1:
            if not self.probes or packet.payload != self.probes[-1].payload:
                self.probes.append(packet)

    def _bind(self):
        if len(self.probes) < 5 or time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + .25
        document = _document(self.directory / (key_id(self.account, self.remote, self.session_id) + '.json'))
        now = int(time.time() * 1000)
        if not document or any(document.get(k) != v for k, v in
                               [('version', 1), ('cipher', 'xtea-cbc-v1'), ('account', self.account),
                                ('remote', self.remote), ('initialSha256', self.initial_sha256), ('sessionId', self.session_id)]):
            return
        expires = document.get('expiresAtMs')
        if type(expires) is not int or not now < expires <= now + 7_260_000:
            return
        try:
            key = base64.b64decode(document.get('keyBase64', ''), validate=True)
        except (ValueError, TypeError):
            return
        if len(key) != 16:
            return
        decoded = [decrypt_packet(packet.payload, key, 1) for packet in self.probes]
        if not all(tea.valid_envelope(value) for value in decoded):
            return
        sequences = [(int.from_bytes(value[:5], 'little') >> 19) & 0x3fff for value in decoded]
        if len(set(sequences)) != 5 or not all(1 <= (b - a) & 0x3fff <= 256
                                             for a, b in zip(sequences, sequences[1:])):
            return
        self.key, self.expires_ms = key, expires
        self.fingerprint = hashlib.sha256(key).hexdigest()
        if self.journal is not None:
            self.journal.close()
            self.journal = None
        saved = self.pending
        self.pending = deque()
        self.spool_iterator = self._spooled()
        self.drain_iterator = itertools.chain(self.spool_iterator, iter(saved))
        self.waiting_path.unlink(missing_ok=True)

    def _spooled(self):
        if self.journal_path is None:
            return
        try:
            with self.journal_path.open(encoding='utf-8') as handle:
                for line in handle:
                    row = json.loads(line)
                    yield self.packet_type(up=bool(row['up']), local=self.local, remote=self.remote,
                                           payload=base64.b64decode(row['payload'], validate=True),
                                           timestamp_ms=int(row['timestamp_ms']), account=self.account)
        finally:
            self.journal_path.unlink(missing_ok=True)
            self.journal_path = None

    def _decode(self, packet):
        if not packet.payload or (packet.up and len(packet.payload) < 9):
            return None
        if packet.up:
            plain = packet.payload[8:]
        elif packet.payload and packet.payload[0] & 1:
            plain = packet.payload
        else:
            plain = decrypt_packet(packet.payload, self.key, 1)
        if not plain:
            return None
        control = bool(plain[0] & 1) and len(plain) in (17, 25, 35, 69)
        if control:
            return plain
        if not tea.valid_envelope(plain):
            self.rejected_packets += 1
            return None
        if not packet.up:
            sequence = (int.from_bytes(plain[:5], 'little') >> 19) & 0x3fff
            if self.last_sequence is not None:
                delta = (sequence - self.last_sequence) & 0x3fff
                if not (delta <= 256 or delta >= 0x4000 - 64):
                    self.rejected_packets += 1
                    return None
                if 0 < delta <= 256:
                    self.last_sequence = sequence
            else:
                self.last_sequence = sequence
        return plain

    def _drain(self):
        output = []
        for _ in range(DRAIN_BATCH):
            if self.drain_iterator is not None:
                try:
                    packet = next(self.drain_iterator)
                except StopIteration:
                    self.drain_iterator = None
                    if not self.arrivals:
                        break
                    packet = self.arrivals.popleft()
            elif self.arrivals:
                packet = self.arrivals.popleft()
            else:
                break
            plain = self._decode(packet)
            if plain is not None:
                output.append((packet, plain))
        return output

    def observe(self, packet):
        if (packet.account, packet.local, packet.remote) != (self.account, self.local, self.remote):
            return []
        now = int(time.time() * 1000)
        if not self.bound:
            if now - self.started_ms > MAX_WAIT_MS:
                raise RuntimeError('session key wait expired')
            self._hold(packet)
            self._bind()
            return self._drain() if self.bound else []
        if now >= self.expires_ms:
            raise RuntimeError('session key expired')
        if self.drain_iterator is not None or self.arrivals:
            if len(self.arrivals) >= MAX_HISTORY_PACKETS:
                raise RuntimeError('session replay queue limit')
            self.arrivals.append(packet)
            return self._drain()
        plain = self._decode(packet)
        return [(packet, plain)] if plain is not None else []

    def close(self):
        if self.journal is not None:
            self.journal.close()
            self.journal = None
        if self.drain_iterator is not None:
            if getattr(self, 'spool_iterator', None) is not None:
                self.spool_iterator.close()
            self.drain_iterator = None
        if self.journal_path is not None:
            self.journal_path.unlink(missing_ok=True)
            self.journal_path = None
        self.waiting_path.unlink(missing_ok=True)
        self.pending.clear()
        self.arrivals.clear()
