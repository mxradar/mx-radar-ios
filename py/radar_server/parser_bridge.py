"""In-process parser bridge for the Android node.

The server version spawns ``gateway_parser`` as a subprocess and talks JSONL
over stdin.  Android can neither spawn that binary nor rely on a writable
interpreter path, so this implementation imports the engine directly, feeds it
the same ``gateway-realtime/v4`` events, and runs its HTTP server on
127.0.0.1.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from . import diagnostics as diag
from .event_inbox import EventInbox

PUBLISH_INTERVAL = 0.1


def next_publish_tick(deadline: float, now: float) -> float:
    """Keep a 5 Hz clock; skip missed ticks instead of catching up in bursts."""
    return deadline + max(1, int((now - deadline) / PUBLISH_INTERVAL) + 1) * PUBLISH_INTERVAL


class LocalParserBridge:
    def __init__(self, output: str | Path, assets: str | Path, port: int = 17891):
        self._out = Path(output)
        self._assets = Path(assets)
        self._port = int(port)
        self._out.mkdir(parents=True, exist_ok=True)
        self._engine = None
        self._server = None
        self._flush_stop = threading.Event()
        self._flush_thread: threading.Thread | None = None
        self._publish_thread: threading.Thread | None = None
        self._publish_stop = threading.Event()
        self._publish_queue: queue.Queue = queue.Queue(maxsize=1)
        self._publish_lock = threading.Lock()
        self._published = 0
        self._publish_replaced = 0
        self._publish_interval = PUBLISH_INTERVAL
        self._slow_until = 0.0
        self._lock = threading.Lock()
        self._sequence = 0
        self._last_source_sequence = 0
        self._source_session_id = None
        self._parser_id = uuid.uuid4().hex
        self._decoded_sequence = 0
        self._decoded_session_id = None
        self._resume_session_id = None
        self._replay_target = 0
        self._replay_reported = True
        self._started_at = time.time()
        self._error: str | None = None
        self._ready = False
        self._last_state: bytes | None = None
        self._events = EventInbox(self._out / '.parser-events')
        self._dropped = 0
        self._accepted = 0
        self._decode_errors = 0
        self._last_diag = 0.0
        self._last_event = 0.0
        self._accept_max_ms = self._flush_max_ms = 0.0

    # ------------------------------------------------------------------ start
    def start(self) -> None:
        import gateway_parser as gp

        gp.ASSETS = self._assets
        gp.ASSET_STORE = gp.AssetStore(directory=str(self._assets))
        models = self._assets / "m3d"
        gp.MODEL_STORE = gp.AssetStore(directory=str(models))

        # Engine opens states.jsonl with mode "x"; a previous run must not
        # make the next launch fail.
        (self._out / "states.jsonl").unlink(missing_ok=True)

        self._engine = gp.Engine(self._out, cache_only=True, preserve_event_context=True)
        from .native_codec import status as codec_status
        diag.log('ParserCodec', f'backend={codec_status()["backend"]} loadError={codec_status()["error"]}')
        self._server = gp.start_http(self._engine, self._port)
        self._ready = True
        self._publish_thread = threading.Thread(
            target=self._publish_loop, name="state-file-publisher", daemon=True
        )
        self._publish_thread.start()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, name="local-parser-worker", daemon=True
        )
        self._flush_thread.start()
        diag.log("ParserDiag", f"engineReady=true httpPort={self._port}")

    def _flush_loop(self) -> None:
        engine = self._engine
        next_flush = time.monotonic()
        last_snapshot = 0.0
        while not self._flush_stop.is_set() and engine is not None:
            try:
                event = self._events.get(timeout=max(0.0, min(0.1, next_flush - time.monotonic())))
            except queue.Empty:
                event = None
            if event is not None:
                try:
                    accept_started = time.monotonic()
                    engine.accept(event)
                    accept_cost = time.monotonic()-accept_started
                    self._accept_max_ms = max(self._accept_max_ms, accept_cost*1000)
                    self._publication_load(decode_seconds=accept_cost, queue_depth=self._events.qsize())
                    self._accepted += 1
                    self._decoded_sequence = event['sequence']
                    if event['type'] == 'session_started':
                        self._decoded_session_id = event['data'].get('sessionId')
                    self._last_event = time.monotonic()
                except Exception as exc:  # keep the tunnel alive on decoder bugs
                    self._error = f"{type(exc).__name__}: {exc}"
                    self._decode_errors += 1
                    diag.error("ParserError", f"accept type={event.get('type')} seq={event.get('sequence')}", exc)
            now = time.monotonic()
            # Decode complete input batches before making another expensive
            # snapshot. Under backlog, snapshot at most once/second so input
            # processing can catch up; the publisher still holds latest only.
            if now >= next_flush and (self._events.qsize() < 32 or now-last_snapshot >= 0.2):
                if engine.dirty:
                    try:
                        decode_started = time.monotonic()
                        engine.flush()
                        engine.dirty = False
                        self._last_state = engine.api_state("")
                        if not self.replaying():
                            self._queue_state(self._last_state)
                        last_snapshot = time.monotonic()
                        self._flush_max_ms = max(self._flush_max_ms, (last_snapshot-decode_started)*1000)
                        self._publication_load(decode_seconds=last_snapshot-decode_started,
                                               queue_depth=self._events.qsize())
                    except Exception as exc:
                        self._error = f"{type(exc).__name__}: {exc}"
                        self._decode_errors += 1
                        diag.error("ParserError", "flush", exc)
                next_flush = next_publish_tick(next_flush, time.monotonic())
            elif now >= next_flush:
                next_flush = next_publish_tick(next_flush, now)
            if not self._replay_reported and not self.replaying():
                self._replay_reported = True
                engine.dirty = True
                diag.log('ParserReplay', f'caughtUp=true decoded={self._decoded_sequence} target={self._replay_target}')
            if now - self._last_diag >= 10:
                self._last_diag = now
                try:
                    state = json.loads(self._last_state) if self._last_state else {}
                    age = now - self._last_event if self._last_event else -1
                    diag.log("ParserDiag", f"events={self._sequence} accepted={self._accepted} "
                             f"queue={self._events.qsize()} drops={self._dropped} errors={self._decode_errors} "
                             f"lastEventAge={age:.1f}s status={state.get('status', 'none')} "
                             f"self={state.get('self') is not None} entities={len(state.get('entities') or [])} "
                             f"seq={(state.get('cursor') or {}).get('seq', 0)} "
                             f"plaintext={engine.stats.get('plaintextPackets', 0)} "
                             f"eventGaps={engine.stats.get('eventGaps', 0)} "
                             f"published={self._published} publishReplaced={self._publish_replaced}")
                    diag.log('PublishBudget', f'targetHz={1/self._publish_interval:.2f} backlog={self._events.qsize()}'
                             f' acceptMaxMs={self._accept_max_ms:.1f} flushMaxMs={self._flush_max_ms:.1f}')
                    self._accept_max_ms = self._flush_max_ms = 0.0
                    if self.replaying():
                        diag.log('ParserReplay', f'catchingUp=true decoded={self._decoded_sequence} target={self._replay_target}')
                    inbox = self._events.status()
                    diag.log("ParserInbox", f"queued={self._events.qsize()} memory={inbox['memory']} "
                             f"diskEvents={inbox['diskEvents']} diskBytes={inbox['diskBytes']} "
                             f"spooled={inbox['spooled']} highWater={inbox['highWater']}")
                except Exception as exc:
                    diag.error("ParserError", "diagnostic snapshot", exc)

    def _queue_state(self, body: bytes | None) -> None:
        if not body:
            return
        with self._publish_lock:
            try:
                self._publish_queue.put_nowait(body)
            except queue.Full:
                try:
                    self._publish_queue.get_nowait()
                    self._publish_replaced += 1
                except queue.Empty:
                    pass
                self._publish_queue.put_nowait(body)

    def _publish_loop(self) -> None:
        # Slow storage affects this single-slot publisher, never packet relay
        # or engine.accept. Old snapshots are replaced, not accumulated.
        last_write = 0.0
        next_write = 0.0
        last_interval = self._publish_interval
        while not self._publish_stop.is_set() or not self._publish_queue.empty():
            try:
                body = self._publish_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            # Coalesce while waiting: under sustained load 1.67 Hz is a safe
            # fallback, rather than forcing every phone to maintain five writes.
            while not self._publish_stop.is_set():
                interval = self._publish_interval
                if interval != last_interval:
                    next_write = last_write+interval
                    last_interval = interval
                remaining = next_write-time.monotonic()
                if remaining <= 0:
                    break
                self._publish_stop.wait(min(.1, remaining))
                try:
                    body = self._publish_queue.get_nowait()
                    self._publish_replaced += 1
                except queue.Empty:
                    pass
            began = time.monotonic()
            if self._write_state(body):
                self._published += 1
                last_write = time.monotonic()
                self._publication_load(write_seconds=last_write-began)
                interval = self._publish_interval
                if next_write == 0.0 or interval != last_interval:
                    next_write = last_write+interval
                    last_interval = interval
                else:
                    # A deadline clock avoids accumulating disk-write and
                    # timer rounding costs on every publication.
                    next_write += max(1, int((last_write-next_write)/interval)+1)*interval

    def _publication_load(self, decode_seconds=0.0, queue_depth=0, write_seconds=0.0):
        now = time.monotonic()
        if decode_seconds > .12 or queue_depth >= 32 or write_seconds > .05:
            self._slow_until = now+10.0
        self._publish_interval = .1 if now < self._slow_until else PUBLISH_INTERVAL

    def _write_state(self, body: bytes | None) -> bool:
        """Publish the latest state to a file so the app never needs the HTTP
        server, which shares the GIL with the decoder and stalls in battle."""
        if not body:
            return False
        try:
            target = self._out / "state.json"
            tmp = self._out / "state.json.tmp"
            with open(tmp, "wb") as handle:
                handle.write(body)
            os.replace(tmp, target)
            return True
        except Exception as exc:
            diag.error("ParserError", "write state.json", exc)
            return False

    # ------------------------------------------------------------------- emit
    def replay_hello(self):
        from .session_replay import PROTOCOL
        with self._lock:
            return dict(protocol=PROTOCOL, parserId=self._parser_id,
                        sessionId=self._source_session_id, lastSequence=self._last_source_sequence)

    def begin_replay(self, session_id, through):
        if not isinstance(session_id, str) or not session_id or type(through) is not int or through < 0:
            raise ValueError('Invalid replay request')
        self._resume_session_id = session_id
        self._replay_target = through
        self._replay_reported = False
        # Never keep displaying a previous incarnation's frozen position while
        # rebuilding its channels. Publication resumes at the captured cursor.
        if self.replaying():
            self._queue_state(json.dumps(dict(schema='gateway-state/v5', status='recovering',
                                              live_active=False, self=None, entities=[], names=[])).encode())

    def replaying(self):
        return self._resume_session_id is not None and (
            self._decoded_session_id != self._resume_session_id or self._decoded_sequence < self._replay_target)

    def emit(self, kind: str, data: dict, source_sequence: int | None = None) -> None:
        engine = self._engine
        if engine is None:
            return
        with self._lock:
            session_id = data.get('sessionId') if kind == 'session_started' else None
            new_session = session_id is not None and session_id != self._source_session_id
            if source_sequence is not None:
                if type(source_sequence) is not int or source_sequence < 1:
                    raise ValueError('Invalid source event sequence')
                if not new_session and source_sequence <= self._last_source_sequence:
                    return  # retry after a socket failure: already accepted
                sequence = source_sequence
            else:
                sequence = self._sequence + 1
            event = {
                "schema": "gateway-realtime/v4",
                "sequence": sequence,
                "timestampMs": int(time.time() * 1000),
                "type": kind,
                "data": data,
            }
            # Assignment and enqueue share one lock: concurrent producers must
            # never invert sequence order. Overflow is spooled, never replaced.
            self._events.put(event)
            self._sequence = sequence
            if source_sequence is not None:
                self._last_source_sequence = source_sequence
            if new_session:
                self._source_session_id = session_id

    def start_session(self, phone: str = "", remote: str = "") -> None:
        self.emit(
            "session_started",
            {
                "schema": "gateway-session/v5",
                "mode": "local-node",
                "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "targetAddress": phone,
                "gateway": remote,
                "output": f"http://127.0.0.1:{self._port}",
                "framing": "in-process",
                "payloadEncoding": "base64",
                "stateSchema": "gateway-state/v5",
            },
        )

    # ----------------------------------------------------------------- status
    @property
    def output(self) -> str:
        return str(self._out)

    @property
    def port(self) -> int:
        return self._port

    def latest_state(self) -> bytes | None:
        return self._last_state

    def status(self) -> dict:
        engine = self._engine
        return {
            "configured": True,
            "running": engine is not None and not self._flush_stop.is_set(),
            "ready": self._ready,
            "port": self._port,
            "events": self._sequence,
            "eventQueue": self._events.qsize(),
            "eventDrops": self._dropped,
            "uptimeSeconds": int(time.time() - self._started_at),
            "error": self._error,
            "output": str(self._out),
        }

    # ------------------------------------------------------------------- stop
    def stop(self) -> None:
        self._flush_stop.set()
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=2)
        self._publish_stop.set()
        if self._publish_thread is not None:
            self._publish_thread.join(timeout=2)
        self._events.close()
        engine, server = self._engine, self._server
        self._engine = None
        self._server = None
        self._ready = False
        if engine is not None:
            try:
                engine.close()
            except Exception:
                pass
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass

    def status_json(self) -> str:
        return json.dumps(self.status(), ensure_ascii=False)


# socks5.py only uses this name as a type annotation.
ParserBridge = LocalParserBridge
