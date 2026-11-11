"""Relay-process bridge: ships gateway events to the parser process over TCP.

The relay and the parser must not share one interpreter on a phone: the GIL
lets a decoding burst delay game packets.  This bridge only serialises events
and writes them to a loopback socket owned by the :parser process.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
import uuid
from pathlib import Path
from . import diagnostics as diag
from .event_inbox import EventInbox
from .session_replay import SessionReplay, PROTOCOL


class TcpParserBridge:
    def __init__(self, host: str = "127.0.0.1", port: int = 17892,
                 output: str | Path = "."):
        self.host, self.port = host, int(port)
        self._out = Path(output)
        self._out.mkdir(parents=True, exist_ok=True)
        self._queue = EventInbox(self._out / '.relay-events')
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sequence = 0
        self._session_id = uuid.uuid4().hex
        self._replay = SessionReplay(self._out / '.session-replay', self._session_id)
        self._sock: socket.socket | None = None
        self._connected = False
        self._error: str | None = None
        self._dropped = 0
        self._sent = 0
        self._failures = 0
        self._thread: threading.Thread | None = None
        self._record_thread: threading.Thread | None = None
        self._record_error = None
        self._restarts = 0
        self._parser_id = None
        self._replayed = 0

    @property
    def output(self) -> str:
        return str(self._out)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._record_thread = threading.Thread(target=self._record, name='session-replay-writer', daemon=True)
        self._record_thread.start()
        self._thread = threading.Thread(target=self._run, name="tcp-parser-bridge", daemon=True)
        self._thread.start()

    def _record(self):
        while not self._stop.is_set():
            try:
                event = self._queue.get(timeout=.5)
                if event is None:
                    return
                self._replay.append(event)
            except queue.Empty:
                continue
            except Exception as exc:
                self._record_error = f'{type(exc).__name__}: {exc}'
                self._error = self._record_error
                self._dropped += 1+self._queue.qsize()
                self._stop.set()
                diag.error('EventBridge', 'replay recording stopped; game forwarding continues', exc)
                return

    @staticmethod
    def _line(sock, limit=4096):
        result = bytearray()
        while len(result) < limit:
            block = sock.recv(min(4096, limit-len(result)))
            if not block:
                raise ConnectionError('Parser closed the event connection')
            result.extend(block)
            if result.endswith(b'\n'):
                return bytes(result)
        raise ConnectionError('Oversized parser reply')

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.host, self.port), timeout=1.0)
                sock.settimeout(10.0)
                self._sock = sock
                hello = json.loads(self._line(sock))
                if hello.get('protocol') != PROTOCOL:
                    raise ConnectionError('Parser replay protocol mismatch')
                parser_id = hello.get('parserId')
                if not isinstance(parser_id, str) or not parser_id:
                    raise ConnectionError('Missing parser incarnation')
                resume = hello.get('lastSequence', 0) if hello.get('sessionId') == self._session_id else 0
                if type(resume) is not int or not 0 <= resume <= self._replay.status()['events']:
                    raise ConnectionError('Invalid parser replay cursor')
                if self._parser_id is not None and parser_id != self._parser_id:
                    self._restarts += 1
                self._parser_id = parser_id
                with self._lock:
                    through = self._sequence
                sock.sendall((json.dumps({'op':'resume', 'sessionId':self._session_id,
                                         'through':through})+'\n').encode())
                self._sent = resume
                self._connected = True
                self._error = None
                diag.log("EventBridge", "parser socket connected", key="bridge-connected")
                diag.log('ParserReplay', f'parser={parser_id} resume={resume} through={through} restarts={self._restarts}')
                while not self._stop.is_set():
                    sequence = self._sent+1
                    try:
                        line = self._replay.read(sequence)
                    except queue.Empty:
                        # Probe the peer while idle too; a dead parser must be
                        # replayed even when the game temporarily sends nothing.
                        sock.sendall(b'{"op":"ping"}\n')
                        if self._line(sock, 32) != b'pong\n':
                            raise ConnectionError('Invalid parser keepalive')
                        continue
                    sock.sendall(line+b'\n')
                    ack = self._line(sock, 32)
                    if ack != (str(sequence)+'\n').encode('ascii'):
                        raise ConnectionError('Invalid parser admission ACK')
                    self._sent = sequence
                    if self._restarts and sequence <= through:
                        self._replayed += 1
            except queue.Empty:
                continue
            except Exception as exc:
                self._failures += 1
                self._error = f"{type(exc).__name__}: {exc}"
                diag.error("EventBridge", "parser socket send/connect", exc)
            finally:
                self._connected = False
                try:
                    if self._sock is not None:
                        self._sock.close()
                except OSError:
                    pass
                self._sock = None
            self._stop.wait(0.5)

    def _enqueue(self, event: dict) -> None:
        if self._stop.is_set():
            self._dropped += 1
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._record_error = self._error = 'Relay event spool full; radar input stopped, game forwarding continues'
            self._dropped += 1
            self._stop.set()
            diag.log('EventBridge', self._error, warning=True)

    def emit(self, kind: str, data: dict) -> None:
        with self._lock:
            self._sequence += 1
            event = {
                "schema": "gateway-realtime/v4",
                "sequence": self._sequence,
                "timestampMs": int(time.time() * 1000),
                "type": kind,
                "data": data,
            }
            self._enqueue(event)

    def start_session(self, phone: str = "", remote: str = "") -> None:
        self.emit("session_started", {
            "schema": "gateway-session/v5",
            "mode": "local-node",
            "sessionId": self._session_id,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "targetAddress": phone,
            "gateway": remote,
            "framing": "tcp-jsonl",
            "payloadEncoding": "base64",
            "stateSchema": "gateway-state/v5",
        })

    def status(self) -> dict:
        return {
            "configured": True,
            "running": self._connected,
            "ready": self._connected,
            "port": self.port,
            "events": self._sequence,
            "eventQueue": self._queue.qsize(),
            "eventDrops": self._dropped,
            "eventSent": self._sent,
            "failures": self._failures,
            "inbox": self._queue.status(),
            "journal": self._replay.status(),
            "parserRestarts": self._restarts,
            "replayed": self._replayed,
            "recordingError": self._record_error,
            "error": self._error,
            "output": str(self._out),
        }

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self._sock.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._record_thread is not None:
            self._record_thread.join(timeout=2)
        self._queue.close()
        self._replay.close()
