"""SOCKS5 server with username/password auth, CONNECT (TGCP MITM) and UDP ASSOCIATE."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import ipaddress
import socket
import struct
import threading
import time
from pathlib import Path

from .config import Config
from .db import Database
from .keybroker import KeyBroker, UdpPacket
from .broker_worker import BrokerWorker
from .parser_bridge import ParserBridge
from .tgcp import TgcpError, TgcpSession, sniff
from .seasonrelay import SeasonRelay
from .notice_worker import NoticeWorker
from . import diagnostics as diag
from .seasonbridge import is_initial

_TCP_KEEPALIVE = os.environ.get("RADAR_TCP_KEEPALIVE", "1") != "0"
_TGCP_STATS = os.environ.get("RADAR_TGCP_STATS", "0") == "1"
# Diagnostic switch: let 65010 through untouched instead of MITM.
FORCE_PASSTHROUGH_65010 = False
_UDP_UNUSED_SECONDS = 30.0
_UDP_IDLE_SECONDS = 120.0
_UDP_PRESSURE_IDLE_SECONDS = 30.0


def _write_json(path: str, payload: dict) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _tune_tcp(sock) -> None:
    if sock is None or not _TCP_KEEPALIVE:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 20),
                            ("TCP_KEEPCNT", 3), ("TCP_NODELAY", 1)):
            opt = getattr(socket, name, None)
            if opt is not None:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
    except OSError:
        pass


class Socks5Server:
    def __init__(self, cfg: Config, db: Database, bridge: ParserBridge):
        self.cfg = cfg
        self.db = db
        self.bridge = bridge
        data = Path(bridge.output).parent
        self.broker = KeyBroker(
            bridge.emit,
            key_dir=data / "keys",
            pending_dir=data / "pending",
            max_flows=cfg.max_udp_flows,
            max_candidates=cfg.max_bank_candidates,
            capture_raw_udp=cfg.capture_raw_udp,
        )
        self.broker_worker = BrokerWorker(self.broker)
        self.notice_worker = NoticeWorker()
        self._connection_id = 0
        # asyncio keeps only weak references to tasks. An idle stream and its
        # read future can otherwise form an unreachable cycle and be collected.
        self._connections: set[asyncio.Task] = set()
        self._udp_ports: set[int] = set()
        self._udp_leases: dict[int, tuple] = {}
        self._udp_port_start = cfg.udp_port_start
        self._udp_port_end = cfg.udp_port_end
        self._udp_next_port = self._udp_port_start
        self._auth_limit = asyncio.Semaphore(4)
        self.season = SeasonRelay(bridge.emit)
        self._udp_relays: dict[str, UdpRelay] = {}
        self.udp_total = dict(up=0, down=0, empty=0, empty_up=0, empty_down=0,
                              sent_up=0, sent_down=0, transform_err=0, err=0, max_ms=0.0)
        self.udp_interval = dict(self.udp_total)
        self.udp_last_err = ""
        self.udp_last_dump = time.monotonic()
        self.loop_lag_ms = 0.0
        self.notice_ms = 0.0
        self._notice_tasks: set[asyncio.Task] = set()

    def _apply_credential(self, credential, username: str) -> None:
        try:
            before = {peer for acct, peer in self.season.bridge.peers if acct == username}
            self.season.bridge.apply_notice(credential, username)
            after = {peer for acct, peer in self.season.bridge.peers if acct == username}
            added = sorted(after - before)
            if added:
                diag.log("Notice", f"account={username} credentialInstalled=true added={len(added)} "
                         f"peers={len(after)} endpoints={','.join(added[:8])}")
                self.bridge.emit('season_peers', {
                    'count': len(after), 'added': added[:16], 'account': username,
                })
        except Exception as exc:
            diag.error("Notice", "install credential", exc)

    def _apply_late(self, task: asyncio.Task, username: str, started: float) -> None:
        try:
            credential = task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            diag.error("Notice", "credential scan", exc)
            return
        diag.log("Notice", f"account={username} scanComplete=true matched={credential is not None} "
                 f"elapsed={(time.monotonic() - started) * 1000:.1f}ms",
                 warning=credential is None,
                 key=f'notice-negative:{username}' if credential is None else None)
        if credential is not None:
            self._apply_credential(credential, username)

    def pending_notices(self, account: str):
        return {task for task in self._notice_tasks
                if not task.cancelled() and getattr(task, "radar_account", None) == account}

    async def wait_notice(self, account: str, remote: str, timeout: float = 0.15) -> None:
        """Hold only an initial UDP hello while this account's scans finish.

        A late credential cannot retroactively take over an established flow.
        Ordinary UDP and other accounts never wait; a deadline preserves play
        if credentials are absent or scanning fails.
        """
        deadline = time.monotonic() + timeout
        while True:
            tasks = self.pending_notices(account)
            remaining = deadline - time.monotonic()
            if not tasks or remaining <= 0:
                return
            done, _ = await asyncio.wait(tasks, timeout=remaining,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                diag.log("Handshake", f"account={account} remote={remote} credentialWaitExpired=true",
                         warning=True, key=f"credential-wait:{account}:{remote}")
                return
            for task in done:
                if not task.cancelled() and task.exception() is None:
                    credential = task.result()
                    if credential is not None:
                        self._apply_credential(credential, account)

    def note_packet(self, up: bool, handled: bool, actions, elapsed_ms: float) -> None:
        key = "up" if up else "down"
        self.udp_total[key] += 1
        self.udp_interval[key] += 1
        if handled and not actions:
            self.udp_total["empty"] += 1
            self.udp_interval["empty"] += 1
            self.udp_total[f"empty_{key}"] += 1
            self.udp_interval[f"empty_{key}"] += 1
        if elapsed_ms > self.udp_total["max_ms"]:
            self.udp_total["max_ms"] = elapsed_ms
        if elapsed_ms > self.udp_interval["max_ms"]:
            self.udp_interval["max_ms"] = elapsed_ms

    def note_error(self, message: str) -> None:
        self.udp_total["err"] += 1
        self.udp_interval["err"] += 1
        self.udp_last_err = message
        diag.log("RelayError", message, warning=True, key="relay-error")

    def note_sent(self, up: bool) -> None:
        # Accepted by the local UDP transport; this is not a remote delivery ACK.
        key = "sent_up" if up else "sent_down"
        self.udp_total[key] += 1
        self.udp_interval[key] += 1

    def dump_udp_stats(self) -> None:
        now = time.monotonic()
        if now - self.udp_last_dump < 5.0:
            return
        self.udp_last_dump = now
        interval = dict(self.udp_interval)
        total = dict(self.udp_total)
        lag = self.loop_lag_ms
        self.loop_lag_ms = 0.0
        for key in self.udp_interval:
            self.udp_interval[key] = 0.0 if key == "max_ms" else 0
        path = os.environ.get("RADAR_RELAY_STATS")
        if not path:
            return
        payload = {
            "ts": time.time(),
            "interval": interval,
            "total": total,
            "loop_lag_ms": lag,
            "notice_ms": self.notice_ms,
            "last_err": self.udp_last_err,
            "event_bridge": self.bridge.status(),
            "diagnostics": diag.status(),
            "notice": self.notice_worker.status(),
            "broker": {"queue": self.broker_worker.queue.qsize(),
                       "drops": self.broker_worker.dropped, "errors": self.broker_worker.failures},
        }
        threading.Thread(target=_write_json, args=(path, payload), daemon=True).start()

    async def _lag_probe(self) -> None:
        while True:
            start = time.monotonic()
            await asyncio.sleep(0.25)
            drift = (time.monotonic() - start - 0.25) * 1000.0
            if drift > self.loop_lag_ms:
                self.loop_lag_ms = drift

    async def run(self) -> None:
        self.bridge.start_session()
        server = await asyncio.start_server(self._accept, self.cfg.socks_host, self.cfg.socks_port)
        sockets = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
        print(f"[socks5] listening on {sockets}", flush=True)
        poll_task = asyncio.create_task(self._poll_udp())
        lag_task = asyncio.create_task(self._lag_probe())
        try:
            async with server:
                await server.serve_forever()
        finally:
            poll_task.cancel()
            lag_task.cancel()
            connections = tuple(self._connections)
            for task in connections:
                task.cancel()
            await asyncio.gather(poll_task, lag_task, *connections, return_exceptions=True)
            self.broker_worker.close()
            self.notice_worker.close()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._connections.add(task)
        task.add_done_callback(self._connection_done)

    def _connection_done(self, task: asyncio.Task) -> None:
        self._connections.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                diag.error("TCP", "connection task", exc)

    async def _poll_udp(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            self.dump_udp_stats()
            try:
                events = self.season.poll_all()
            except Exception as exc:
                diag.error("Handshake", "poll", exc)
                continue
            for account, local, remote, actions in events:
                relay = self._udp_relays.get((account, local))
                if relay is None:
                    continue
                host, _, port = remote.rpartition(":")
                try:
                    target = (host, int(port))
                except ValueError:
                    continue
                for up, wire in actions:
                    relay.send_action(up, target, wire)

    # --------------------------------------------------------------- session
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        try:
            _tune_tcp(writer.get_extra_info("socket"))
            username = await asyncio.wait_for(self._handshake(reader, writer), timeout=15)
            if username is None:
                return
            command, host, port = await asyncio.wait_for(self._read_request(reader), timeout=15)
            if command == 1:
                await self._connect(reader, writer, username, host, port)
            elif command == 3:
                await self._udp_associate(reader, writer, username, peer)
            else:
                await self._reply(writer, 7)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
            pass
        except Exception as exc:
            print(f"[socks5] {peer} error: {exc}", flush=True)
        finally:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=3)
            except Exception:
                pass

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str | None:
        version, count = await reader.readexactly(2)
        if version != 5:
            return None
        methods = await reader.readexactly(count)
        if 2 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return None
        writer.write(b"\x05\x02")
        await writer.drain()
        auth_version, ulen = await reader.readexactly(2)
        if auth_version != 1:
            return None
        username = (await reader.readexactly(ulen)).decode("utf-8", "replace")
        (plen,) = await reader.readexactly(1)
        password = (await reader.readexactly(plen)).decode("utf-8", "replace")
        # PBKDF2 and SQLite must never run on the shared UDP/TCP event loop.
        # Bound the worker count so reconnect bursts cannot consume every core.
        async with self._auth_limit:
            user = await asyncio.to_thread(self.db.authenticate, username, password)
        if user is None or user["expired"] or not user["active"]:
            writer.write(b"\x01\x01")
            await writer.drain()
            return None
        writer.write(b"\x01\x00")
        await writer.drain()
        return username

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[int, str, int]:
        version, command, _, atyp = await reader.readexactly(4)
        if version != 5:
            raise ConnectionError("bad socks version")
        host = await self._read_address(reader, atyp)
        (port,) = struct.unpack("!H", await reader.readexactly(2))
        return command, host, port

    @staticmethod
    async def _read_address(reader: asyncio.StreamReader, atyp: int) -> str:
        if atyp == 1:
            return socket.inet_ntoa(await reader.readexactly(4))
        if atyp == 3:
            (length,) = await reader.readexactly(1)
            return (await reader.readexactly(length)).decode("ascii", "replace")
        if atyp == 4:
            return socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        raise ConnectionError("unsupported address type")

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, code: int, host: str = "0.0.0.0", port: int = 0) -> None:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = ipaddress.ip_address("0.0.0.0")
        if address.version == 4:
            body = b"\x01" + address.packed
        else:
            body = b"\x04" + address.packed
        writer.write(bytes([5, code, 0]) + body + struct.pack("!H", port))
        await writer.drain()

    # ---------------------------------------------------------------- connect
    async def _connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        username: str,
        host: str,
        port: int,
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10
            )
        except Exception as exc:
            print(f"[socks] upstream FAIL {host}:{port} {type(exc).__name__}: {exc}", flush=True)
            await self._reply(writer, 5)
            return
        _tune_tcp(upstream_writer.get_extra_info("socket"))
        await self._reply(writer, 0)
        self._connection_id += 1
        connection_id = self._connection_id
        def passthrough_reason(reason: str, payload_bytes: int) -> None:
            if port == 65010:
                self.bridge.emit("tcp_passthrough", {
                    "connection": connection_id, "account": username,
                    "reason": reason, "port": port, "payloadBytes": payload_bytes,
                })

        hello_timeout = 5.0 if port == 65010 else 1.0
        try:
            initial = b""
            try:
                initial = await asyncio.wait_for(reader.read(65536), timeout=hello_timeout)
            except asyncio.TimeoutError:
                initial = b""
            if not initial:
                passthrough_reason("initial_timeout_or_eof", 0)
                await self._passthrough(reader, writer, upstream_reader, upstream_writer, b"")
                return
            prefix = initial
            while True:
                verdict = sniff(prefix)
                if port == 65010 and FORCE_PASSTHROUGH_65010:
                    await self._passthrough(reader, writer, upstream_reader, upstream_writer, prefix)
                    return
                if verdict == 1:
                    passthrough_reason("not_client_hello", len(prefix))
                    await self._passthrough(reader, writer, upstream_reader, upstream_writer, prefix)
                    return
                if verdict == 2:
                    await self._mitm(
                        reader, writer, upstream_reader, upstream_writer, prefix, connection_id, username
                    )
                    return
                if len(prefix) > 1024 * 1024:
                    passthrough_reason("prefix_limit", len(prefix))
                    await self._passthrough(reader, writer, upstream_reader, upstream_writer, prefix)
                    return
                try:
                    more = await asyncio.wait_for(reader.read(65536), timeout=hello_timeout)
                except asyncio.TimeoutError:
                    passthrough_reason("incomplete_hello_timeout", len(prefix))
                    await self._passthrough(reader, writer, upstream_reader, upstream_writer, prefix)
                    return
                if not more:
                    passthrough_reason("incomplete_hello_eof", len(prefix))
                    await self._passthrough(reader, writer, upstream_reader, upstream_writer, prefix)
                    return
                prefix += more
        finally:
            try:
                upstream_writer.close()
                await asyncio.wait_for(upstream_writer.wait_closed(), timeout=3)
            except Exception:
                pass

    async def _passthrough(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        initial: bytes,
    ) -> None:
        if initial:
            upstream_writer.write(initial)
            await upstream_writer.drain()

        async def pump(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
            while True:
                chunk = await source.read(65536)
                if not chunk:
                    break
                destination.write(chunk)
                await destination.drain()
            try:
                destination.write_eof()
            except Exception:
                pass

        tasks = [asyncio.create_task(pump(reader, upstream_writer)),
                 asyncio.create_task(pump(upstream_reader, writer))]
        try:
            # A reset on one direction must also release the other direction.
            # Normal EOF still permits TCP half-close and drains the reply.
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _capture_wire_key(self, session: TgcpSession, connection_id: int, username: str) -> None:
        """Operator-scoped, short-lived diagnostics for one account; disabled by default."""
        try:
            diagnostics = Path(self.bridge.output).parent / "diagnostics"
            marker = diagnostics / "wire-key-capture.json"
            if not marker.is_file():
                return
            lease = json.loads(marker.read_text(encoding="utf-8"))
            now = time.time()
            digest = hashlib.sha256(username.encode("utf-8")).hexdigest()
            if lease.get("accountSha256") != digest:
                return
            deadline = float(lease.get("expiresAt", 0))
            if not now < deadline <= now + 600:
                return
            key = session._client_key
            if not isinstance(key, bytes) or len(key) != 16:
                return
            target = diagnostics / "wire-keys.jsonl"
            if target.exists() and target.stat().st_size >= 65536:
                return
            record = {
                "timestampMs": int(now * 1000), "connection": connection_id,
                "accountSha256": digest, "purpose": "bounded-current-session-replay",
                "clientWireKeyBase64": base64.b64encode(key).decode("ascii"),
            }
            public = getattr(session, "_public_x", None)
            if isinstance(public, int) and 0 < public < (1 << 512):
                record["clientFacingPublicSha256"] = hashlib.sha256(public.to_bytes(64, "big")).hexdigest()
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(target, flags, 0o600)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except Exception as exc:
            # The entire optional diagnostic boundary, including stat and logging,
            # must fail open without changing the live transport.
            try:
                self.bridge.emit("tcp_key_capture_error", {
                    "connection": connection_id, "account": username,
                    "reason": "private_diagnostic_write_failed",
                    "errorType": type(exc).__name__,
                })
            except Exception:
                pass

    async def _mitm(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        prefix: bytes,
        connection_id: int,
        username: str,
    ) -> None:
        session = TgcpSession()
        lock = asyncio.Lock()
        established = False
        cmd_stats: dict[str, int] = {}
        last_cmd_dump = time.monotonic()
        notice_hashes: dict[bytes, None] = {}

        async def feed(up: bool, data: bytes) -> None:
            nonlocal established, last_cmd_dump
            async with lock:
                # Decrypt + bank scan + re-encrypt is pure Python and used to run
                # inline here, blocking the UDP/TCP loop for 100-270 ms per large
                # battle frame. Keep the ordering by awaiting it under the lock.
                to_server, to_client, observations = await asyncio.to_thread(
                    session.feed, up, data)
                # Install advertised keys before the client receives the
                # notice and can initiate its UDP handshake. Expensive parsing
                # runs in bounded workers; session mutation stays on this loop.
                scans = set()
                for observation in observations:
                    # The bank must reach the client before it starts its UDP
                    # handshake, but a full 16403 parse can take hundreds of ms
                    # (65 LZ4 attempts + protobuf walk) and used to stall map
                    # loading. Wait at most 50 ms, then let the game frame go;
                    # the parse keeps running and installs the key when done.
                    if (not observation.up and observation.command == 16403
                            and len(observation.plain) >= 150):
                        digest = hashlib.sha256(observation.plain).digest()
                        if digest not in notice_hashes:
                            notice_hashes[digest] = None
                            if len(notice_hashes) > 256:
                                del notice_hashes[next(iter(notice_hashes))]
                            started = time.monotonic()
                            diag.log("Notice", f"account={username} connection={connection_id} "
                                     f"command=16403 bytes={len(observation.plain)} scanStart=true",
                                     key=f'notice-start:{username}')
                            task = asyncio.create_task(
                                self.notice_worker.parse(observation.command, observation.plain))
                            task.radar_account = username
                            scans.add(task)
                            self._notice_tasks.add(task)
                            task.add_done_callback(self._notice_tasks.discard)
                            task.add_done_callback(
                                lambda t, u=username, s=started: self._apply_late(t, u, s))
                if scans:
                    # One budget per received batch, renewed for every batch.
                    # An ordinary first notice must not consume a connection's
                    # only wait. Late scans are guarded at the UDP initial too.
                    started = time.monotonic()
                    done, pending = await asyncio.wait(scans, timeout=0.05)
                    for task in done:
                        if not task.cancelled() and task.exception() is None:
                            credential = task.result()
                            if credential is not None:
                                self._apply_credential(credential, username)
                    self.notice_ms = (time.monotonic() - started) * 1000.0
                    if pending:
                        diag.log("Notice", f"account={username} batchWaitLimit=50ms pending={len(pending)} "
                                 "udpInitialGuard=true", key=f"notice-pending:{username}")
                if to_server:
                    upstream_writer.write(to_server)
                    await upstream_writer.drain()
                if to_client:
                    writer.write(to_client)
                    await writer.drain()
                for observation in observations:
                    self.broker_worker.add(connection_id, observation, username)
                    if observation.up or observation.command!=16403:
                        self.season.observe_notice(observation, username)
                    if _TGCP_STATS:
                        key = f"{'up' if observation.up else 'down'}:{observation.command}"
                        cmd_stats[key] = cmd_stats.get(key, 0) + 1
                if _TGCP_STATS and time.monotonic() - last_cmd_dump >= 5 and cmd_stats:
                    self.bridge.emit(
                        "tgcp_commands",
                        {"connection": connection_id, "account": username,
                         "commands": dict(cmd_stats)},
                    )
                    cmd_stats.clear()
                    last_cmd_dump = time.monotonic()
                if session.state == 2 and not established:
                    established = True
                    diag.log("TCP", f"account={username} connection={connection_id} tgcpEstablished=true")
                    self._capture_wire_key(session, connection_id, username)
                    self.bridge.emit(
                        "tgcp_established",
                        {"connection": connection_id, "account": username, "frames": session.frames},
                    )

        try:
            await feed(True, prefix)
            self.bridge.emit(
                "tcp_classified",
                {"connection": connection_id, "mode": "tgcp", "account": username},
            )

            async def pump(source: asyncio.StreamReader, up: bool) -> None:
                while True:
                    chunk = await source.read(65536)
                    if not chunk:
                        break
                    await feed(up, chunk)
                async with lock:
                    try:
                        session.finish(up)
                    except TgcpError:
                        pass

            results = await asyncio.gather(
                pump(reader, True),
                pump(upstream_reader, False),
                return_exceptions=True,
            )
            for direction, result in zip(("up", "down"), results):
                if isinstance(result, Exception):
                    diag.error("TCP", f"account={username} connection={connection_id} pump={direction}", result)
        except (TgcpError, ConnectionError, OSError) as exc:
            diag.error("TCP", f"account={username} connection={connection_id} mitm", exc)
            print(f"[mitm] error {type(exc).__name__}: {exc}", flush=True)
            self.bridge.emit(
                "tcp_error",
                {"connection": connection_id, "mode": "tgcp", "error": str(exc), "account": username},
            )
        finally:
            session.dispose()

    # -------------------------------------------------------------------- udp
    def _reclaim_idle_udp(self, now: float) -> int:
        # Reclaim cold associations only when the fixed public port pool is full.
        # The control TCP connection is closed too, so clients can reconnect.
        cold = sorted((relay.last_activity, port, relay, writer, transport)
                      for port, (relay, writer, transport) in self._udp_leases.items()
                      if now - relay.last_activity >= _UDP_PRESSURE_IDLE_SECONDS)
        for _, port, relay, writer, transport in cold[:64]:
            if self._udp_leases.get(port, (None,))[0] is not relay:
                continue
            self._udp_leases.pop(port, None)
            self._udp_ports.discard(port)
            transport.close()
            writer.close()
        return min(len(cold), 64)

    async def _udp_associate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        username: str,
        peer: tuple,
    ) -> None:
        loop = asyncio.get_running_loop()
        transport = None
        relay_port = 0
        # Fixed range so the cloud firewall can allow it; hev-socks5-tunnel
        # sends UDP straight to the BND.ADDR/PORT from the reply.
        capacity = self._udp_port_end - self._udp_port_start + 1
        if len(self._udp_ports) >= capacity:
            if self._reclaim_idle_udp(time.monotonic()):
                # DatagramTransport.close() releases its socket on the next turn.
                await asyncio.sleep(0)
        if len(self._udp_ports) >= capacity:
            self.bridge.emit("udp_capacity_full", {"account": username, "used": len(self._udp_ports)})
            await self._reply(writer, 1)
            return
        relay = UdpRelay(self, username, peer_ip=peer[0])
        for _ in range(capacity):
            port = self._udp_next_port
            self._udp_next_port = self._udp_port_start + (port - self._udp_port_start + 1) % capacity
            if port in self._udp_ports:
                continue
            # Reserve before awaiting: concurrent associations must not probe
            # the same port. Keep the cursor moving instead of rebinding every
            # occupied port from the start of the range on each connection.
            self._udp_ports.add(port)
            # The TCP listener may be loopback-only; the UDP relay still has to
            # reach the real network, so bind it to all interfaces on-device.
            udp_bind_host = ("0.0.0.0" if self.cfg.socks_host in
                             ("127.0.0.1", "::1", "localhost", "") else self.cfg.socks_host)
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: relay,
                    local_addr=(udp_bind_host, port),
                )
                relay_port = port
                try:
                    sock = transport.get_extra_info("socket")
                    if sock is not None:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
                except OSError:
                    pass
                break
            except BaseException as exc:
                self._udp_ports.discard(port)
                transport = None
                if not isinstance(exc, OSError):
                    raise
        if transport is None:
            await self._reply(writer, 1)
            return
        self._udp_leases[relay_port] = (relay, writer, transport)
        local = writer.get_extra_info("sockname")
        bind_host = self.cfg.public_host.strip() or (local[0] if local else "0.0.0.0")
        try:
            await self._reply(writer, 0, bind_host, relay_port)
            while True:
                remaining = relay.idle_remaining(time.monotonic())
                if remaining <= 0:
                    break
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            transport.close()
            # A pressure eviction may already have reassigned this port. Never
            # let the old coroutine release a new association's reservation.
            if self._udp_leases.get(relay_port, (None,))[0] is relay:
                self._udp_leases.pop(relay_port, None)
                self._udp_ports.discard(relay_port)

    def observe_udp(self, up: bool, client: tuple, remote: tuple, payload: bytes, account: str = ""):
        packet = UdpPacket(
            up=up,
            local=f"{client[0]}:{client[1]}",
            remote=f"{remote[0]}:{remote[1]}",
            payload=payload,
            timestamp_ms=int(time.time() * 1000),
            account=account,
        )
        handled, actions = self.season.handle(up, packet.local, packet.remote, payload, account)
        if not handled:
            self.broker_worker.observe(packet)
        return handled, actions


def _parse_udp_request(data: bytes) -> tuple[str, int, bytes] | None:
    if len(data) < 10 or data[0] != 0 or data[1] != 0:
        return None
    if data[2] != 0:
        return None
    atyp = data[3]
    offset = 4
    if atyp == 1:
        if len(data) < offset + 4 + 2:
            return None
        host = socket.inet_ntoa(data[offset:offset + 4])
        offset += 4
    elif atyp == 3:
        if len(data) < offset + 1:
            return None
        length = data[offset]
        offset += 1
        if len(data) < offset + length + 2:
            return None
        host = data[offset:offset + length].decode("ascii", "replace")
        offset += length
    elif atyp == 4:
        if len(data) < offset + 16 + 2:
            return None
        host = socket.inet_ntop(socket.AF_INET6, data[offset:offset + 16])
        offset += 16
    else:
        return None
    port = struct.unpack("!H", data[offset:offset + 2])[0]
    offset += 2
    return host, port, data[offset:]


def _encode_udp_header(host: str, port: int) -> bytes:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = ipaddress.ip_address("0.0.0.0")
    if address.version == 4:
        return b"\x00\x00\x00\x01" + address.packed + struct.pack("!H", port)
    return b"\x00\x00\x00\x04" + address.packed + struct.pack("!H", port)


class UdpRelay(asyncio.DatagramProtocol):
    def __init__(self, server: Socks5Server, username: str, peer_ip: str | None = None):
        self.server = server
        self.username = username
        self.peer_ip = peer_ip
        self.last_activity = time.monotonic()
        self.transport: asyncio.DatagramTransport | None = None
        self.client_addr: tuple | None = None
        self.stats = {"up": 0, "down": 0, "empty": 0, "err": 0, "max_ms": 0.0}
        self.total = {"up": 0, "down": 0, "empty": 0, "err": 0, "max_ms": 0.0}
        self.last_err = ""
        self.last_log = time.monotonic()
        self._pending_initials: set[asyncio.Task] = set()

    async def _defer_initial(self, host: str, port: int, payload: bytes) -> None:
        try:
            started = time.monotonic()
            await self.server.wait_notice(self.username, f"{host}:{port}")
            if self.transport is not None and self.client_addr is not None:
                diag.log("Handshake", f"account={self.username} remote={host}:{port} "
                         f"initialDeferredMs={(time.monotonic()-started)*1000:.1f} "
                         f"credential={int((self.username, f'{host}:{port}') in self.server.season.bridge.peers)}")
                self._forward_up(host, port, payload)
        except Exception as exc:
            self.server.note_error(f"deferred-initial: {type(exc).__name__}")
            diag.error("Handshake", "deferred initial", exc)

    def _forward_up(self, host: str, port: int, payload: bytes) -> None:
        started = time.monotonic()
        handled, actions = self.server.observe_udp(True, self.client_addr, (host, port), payload, self.username)
        self._stat(True, handled, actions, (time.monotonic() - started) * 1000)
        if handled:
            for up, wire in actions:
                self.send_action(up, (host, port), wire)
        else:
            try:
                self.transport.sendto(payload, (host, port))
                self.server.note_sent(True)
            except (OSError, ValueError) as exc:
                self.server.note_error(f"up-send: {type(exc).__name__}: {exc}")

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        try:
            sock = transport.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        except (OSError, AttributeError):
            pass

    def idle_remaining(self, now: float) -> float:
        limit = _UDP_UNUSED_SECONDS if self.client_addr is None else _UDP_IDLE_SECONDS
        return limit - (now - self.last_activity)

    def _stat(self, up: bool, handled: bool = False, actions=None, elapsed_ms: float = 0.0) -> None:
        self.stats["up" if up else "down"] += 1
        self.total["up" if up else "down"] += 1
        if handled and not actions:
            self.stats["empty"] += 1
            self.total["empty"] += 1
        if elapsed_ms > self.stats["max_ms"]:
            self.stats["max_ms"] = elapsed_ms
        if elapsed_ms > self.total["max_ms"]:
            self.total["max_ms"] = elapsed_ms
        self.server.note_packet(up, handled, actions, elapsed_ms)
        now = time.monotonic()
        if now - self.last_log >= 5.0:
            st = self.stats
            print(f"[relay] up={st['up']} down={st['down']} empty={st['empty']} "
                  f"err={st['err']} max={st['max_ms']:.1f}ms", flush=True)
            st["up"] = st["down"] = st["empty"] = st["err"] = 0
            st["max_ms"] = 0.0
            self.last_log = now
            self.server.dump_udp_stats()

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            self._receive_datagram(data, addr)
        except Exception as exc:
            self.server.udp_total["transform_err"] += 1
            self.server.udp_interval["transform_err"] += 1
            self.server.note_error(f"udp-processing: {type(exc).__name__}")
            diag.error("RelayError", f"account={self.username} peer={addr[0]}:{addr[1]} "
                       f"bytes={len(data)}", exc)

    def _receive_datagram(self, data: bytes, addr: tuple) -> None:
        if self.transport is None:
            return
        _t0 = time.monotonic()
        if self.client_addr is None or addr == self.client_addr:
            if self.peer_ip is not None and addr[0] != self.peer_ip:
                return
            parsed = _parse_udp_request(data)
            if parsed is None:
                return
            host, port, payload = parsed
            first = self.client_addr is None
            self.client_addr = addr
            self.last_activity = time.monotonic()
            if first:
                diag.log("UDP", f"account={self.username} firstUp={host}:{port} bytes={len(payload)}")
                print(f"[udp] up {host}:{port} first={len(payload)}", flush=True)
            self.server._udp_relays[(self.username, f"{addr[0]}:{addr[1]}")] = self
            if (is_initial(payload) and self.server.pending_notices(self.username)
                    and len(self._pending_initials) < 8):
                task = asyncio.create_task(self._defer_initial(host, port, payload))
                self._pending_initials.add(task)
                task.add_done_callback(self._pending_initials.discard)
                return
            self._forward_up(host, port, payload)
            return
        if self.client_addr is None:
            return
        self.last_activity = time.monotonic()
        handled, actions = self.server.observe_udp(False, self.client_addr, addr, data, self.username)
        self._stat(False, handled, actions, (time.monotonic() - _t0) * 1000)
        if handled:
            for up, wire in actions:
                self.send_action(up, addr, wire)
        else:
            try:
                self.transport.sendto(_encode_udp_header(addr[0], addr[1]) + data, self.client_addr)
                self.server.note_sent(False)
            except (OSError, ValueError) as exc:
                self.server.note_error(f"down-send: {type(exc).__name__}: {exc}")

    def send_action(self, up: bool, remote: tuple, wire: bytes) -> None:
        if self.transport is None or self.client_addr is None:
            return
        try:
            if up:
                self.transport.sendto(wire, remote)
            else:
                self.transport.sendto(_encode_udp_header(remote[0], remote[1]) + wire, self.client_addr)
            self.server.note_sent(up)
        except (OSError, ValueError) as exc:
            self.stats["err"] += 1
            self.total["err"] += 1
            self.last_err = f"send: {type(exc).__name__}: {exc}"
            self.server.note_error(self.last_err)

    def error_received(self, exc: Exception) -> None:
        self.stats["err"] += 1
        self.total["err"] += 1
        self.last_err = f"{type(exc).__name__}: {exc}"
        self.server.note_error(self.last_err)

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None
        for task in self._pending_initials:
            task.cancel()
        if self.client_addr is None:
            return
        key = (self.username, f"{self.client_addr[0]}:{self.client_addr[1]}")
        if self.server._udp_relays.get(key) is self:
            self.server._udp_relays.pop(key, None)
        self.client_addr = None
