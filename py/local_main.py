"""On-device radar node, split into two processes.

- ``start_parser`` runs in the :parser process: UE4 decoding + the local HTTP
  state page (127.0.0.1:17891) and the loopback event socket (17892).
- ``start_relay`` runs in the main app process: VPN SOCKS/TGCP/season relay
  only.  It never imports numpy or the parser, so decoding can never hold the
  relay's GIL and delay game packets.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for entry in (str(HERE), str(HERE / "parser_src")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

sys.setswitchinterval(0.001)

_relay: dict = {
    "thread": None, "loop": None, "server": None, "bridge": None,
    "error": None, "started_at": 0.0, "socks_port": 0,
}
_parser: dict = {
    "thread": None, "loop": None, "bridge": None, "error": None,
    "started_at": 0.0, "http_port": 0,
}


def _port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------------- parser
def start_parser(data_dir: str, http_port: int = 17891, event_port: int = 17892) -> bool:
    thread = _parser.get("thread")
    if thread is not None and thread.is_alive():
        return True
    from radar_server.parser_bridge import LocalParserBridge

    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    bridge = LocalParserBridge(data / "parser", HERE / "parser_src" / "ErCore" / "web", http_port)
    try:
        bridge.start()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        tb = traceback.format_exc()
        try:
            with open(str(Path(data_dir) / "parser_traceback.txt"), "w", encoding="utf-8") as _f:
                _f.write(tb)
        except Exception:
            pass
        _parser["error"] = f"parser start failed: {type(exc).__name__}: {exc}"
        return False

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _parser["loop"] = loop

        async def handle(reader, writer) -> None:
            from radar_server import diagnostics as diag
            diag.log("ParserInput", "event socket connected", key="parser-input-connected")
            try:
                writer.write((json.dumps(bridge.replay_hello())+'\n').encode())
                await writer.drain()
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError) as exc:
                        diag.error("ParserInput", "invalid JSON event", exc)
                        continue
                    if event.get('op') == 'resume':
                        bridge.begin_replay(event.get('sessionId'), event.get('through'))
                        continue
                    if event.get('op') == 'ping':
                        writer.write(b'pong\n')
                        await writer.drain()
                        continue
                    bridge.emit(event.get("type", ""), event.get("data") or {},
                                source_sequence=event.get("sequence"))
                    # Acknowledge admission to the ordered inbox. sendall()
                    # alone only confirms a kernel buffer, not parser receipt.
                    writer.write((str(event['sequence'])+'\n').encode('ascii'))
                    await writer.drain()
            except Exception as exc:
                diag.error("ParserInput", "event socket read", exc)
            finally:
                diag.log("ParserInput", "event socket disconnected", key="parser-input-disconnected")
                try:
                    writer.close()
                except Exception:
                    pass

        async def serve() -> None:
            server = await asyncio.start_server(handle, "127.0.0.1", event_port,
                                                limit=2*1024*1024)
            async with server:
                await server.serve_forever()

        try:
            loop.run_until_complete(serve())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _parser["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    thread = threading.Thread(target=run, name="parser-events", daemon=True)
    _parser.update(thread=thread, bridge=bridge, error=None,
                   started_at=time.time(), http_port=http_port)
    thread.start()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _port_open("127.0.0.1", http_port) and _port_open("127.0.0.1", event_port):
            return True
        if _parser.get("error"):
            return False
        time.sleep(0.05)
    _parser["error"] = "parser ports did not open in time"
    return False


def stop_parser() -> None:
    bridge = _parser.get("bridge")
    loop = _parser.get("loop")
    if bridge is not None:
        try:
            bridge.stop()
        except Exception:
            pass
    if loop is not None:
        try:
            loop.call_soon_threadsafe(lambda: [t.cancel() for t in asyncio.all_tasks(loop)])
        except Exception:
            pass
    thread = _parser.get("thread")
    if thread is not None:
        thread.join(timeout=3)
    _parser.update(thread=None, loop=None, bridge=None, started_at=0.0)


def parser_status_json() -> str:
    bridge = _parser.get("bridge")
    return json.dumps({
        "running": bool(_parser.get("thread") and _parser["thread"].is_alive()),
        "httpPort": _parser.get("http_port", 0),
        "parser": bridge.status() if bridge is not None else None,
        "error": _parser.get("error"),
    }, ensure_ascii=False)


# -------------------------------------------------------------------- relay
def start_relay(data_dir: str, socks_port: int = 1080, parser_port: int = 17892) -> bool:
    thread = _relay.get("thread")
    if thread is not None and thread.is_alive():
        return _port_open("127.0.0.1", socks_port)

    from radar_server.config import Config
    from radar_server.db import Database
    from radar_server.socks5 import Socks5Server
    from radar_server.tcp_bridge import TcpParserBridge
    from radar_server.seasoncrypto import crypto_status
    from radar_server import diagnostics as diag
    crypto = crypto_status()
    diag.log("Crypto", f"backend={crypto['backend']} loadError={crypto['error']}",
             warning=crypto['backend'] != 'native-xtea')

    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    # UDP relay counters (up/down/err/max_ms) are read back by the app's log.
    os.environ["RADAR_RELAY_STATS"] = str(data / "relay_stats.json")

    cfg = Config()
    cfg.socks_host = "127.0.0.1"
    cfg.socks_port = int(socks_port)
    cfg.public_host = "127.0.0.1"
    cfg.udp_port_start = 20000
    cfg.udp_port_end = 20999
    cfg.parser_port = 17891
    cfg.parser_output = str(data / "parser")
    cfg.max_udp_flows = 64
    cfg.max_bank_candidates = 32
    cfg.capture_raw_udp = False

    bridge = TcpParserBridge("127.0.0.1", parser_port, data / "parser")
    bridge.start()
    db = Database()
    server = Socks5Server(cfg, db, bridge)
    _relay.update(thread=None, loop=None, server=server, bridge=bridge, error=None,
                  started_at=time.time(), socks_port=cfg.socks_port)

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _relay["loop"] = loop
        try:
            loop.run_until_complete(server.run())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _relay["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()

    thread = threading.Thread(target=run, name="local-socks", daemon=True)
    _relay["thread"] = thread
    thread.start()

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        if _port_open("127.0.0.1", cfg.socks_port):
            return True
        if _relay.get("error"):
            return False
        time.sleep(0.05)
    _relay["error"] = "local socks did not start in time"
    return False


def start(data_dir: str, socks_port: int = 1080, parser_port: int = 17892) -> bool:
    return start_relay(data_dir, socks_port, parser_port)


def stop_relay() -> None:
    bridge = _relay.get("bridge")
    loop = _relay.get("loop")
    if bridge is not None:
        try:
            bridge.stop()
        except Exception:
            pass
    if loop is not None:
        try:
            loop.call_soon_threadsafe(lambda: [t.cancel() for t in asyncio.all_tasks(loop)])
            time.sleep(0.2)
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
    thread = _relay.get("thread")
    if thread is not None:
        thread.join(timeout=3)
    _relay.update(thread=None, loop=None, server=None, bridge=None, started_at=0.0)


def stop() -> None:
    stop_relay()


def is_running() -> bool:
    thread = _relay.get("thread")
    return bool(thread is not None and thread.is_alive())


def status_json() -> str:
    bridge = _relay.get("bridge")
    return json.dumps({
        "running": is_running(),
        "socksPort": _relay.get("socks_port") or 0,
        "parser": bridge.status() if bridge is not None else None,
        "error": _relay.get("error"),
        "uptimeSeconds": int(time.time() - _relay["started_at"]) if _relay["started_at"] else 0,
    }, ensure_ascii=False)


def assets_dir() -> str:
    return str(HERE / "parser_src" / "ErCore" / "web")


def latest_state_json() -> str:
    # StateUploader reads http://127.0.0.1:17891/api/state from the parser
    # process; the relay process deliberately has no engine.
    return ""
