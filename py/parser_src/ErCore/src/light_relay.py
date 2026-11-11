"""Lightweight SOCKS5 pass-through relay with buffered capture.

Purpose: keep the phone online (SOCKS5) WITHOUT the in-match lag of the
full pipeline. Differences from relay.py / main.py:

- NO per-packet disk flush          (was: 3 fsync-ish flushes per datagram)
- NO pcap                           (was: duplicate write of every payload)
- NO engine parsing / HTTP / WS     (pass-through only, single asyncio loop)
- capture files are written with normal userspace buffering and flushed
  by a periodic task (default every 1.0 s) and on close.

Capture layout is compatible with existing offline tools
(capture_store.load_frames):
    captures/run_YYYYMMDD_HHMMSS/flows/<n>/{meta.json,request.bin,response.bin,frames.jsonl}

Usage:
    python light_relay.py                       # relay + capture
    python light_relay.py --no-capture          # relay only, zero disk writes
    python light_relay.py --port 1081
"""
from __future__ import annotations
import argparse
import asyncio
import concurrent.futures
import json
import logging
import os
import queue
import shutil
import signal
import struct
import socket
import subprocess
import time
import threading
from pathlib import Path

log = logging.getLogger("light_relay")

SOCKS_VERSION = 0x05
NO_AUTH = 0x00
USER_PASS_AUTH = 0x02
NO_ACCEPTABLE_METHODS = 0xFF
CMD_CONNECT = 0x01
CMD_UDP_ASSOCIATE = 0x03
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04
REP_SUCCESS = 0x00
REP_GENERAL_FAILURE = 0x01
REP_COMMAND_NOT_SUPPORTED = 0x07
REP_ADDRESS_TYPE_NOT_SUPPORTED = 0x08

# ---- 资源回收（实测泄漏修复：5.7h 积累 17120 句柄后出站全部挂死）----
# 主漏：CaptureStore.flows 每个唯一 {client}->{host}:{port} 一个 FlowWriter
#（3 个文件句柄）且永不关闭；游戏服务器端口持续轮换 -> 线性涨到崩溃。
FLOW_IDLE_TTL = 90.0        # 流静默该秒数后关闭其文件句柄（再有流量则重开新目录）
UDP_UPSTREAM_TTL = 75.0     # UDP 上游 endpoint 静默该秒数后关闭并清理表项
JANITOR_INTERVAL = 15.0     # 回收器扫描周期
MAX_OPEN_FLOWS = 65535      # 打开中的 FlowWriter 硬上限（超限强制回收，豁免活跃流）。
                            # 与服务器 fd 上限同值（/proc/.../limits 65535）；每 FlowWriter
                            # ~4 个 fd，实际先撞 fd 墙（~16k 流）——本上限只是兜底，
                            # 常态由 janitor 90s TTL 控制在低位
FLOW_PROTECT_SEC = 10.0     # 硬上限回收的活跃豁免窗：last_activity 距今 <10s 的流不回收
                            # （0828 事故：三人同玩 UDP 碎流 ~11流/s，90s 打满 1024 上限，
                            # reap_idle(0.0) 无条件回收把**正在写入的战斗流**也关了，
                            # 同 flow_id 再来流量重开新目录无握手 -> manager 握手门
                            # 永不通过 -> 全员 detecting。对局内战斗流最大静止实测
                            # 3.12s，10s 窗口足够豁免；真静默流仍被回收，防泄漏语义不变）
# SOCKS5 认证白名单（0828 二次事故）：曾实现为「用户名任意非空即过+密码不校验」，
# 公网扫描器用 admin/root/socks 字典随便过认证，12h 刷了 20.4 万条 TCP 流目录
# （dir_seq 飙到 20 万+），管理台 udp_flows 被混入扫描器流。现用户名+密码都必须
# 命中合法 token（逗号分隔环境变量 RELAY_TOKENS；手机端 SOCKS5 用户名=雷达 token，
# 密码填同一 token）。未配置时回退旧行为（仅要求用户名非空），LAN 内联调不破。
_RELAY_TOKENS = tuple(
    t.strip() for t in os.environ.get("RELAY_TOKENS", "").split(",") if t.strip()
)
# ---- 自动封禁（0828 三次：扫描器字典爆破刷屏）----
# 单 IP 在 AUTH_BAN_WINDOW 秒内认证失败 AUTH_BAN_FAILS 次 -> nft 表
# inet/nr_blacklist 的 blocklist 集合 drop（进程外封禁，重启 relay 不解封）。
# AUTH_BAN_OFF=1 可关（默认开）。NFT_BIN 找不到 nft 时自动禁用（本机开发）。
AUTH_BAN_FAILS = int(os.environ.get("AUTH_BAN_FAILS", "20"))
AUTH_BAN_WINDOW = 300.0     # 失败计数窗口（秒）：窗口外的时间戳不计入
AUTH_BAN_OFF = os.environ.get("AUTH_BAN_OFF", "") == "1"
NFT_BIN = shutil.which("nft")
NFT_TABLE = "inet nr_blacklist"
NFT_SET = "blocklist"
# 0828 user 定版：**不写持久化文件**——临时封禁即可（重启 relay/服务器自然解封，
# 扫描器换 IP 也无所谓）；手动永久拉黑走 nft 命令行。
HANDSHAKE_TIMEOUT = 20.0    # SOCKS5 握手（greeting/认证/请求）超时；防 slowloris 挂死占 fd
CONNECT_TIMEOUT = 15.0      # CONNECT 上游建连超时
STREAM_CLOSE_TIMEOUT = 2.0  # 异常 socket 关闭不能反过来卡住 handler 的连接计数回收
SOCKS_BACKLOG = int(os.environ.get("RELAY_BACKLOG", "1024"))
PREAUTH_CONN_LIMIT = int(os.environ.get("RELAY_PREAUTH_CONNS_PER_IP", "128"))
# 抓包空间护栏：磁盘写满会让 relay 无法创建抓包目录（实测 35G 抓包打满 40G 盘）。
# 转发是主功能、落盘是尽力而为。CAPTURE_MAX_GB 是抓包总配额，不是剩余空间；
# CAPTURE_MIN_FREE_GB 是整块磁盘的保底空间。任一阈值触发时只停写盘，继续转发。
CAPTURE_MAX_GB = float(os.environ.get("ER_CAPTURE_MAX_GB", "8"))
CAPTURE_MIN_FREE_GB = float(os.environ.get("ER_CAPTURE_MIN_FREE_GB", "4"))
CAPTURE_PRUNE_INTERVAL = float(os.environ.get("ER_CAPTURE_PRUNE_INTERVAL", "60"))
CAPTURE_FLOW_MIN_AGE_SEC = float(
    os.environ.get("ER_CAPTURE_FLOW_MIN_AGE", "180"))
CAPTURE_KEEP_RUNS = 1             # 默认只保留当前/最新 run，旧 run 可按配额回收


def _run_disk_usage(root: str) -> tuple[float, list]:
    """captures 根下各 run_*/ 的总字节与 (mtime, path, bytes) 列表。"""
    total = 0
    runs = []
    try:
        entries = list(Path(root).iterdir())
    except OSError:
        return 0.0, []
    for d in entries:
        if not (d.is_dir() and d.name.startswith("run_")):
            continue
        size = 0
        for p in d.rglob("*"):
            try:
                if p.is_file():
                    size += p.stat().st_size
            except OSError:
                continue
        try:
            mt = d.stat().st_mtime
        except OSError:
            mt = 0.0
        total += size
        runs.append((mt, str(d), size))
    return float(total), sorted(runs)   # mtime 升序 = 最旧在前


def _prune_capture_runs(root: str, max_bytes: float,
                        keep_runs: int = CAPTURE_KEEP_RUNS,
                        protected_run: str | None = None) -> tuple[int, float]:
    """删除旧 run，并返回 ``(删除数, 估算剩余字节)``。

    ``protected_run`` 通常是当前 relay 正在写入的 run。它永远不会被删，
    即使当前 run 是唯一目录且已经超过配额；这种情况下调用方应停写抓包，
    不能为了回收空间删除正在写入的文件。
    """
    if max_bytes <= 0:
        return 0, 0.0
    total, runs = _run_disk_usage(root)
    if total <= max_bytes:
        return 0, total

    protected = (os.path.realpath(protected_run)
                 if protected_run else None)
    keep_n = max(0, int(keep_runs))
    keep_rows = runs[-keep_n:] if keep_n else ()
    keep_paths = {os.path.realpath(path) for _mt, path, _size in keep_rows}
    if protected:
        keep_paths.add(protected)

    # 最旧优先删。当前 run 和保留窗口内的最新 run 都不可删除；
    # 这允许“正好两个 run 但超额”时清掉旧 run，而不会碰当前目录。
    deletable = [row for row in runs
                 if os.path.realpath(row[1]) not in keep_paths]
    freed = 0
    n_del = 0
    for _mt, path, size in deletable:
        if total - freed <= max_bytes:
            break
        try:
            shutil.rmtree(path)
            freed += size
            n_del += 1
            log.info("capture prune: removed %s (%.1f MB)", path, size / 1e6)
        except OSError as exc:
            log.warning("capture prune failed %s: %s", path, exc)
    return n_del, max(0.0, total - freed)


def _flow_disk_usage(run_dir: str, protected_flows=(),
                     min_age: float = CAPTURE_FLOW_MIN_AGE_SEC) -> list:
    """返回当前 run 中可回收的旧 flow：(mtime, path, bytes)。

    活跃 flow 由调用方传入保护集合；其余目录还要经过最小年龄门槛，
    避免清理线程和刚创建/刚结束的流发生竞争。
    """
    if not run_dir:
        return []
    flows_dir = Path(run_dir) / "flows"
    protected = {os.path.realpath(str(p)) for p in protected_flows}
    now = time.time()
    try:
        entries = list(flows_dir.iterdir())
    except OSError:
        return []
    rows = []
    for flow_dir in entries:
        if not flow_dir.is_dir() or os.path.realpath(str(flow_dir)) in protected:
            continue
        size = 0
        latest = 0.0
        try:
            latest = flow_dir.stat().st_mtime
            for p in flow_dir.rglob("*"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                latest = max(latest, st.st_mtime)
                if p.is_file():
                    size += st.st_size
        except OSError:
            continue
        if now - latest >= min_age:
            rows.append((latest, str(flow_dir), size))
    return sorted(rows)


def _capture_pressure(total: float, free: float, max_bytes: float,
                      min_free_bytes: float) -> bool:
    return ((max_bytes > 0 and total > max_bytes) or
            (min_free_bytes > 0 and free < min_free_bytes))


def _prune_capture_storage(root: str, max_bytes: float,
                           keep_runs: int = CAPTURE_KEEP_RUNS,
                           protected_run: str | None = None,
                           protected_flows=(),
                           min_free_bytes: float = 0.0) -> tuple[int, int, float, float]:
    """滚动清理抓包，返回 ``(删除 run 数, 删除 flow 数, 剩余字节, 剩余空间)``。

    清理顺序是旧 run -> 当前 run 中已结束的旧 flow。当前 run 和活跃 flow
    都受保护；因此清理可以和抓包并行进行，不会删掉正在写入的文件。
    """
    total, runs = _run_disk_usage(root)
    try:
        free = float(shutil.disk_usage(root).free)
    except OSError:
        free = 0.0
    if not _capture_pressure(total, free, max_bytes, min_free_bytes):
        return 0, 0, total, free

    protected = (os.path.realpath(protected_run)
                 if protected_run else None)
    keep_n = max(0, int(keep_runs))
    keep_rows = runs[-keep_n:] if keep_n else ()
    keep_paths = {os.path.realpath(path) for _mt, path, _size in keep_rows}
    if protected:
        keep_paths.add(protected)

    freed = 0.0
    n_runs = 0
    n_flows = 0
    old_runs = [row for row in runs
                if os.path.realpath(row[1]) not in keep_paths]
    for _mt, path, size in old_runs:
        if not _capture_pressure(total - freed, free + freed,
                                 max_bytes, min_free_bytes):
            break
        try:
            shutil.rmtree(path)
            freed += size
            n_runs += 1
            log.info("capture prune: removed run %s (%.1f MB)",
                     path, size / 1e6)
        except OSError as exc:
            log.warning("capture prune failed %s: %s", path, exc)

    # 如果旧 run 不够，继续从当前 run 清理已结束的旧 flow，保持持续抓包。
    if (_capture_pressure(total - freed, free + freed,
                          max_bytes, min_free_bytes) and protected_run):
        for _mt, path, size in _flow_disk_usage(
                protected_run, protected_flows, CAPTURE_FLOW_MIN_AGE_SEC):
            if not _capture_pressure(total - freed, free + freed,
                                     max_bytes, min_free_bytes):
                break
            try:
                shutil.rmtree(path)
                freed += size
                n_flows += 1
                log.info("capture prune: removed flow %s (%.1f MB)",
                         path, size / 1e6)
            except OSError as exc:
                log.warning("capture prune failed %s: %s", path, exc)

    return n_runs, n_flows, max(0.0, total - freed), free + freed


def prune_captures(root: str, max_bytes: float,
                   keep_runs: int = CAPTURE_KEEP_RUNS,
                   protected_run: str | None = None) -> int:
    """超量删除最旧 run 目录，返回删除数。

    只删 relay 自己的 captures 根下的 ``run_*``；当前 run 可通过
    ``protected_run`` 保护。根不存在/未超量直接返回 0。
    """
    return _prune_capture_runs(root, max_bytes, keep_runs, protected_run)[0]


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    return await reader.readexactly(n)


async def _close_stream_writer(writer: asyncio.StreamWriter | None) -> None:
    """关闭流并等待 transport 真正释放，但为异常 socket 设置硬超时。"""
    if writer is None:
        return
    try:
        writer.close()
    except Exception:
        return
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is None:
        return
    try:
        await asyncio.wait_for(wait_closed(), timeout=STREAM_CLOSE_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass


def _detect_lan_ip() -> str | None:
    """检测本机对外可达 IP（UDP connect 不实际发包，取路由出口接口地址）。

    局域网测试/公网部署无 --public-addr 时，BND 用它，避免回 127.0.0.1 导致
    手机 UDP 打不进来。"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return None


class FlowWriter:
    """Buffered per-flow capture writer (no flush per append)."""

    def __init__(self, flow_dir: str, flow_id: str, meta: dict,
                 on_write_error=None):
        self.flow_dir = flow_dir
        self.flow_id = flow_id
        self.meta = meta
        self._on_write_error = on_write_error
        self._seq = 0
        self.last_activity = time.monotonic()   # janitor 回收依据
        self.closed = False                     # 关闭后 append 静默丢弃，
        self._req = self._resp = self._frames = None
        try:
            self._req = open(os.path.join(flow_dir, "request.bin"), "wb")
            self._resp = open(os.path.join(flow_dir, "response.bin"), "wb")
            self._frames = open(os.path.join(flow_dir, "frames.jsonl"),
                                "a", encoding="utf-8")
            with open(os.path.join(flow_dir, "meta.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=2)
        except Exception:
            # 目录刚创建后空间耗尽时，前两个句柄可能已经打开；释放它们，
            # 否则“仅停写抓包”的降级路径本身会制造句柄泄漏。
            self.close()
            raise

    def append(self, direction: str, data: bytes, timestamp: float | None = None) -> None:
        if not data or self.closed:
            return
        self.last_activity = time.monotonic()
        seq = self._seq
        self._seq += 1
        ts = time.time() if timestamp is None else timestamp
        fh = self._req if direction == "request" else self._resp
        try:
            offset = fh.tell()
            fh.write(data)
            frame = (json.dumps(
                {
                    "seq": seq,
                    "ts": round(ts, 6),
                    "dir": direction,
                    "offset": offset,
                    "length": len(data),
                },
                separators=(",", ":"),
            ) + "\n")
            self._frames.write(frame)
        except OSError as exc:
            # 磁盘满等：落盘尽力而为，绝不能把写盘失败抛回转发循环（会断手机连接）。
            if not self.closed:
                log.warning("capture write failed for flow %s; pausing this flow (%s)",
                            self.flow_id, exc)
                self.close()
                if self._on_write_error is not None:
                    try:
                        self._on_write_error(exc)
                    except Exception:
                        log.exception("capture error callback failed")

    def flush(self) -> None:
        for fh in (self._req, self._resp, self._frames):
            if fh is None:
                continue
            try:
                fh.flush()
            except Exception:
                pass

    def close(self) -> None:
        self.closed = True
        for fh in (self._req, self._resp, self._frames):
            if fh is None:
                continue
            try:
                fh.flush()
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


class CaptureStore:
    def __init__(self, root: str, enabled: bool):
        self.root = root
        self.enabled = enabled
        self.capture_disabled_reason = None
        self._capture_retry_after = 0.0
        self._capture_log_after = 0.0
        self._prune_requested = False
        self._prune_event = None
        self.run_dir = None
        self.flows: dict[str, FlowWriter] = {}
        self._dir_seq = 0   # 目录号单调递增：流被回收后 len(flows) 会回退导致重名
        if enabled:
            run_id = time.strftime("run_%Y%m%d_%H%M%S")
            self.run_dir = os.path.join(root, run_id)
            try:
                os.makedirs(os.path.join(self.run_dir, "flows"), exist_ok=True)
                with open(os.path.join(self.run_dir, "meta.json"), "w", encoding="utf-8") as fh:
                    json.dump(
                        {
                            "created": time.time(),
                            "created_iso": time.strftime(
                                "%Y-%m-%d %H:%M:%S", time.localtime()
                            ),
                            "run_id": run_id,
                            "mode": "light_relay",
                        },
                        fh,
                        ensure_ascii=False,
                        indent=2,
                    )
            except OSError as exc:
                # 磁盘满/不可写：降级为仅转发（不落盘）。转发是主功能，
                # 落盘是尽力而为；绝不能因写盘失败让 relay 崩溃 -> 手机没网。
                log.warning("capture disabled: cannot write %s (%s); relaying only",
                            self.run_dir, exc)
                self.enabled = False
                self.capture_disabled_reason = str(exc)
                self.run_dir = None

    def disable_capture(self, reason: str) -> None:
        """关闭落盘并释放所有抓包句柄；SOCKS 转发继续运行。"""
        if not self.enabled:
            return
        self.enabled = False
        self.capture_disabled_reason = reason
        log.error("capture disabled; relay remains online: %s", reason)
        for fw in self.flows.values():
            fw.close()

    def open_flow(self, flow_id: str, host: str, port: int, proto: str, client: str,
                  user: str | None = None) -> FlowWriter | None:
        if not self.enabled:
            return None
        if time.monotonic() < self._capture_retry_after:
            return None
        fw = self.flows.get(flow_id)
        if fw is not None and not fw.closed:
            return fw
        # 已回收的流再来流量 -> 重开一个新目录（frames.jsonl 从头计，互不影响）
        if self._count_open() >= MAX_OPEN_FLOWS:
            # 硬上限回收但豁免活跃流（FLOW_PROTECT_SEC 内有写入）：战斗流正被
            # 写入时拆掉 = 同 flow_id 后续流量重开无握手新目录，雷达整局失明
            # （0828 三人同玩事故）。极端情况下活跃流挤满 open 表时放弃落盘
            # （返回 None，append 调用处已判空）——转发主功能不受影响，且
            # 保住 fb19a27 句柄泄漏修复的硬上限语义。
            self.reap_idle(FLOW_PROTECT_SEC)
            if self._count_open() >= MAX_OPEN_FLOWS:
                return None
        self._dir_seq += 1
        flow_dir = os.path.join(self.run_dir, "flows", str(self._dir_seq - 1))
        try:
            os.makedirs(flow_dir, exist_ok=True)
            fw = FlowWriter(
                flow_dir,
                flow_id,
                {
                    "flow_id": flow_id,
                    "host": host,
                    "port": port,
                    "proto": proto,
                    "client": client,
                    "user": user or ("ip:" + client.rsplit(":", 1)[0]),
                    "created": time.time(),
                },
                on_write_error=self._on_flow_write_error,
            )
        except OSError as exc:
            # 目录/文件创建失败同样只能关闭抓包，不能让异常回到 SOCKS 处理器。
            try:
                shutil.rmtree(flow_dir, ignore_errors=True)
            except Exception:
                pass
            self._note_capture_failure("capture open failed: %s" % exc)
            return None
        self.flows[flow_id] = fw
        return fw

    def _on_flow_write_error(self, exc: BaseException) -> None:
        self._note_capture_failure("capture write failed: %s" % exc)

    def _note_capture_failure(self, reason: str) -> None:
        """短暂退避并唤醒清理器；抓包保持开启，不影响网络转发。"""
        now = time.monotonic()
        self._capture_retry_after = max(self._capture_retry_after, now + 10.0)
        self._prune_requested = True
        if now >= self._capture_log_after:
            log.warning("capture pressure; retrying after cleanup: %s", reason)
            self._capture_log_after = now + 30.0
        if self._prune_event is not None:
            loop = getattr(self, "_prune_loop", None)
            if loop is not None:
                loop.call_soon_threadsafe(self._prune_event.set)
            else:
                self._prune_event.set()

    def _count_open(self) -> int:
        return sum(1 for fw in self.flows.values() if not fw.closed)

    def reap_idle(self, max_idle: float) -> int:
        """关闭静默超时的 FlowWriter，释放其文件句柄。返回关闭数。"""
        now = time.monotonic()
        dead = [fid for fid, fw in self.flows.items()
                if not fw.closed and now - fw.last_activity > max_idle]
        dead.sort(key=lambda fid: self.flows[fid].last_activity)   # 最旧优先
        for fid in dead:
            fw = self.flows.pop(fid)
            fw.close()
        return len(dead)

    def flush_all(self) -> None:
        for fw in self.flows.values():
            if not fw.closed:
                fw.flush()

    def close(self) -> None:
        for fw in self.flows.values():
            fw.close()


class _QueuedFlow:
    def __init__(self, owner, metadata):
        self.owner, self.metadata = owner, metadata

    def append(self, direction, data):
        self.owner.enqueue(self.metadata, direction, data, time.time())


class QueuedCaptureStore:
    """One ordered disk writer; packet forwarding never waits for disk I/O.

    Overload preserves a complete capture prefix then disables recording rather
    than silently omitting frames inside a live capture or blocking the relay.
    """
    def __init__(self, storage, max_bytes=32 * 1024 * 1024, max_packets=65536):
        self.storage = storage
        self.max_bytes, self.max_packets = max_bytes, max_packets
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._bytes = self._packets = 0
        self._failed = self._closing = False
        self.storage._prune_loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run, name="capture-writer", daemon=True)
        self._thread.start()

    @property
    def enabled(self):
        return self.storage.enabled and not self._failed

    def open_flow(self, flow_id, host, port, proto, client, user=None):
        if self._failed or self._closing or not self.storage.enabled:
            return None
        return _QueuedFlow(self, (flow_id, host, port, proto, client, user))

    def enqueue(self, metadata, direction, data, timestamp):
        if not data:
            return
        with self._lock:
            if self._failed or self._closing or not self.storage.enabled:
                return
            if self._bytes + len(data) > self.max_bytes or self._packets >= self.max_packets:
                self._failed = True
                self._queue.put(("disable_capture", ("capture queue capacity exceeded",), None, 0))
                log.error("capture queue full; forwarding continues, recording stops after queued prefix")
                return
            self._bytes += len(data)
            self._packets += 1
            self._queue.put(("packet", (metadata, direction, data, timestamp), None, len(data)))

    def pending(self):
        with self._lock:
            return self._packets, self._bytes

    async def call(self, op, *args):
        future = concurrent.futures.Future()
        self._queue.put((op, args, future, 0))
        # Cancellation of a maintenance task must not cancel work already
        # queued before shutdown and leave the capture writer in a bad state.
        return await asyncio.shield(asyncio.wrap_future(future))

    def _run(self):
        while True:
            op, args, future, size = self._queue.get()
            try:
                if op == "packet":
                    metadata, direction, data, timestamp = args
                    fw = self.storage.open_flow(*metadata)
                    if fw is not None:
                        fw.append(direction, data, timestamp=timestamp)
                    result = None
                elif op == "protected":
                    result = tuple(fw.flow_dir for fw in self.storage.flows.values())
                else:
                    result = getattr(self.storage, op)(*args)
                if future is not None:
                    future.set_result(result)
            except Exception as exc:
                if future is not None:
                    future.set_exception(exc)
                else:
                    self._failed = True
                    log.exception("capture writer failed; forwarding remains online")
                    self.storage.disable_capture(str(exc))
            finally:
                if op == "packet":
                    with self._lock:
                        self._bytes -= size
                        self._packets -= 1
                self._queue.task_done()
            if op == "close":
                return

    async def aclose(self):
        with self._lock:
            self._closing = True
        await self.call("close")
        await asyncio.to_thread(self._thread.join)


def _udp_buffers(transport):
    sock = transport.get_extra_info("socket")
    if sock is not None:
        for option in (socket.SO_RCVBUF, socket.SO_SNDBUF):
            try:
                sock.setsockopt(socket.SOL_SOCKET, option, 1024 * 1024)
            except OSError:
                pass


class _AuthRejected(Exception):
    """SOCKS5 认证被拒（无认证客户端 / 认证失败）。正常关闭，不算错误。"""


class LightRelay:
    def __init__(self, bind_host: str, port: int, store: CaptureStore,
                 public_addr: str | None = None, require_auth: bool = False,
                 max_conns_per_user: int = 0,
                 preauth_conns_per_ip: int = PREAUTH_CONN_LIMIT,
                 max_conns_per_ip: int | None = None):
        # 兼容旧部署/测试调用方：旧参数名仍可使用，但现在表示“认证账号
        # 上限”。认证前的 IP 护栏由 preauth_conns_per_ip 独立承担。
        if max_conns_per_ip is not None:
            if max_conns_per_user and max_conns_per_user != max_conns_per_ip:
                raise ValueError("max_conns_per_user 与旧 max_conns_per_ip 不一致")
            max_conns_per_user = max_conns_per_ip
        self.bind_host = bind_host
        self.port = port
        self.store = store
        self.udp_relay_port = port
        # UDP ASSOCIATE 的 BND 地址：公网部署（NAT/EIP）时服务器 socket 的
        # sockname 是内网 IP，手机按它发 UDP 根本不可达 -> 进不去对局。
        # 显式指定 public_addr 后用它回 BND；否则自动检测局域网出口 IP。
        self.public_addr = public_addr or _detect_lan_ip()
        # 公开部署防护：不强制认证 = 全网开放代理，被扫到即耗尽 fd -> 手机没网。
        self.require_auth = require_auth
        self.max_conns_per_user = max_conns_per_user   # 0 = 不限
        self.preauth_conns_per_ip = preauth_conns_per_ip  # 仅握手阶段，0 = 不限
        self._preauth_by_ip: dict[str, int] = {}
        # 认证成功后的连接按账号计数；同一公网 IP 下的不同账号互不影响。
        self._conns_by_user: dict[str, int] = {}
        # 旧诊断脚本读取此字段，保留为认证前 IP 计数表的兼容别名。
        self._conns_by_ip = self._preauth_by_ip
        self.denied = 0                 # 被拒绝的握手数（诊断用）
        # ---- 自动封禁（0828）：认证失败计次 -> nft 拉黑 ----
        # 阈值内失败只拒不封（手机用户改配置可能连错几次）；打满 AUTH_BAN_FAILS
        # 次即 nft drop（重启不丢：nft 持久化由 nr_blacklist.service 负责，这里
        # 再补写持久文件）。白名单 IP / 本机永不封。
        self._auth_fail: dict[str, list] = {}   # ip -> [失败 monotonic 时间戳...]
        self._banned: set = set()               # 本进程已 nft 封过的 ip（防重复下发）
        self._ban_exempt = {"127.0.0.1", "::1"}
        self._udp_shared_transport: asyncio.DatagramTransport | None = None
        self._udp_proto: _UdpRelayProtocol | None = None
        self._udp_clients: set = set()

    # ---------------------------------------------------------- 准入
    def _note_deny(self, ip: str) -> None:
        """拒绝计次，打满阈值即 nft 拉黑（见 AUTH_BAN_* 常量注释）。"""
        self._maybe_autoban(ip)

    def _maybe_autoban(self, ip: str) -> None:
        """认证失败计数 -> 打满阈值 nft 拉黑（见 AUTH_BAN_* 常量注释）。

        阻塞的 nft 调用丢线程池；失败只记日志不影响转发主链路。"""
        if (AUTH_BAN_OFF or not NFT_BIN or not ip or ":" not in ip and
                ip.count(".") != 3 or ip in self._ban_exempt):
            return
        now = time.monotonic()
        rows = [t for t in self._auth_fail.get(ip, []) if now - t < AUTH_BAN_WINDOW]
        rows.append(now)
        self._auth_fail[ip] = rows
        if len(rows) < AUTH_BAN_FAILS or ip in self._banned:
            return
        self._banned.add(ip)
        self._auth_fail.pop(ip, None)
        log.warning("AUTOBAN %s: %d auth fails in %ds -> nft drop",
                    ip, len(rows), int(AUTH_BAN_WINDOW))
        # nft 添加（临时封禁：不写持久化文件，重启即解封——0828 user 定版）
        def _ban():
            subprocess.run(
                [NFT_BIN, "add", "element", "inet", "nr_blacklist", NFT_SET,
                 "{", ip, "}"], capture_output=True)
        try:
            asyncio.get_running_loop().run_in_executor(None, _ban)
        except Exception as exc:
            log.warning("autoban nft error %s: %r", ip, exc)

    def _flow_key(self, host: str, port: int, proto: str, client: str) -> str:
        return f"{client}->{host}:{port}/{proto}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.bind_host, self.port,
            backlog=SOCKS_BACKLOG,
        )
        log.info("light SOCKS5 relay listening on %s:%d (capture=%s, token-whitelist=%d)",
                 self.bind_host, self.port, self.store.enabled, len(_RELAY_TOKENS))

    # ------------------------------------------------------------- TCP

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "unknown"
        client = f"{ip}:{peer[1]}" if peer else "unknown"
        preauth_counted = False
        user_key: str | None = None
        # 认证前仍保留 IP 护栏，防止全网扫描器用 slowloris 占满 fd；它只覆盖
        # 尚未完成 SOCKS5 认证的短窗口，不会把同一 NAT 下的多个已认证账号绑在一起。
        if self.preauth_conns_per_ip > 0:
            cur = self._preauth_by_ip.get(ip, 0)
            if cur >= self.preauth_conns_per_ip:
                log.warning("reject %s: pre-auth per-ip conn limit %d",
                            client, self.preauth_conns_per_ip)
                await _close_stream_writer(writer)
                return
            self._preauth_by_ip[ip] = cur + 1
            preauth_counted = True
        try:
            try:
                user, cmd, host, port, atyp, addr = await asyncio.wait_for(
                    self._socks_handshake(reader, writer), timeout=HANDSHAKE_TIMEOUT
                )
            except _AuthRejected:
                return
            except asyncio.TimeoutError:
                log.debug("handshake timeout %s", client)
                return
            finally:
                if preauth_counted:
                    current = self._preauth_by_ip.get(ip, 0) - 1
                    if current > 0:
                        self._preauth_by_ip[ip] = current
                    else:
                        self._preauth_by_ip.pop(ip, None)
                    preauth_counted = False

            # 认证成功后才按用户名限流。这样同一公网 IP 下的 11 个账号各自有
            # 独立配额；并发竞争在单一 asyncio 事件循环内完成，不需要锁。
            if (user is not None and self.max_conns_per_user > 0):
                cur = self._conns_by_user.get(user, 0)
                if cur >= self.max_conns_per_user:
                    log.warning("reject %s user=%r: per-user conn limit %d",
                                client, user, self.max_conns_per_user)
                    writer.write(self._reply_packet(
                        REP_GENERAL_FAILURE, ATYP_IPV4, b"\x00\x00\x00\x00", 0))
                    await writer.drain()
                    return
                self._conns_by_user[user] = cur + 1
                user_key = user
            if cmd == CMD_CONNECT:
                await self._handle_connect(reader, writer, client, host, port, user)
            elif cmd == CMD_UDP_ASSOCIATE:
                await self._handle_udp_associate(reader, writer, client, user)
            else:
                writer.write(self._reply_packet(REP_COMMAND_NOT_SUPPORTED, ATYP_IPV4, b"\x00\x00\x00\x00", 0))
                await writer.drain()
        except (asyncio.IncompleteReadError, ValueError, ConnectionError, OSError) as exc:
            log.debug("client %s dropped: %s", client, exc)
        finally:
            if user_key is not None:
                current = self._conns_by_user.get(user_key, 0) - 1
                if current > 0:
                    self._conns_by_user[user_key] = current
                else:
                    self._conns_by_user.pop(user_key, None)
            if preauth_counted:
                current = self._preauth_by_ip.get(ip, 0) - 1
                if current > 0:
                    self._preauth_by_ip[ip] = current
                else:
                    self._preauth_by_ip.pop(ip, None)
            await _close_stream_writer(writer)

    async def _socks_handshake(self, reader, writer) -> tuple:
        """SOCKS5 greeting + (可选) RFC1929 认证 + 请求解析。

        返回 (user, cmd, host, port, atyp, addr)。认证被拒时抛 _AuthRejected。"""
        head = await _read_exact(reader, 2)
        nmethods = head[1]
        methods = await _read_exact(reader, nmethods)
        if USER_PASS_AUTH in methods:
            # RFC 1929 username/password: username = device/user token
            writer.write(bytes([SOCKS_VERSION, USER_PASS_AUTH]))
            await writer.drain()
            ver = (await _read_exact(reader, 1))[0]
            ulen = (await _read_exact(reader, 1))[0]
            uname = (await _read_exact(reader, ulen)).decode("utf-8", "replace") if ulen else ""
            plen = (await _read_exact(reader, 1))[0]
            passwd = (await _read_exact(reader, plen)).decode("utf-8", "replace") if plen else ""
            if _RELAY_TOKENS:
                # 白名单：用户名=密码=合法 token 才放行（防公网扫描器字典爆破）
                ok = 1 if (ver == 0x01 and uname in _RELAY_TOKENS
                           and passwd in _RELAY_TOKENS) else 0
                reason = "whitelist" if ok else "bad_token"
            else:
                # 未配置白名单：LAN 内联调，仅要求用户名非空
                ok = 1 if (ver == 0x01 and uname) else 0
                reason = "no_username"
            # RFC 1929 status: 0x00 = success, 0x01 = failure
            writer.write(bytes([0x01, 0x00 if ok else 0x01]))
            await writer.drain()
            if not ok:
                self.denied += 1
                log.info("auth deny user=%r reason=%s", uname, reason)
                self._note_deny(self._peer_ip(writer))
                raise _AuthRejected()
            user = uname
        else:
            if self.require_auth:
                # 公开部署：无认证客户端 = 全网开放代理。直接回"无可用认证方法"拒绝。
                writer.write(bytes([SOCKS_VERSION, NO_ACCEPTABLE_METHODS]))
                await writer.drain()
                raise _AuthRejected()
            writer.write(bytes([SOCKS_VERSION, NO_AUTH]))
            await writer.drain()
            user = None

        cmd, host, port, atyp, addr = await self._parse_socks_request(reader)
        return user, cmd, host, port, atyp, addr

    @staticmethod
    def _peer_ip(writer) -> str:
        peer = writer.get_extra_info("peername")
        return peer[0] if peer else ""

    async def _parse_socks_request(self, reader):
        header = await _read_exact(reader, 4)
        ver, cmd, rsv, atyp = header
        if ver != SOCKS_VERSION:
            raise ValueError("bad socks version")
        if atyp == ATYP_IPV4:
            addr = await _read_exact(reader, 4)
            host = ".".join(str(b) for b in addr)
        elif atyp == ATYP_DOMAIN:
            (length,) = await _read_exact(reader, 1)
            addr = await _read_exact(reader, length)
            host = addr.decode("latin-1")
        elif atyp == ATYP_IPV6:
            addr = await _read_exact(reader, 16)
            host = ":".join(f"{int.from_bytes(addr[i:i+2], 'big'):x}" for i in range(0, 16, 2))
        else:
            raise ValueError("bad atyp")
        port = struct.unpack(">H", await _read_exact(reader, 2))[0]
        return cmd, host, port, atyp, addr

    def _reply_packet(self, rep: int, atyp: int, addr: bytes, port: int) -> bytes:
        return bytes([SOCKS_VERSION, rep, 0x00, atyp]) + addr + struct.pack(">H", port)

    async def _handle_connect(self, reader, writer, client, host, port, user=None) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("CONNECT %s:%d failed: %s", host, port, exc)
            writer.write(self._reply_packet(REP_GENERAL_FAILURE, ATYP_IPV4, b"\x00\x00\x00\x00", 0))
            await writer.drain()
            return
        # 连接建立后的任何异常（客户端断开、写盘失败等）都必须回收上游 socket，
        # 否则每次失败的握手泄漏一个句柄（实测泄漏路径之一）
        try:
            bound = upstream_writer.get_extra_info("sockname")
            bhost, bport = bound[0], bound[1]
            import ipaddress
            ip = ipaddress.ip_address(bhost.split("%")[0])   # 去掉 scope id
            if ip.version == 6:
                writer.write(self._reply_packet(REP_SUCCESS, ATYP_IPV6, ip.packed, bport))
            else:
                writer.write(self._reply_packet(REP_SUCCESS, ATYP_IPV4, ip.packed, bport))
            await writer.drain()

            flow_id = self._flow_key(host, port, "tcp", client)
            flow = self.store.open_flow(flow_id, host, port, "tcp", client, user=user)
            log.info("TCP flow %s user=%s", flow_id, user)

            async def pipe(src, dst, direction):
                try:
                    while True:
                        try:
                            data = await asyncio.wait_for(src.read(65536), timeout=600)
                        except asyncio.TimeoutError:
                            break  # idle/dead connection: recycle the pipe
                        if not data:
                            break
                        dst.write(data)
                        # drain only under backpressure; never per-chunk
                        if dst.transport.get_write_buffer_size() > 256 * 1024:
                            await dst.drain()
                        if flow is not None:
                            flow.append(direction, data)
                except (ConnectionError, OSError, asyncio.IncompleteReadError):
                    pass

            # 任一方向 EOF/异常/空闲超时都说明这个 SOCKS 隧道已经不能继续复用。
            # 旧实现 gather 两个方向：手机先断开后，上游若不主动 FIN，另一个 read
            # 会再挂 600s，连接停在 CLOSE-WAIT 且 _conns_by_ip 不递减；真实客户端
            # 很快打满 128/IP 上限，最终表现为“VPN 已连接但无网络”。先完成的一侧
            # 触发取消另一侧，finally 随即关闭两端，避免半关闭连接长期占位。
            pipes = {
                asyncio.create_task(pipe(reader, upstream_writer, "request")),
                asyncio.create_task(pipe(upstream_reader, writer, "response")),
            }
            _done, pending = await asyncio.wait(
                pipes, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
        finally:
            await asyncio.gather(
                _close_stream_writer(writer),
                _close_stream_writer(upstream_writer),
                return_exceptions=True,
            )

    # ------------------------------------------------------------- UDP

    async def _handle_udp_associate(self, reader, writer, client, user=None) -> None:
        loop = asyncio.get_running_loop()
        if self._udp_shared_transport is None or self._udp_shared_transport.is_closing():
            transport, proto = await loop.create_datagram_endpoint(
                lambda: _UdpRelayProtocol(self),
                local_addr=(self.bind_host, self.udp_relay_port),
            )
            self._udp_shared_transport = transport
            self._udp_clients.add(transport)
            self._udp_proto = proto
            log.info("UDP relay datagram socket on %s:%d", self.bind_host, self.udp_relay_port)
        peer = writer.get_extra_info("peername")
        if self._udp_proto is not None and peer and user:
            self._udp_proto.user_by_ip[peer[0]] = user
        sockname = writer.get_extra_info("sockname")
        if self.public_addr:
            # 公网/LAN 部署：BND 回对外可达地址（NAT 后的 sockname 是内网 IP）
            lhost = self.public_addr
        else:
            lhost = sockname[0] if sockname and sockname[0] not in ("0.0.0.0", "") else "127.0.0.1"
        lport = self.udp_relay_port
        writer.write(self._reply_packet(REP_SUCCESS, ATYP_IPV4, bytes(map(int, lhost.split("."))), lport))
        await writer.drain()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
        except (ConnectionError, OSError):
            pass


class _UdpRelayProtocol(asyncio.DatagramProtocol):
    def __init__(self, relay: LightRelay):
        self.relay = relay
        self.transport: asyncio.DatagramTransport | None = None
        self.upstreams: dict[tuple, asyncio.DatagramTransport] = {}
        self.headers: dict[tuple, bytes] = {}
        self.last_seen: dict[tuple, float] = {}   # key -> 单调时钟（janitor 回收依据）
        self._opening: set = set()                # 正在建立 upstream 的 key（防重复创建）
        self._pending_bufs: dict[tuple, list] = {}  # key -> 建立期间缓冲的 payload（防丢包）
        self._clients_seen: set = set()
        self.user_by_ip: dict[str, str] = {}   # client ip -> SOCKS auth username
        self._udp_first_logged: set = set()    # (client, dst) 已打过首包的记录

    def connection_made(self, transport) -> None:
        self.transport = transport
        _udp_buffers(transport)

    def error_received(self, exc) -> None:
        log.debug("udp error: %s", exc)

    def datagram_received(self, data: bytes, addr) -> None:
        if addr not in self._clients_seen:
            self._clients_seen.add(addr)
            log.info("udp client %s:%d (total %d)", addr[0], addr[1], len(self._clients_seen))
        if len(data) < 10:
            return
        if data[2]:  # FRAG != 0: no fragmentation support
            return
        atyp = data[3]
        try:
            if atyp == ATYP_IPV4:
                host = ".".join(str(b) for b in data[4:8])
                port = struct.unpack(">H", data[8:10])[0]
                payload = data[10:]
                header = data[:10]
            elif atyp == ATYP_DOMAIN:
                length = data[4]
                host = data[5 : 5 + length].decode("latin-1")
                port = struct.unpack(">H", data[5 + length : 7 + length])[0]
                payload = data[7 + length :]
                header = data[: 7 + length]
            else:
                return
        except (IndexError, struct.error):
            return

        dst = (host, port)
        client = f"{addr[0]}:{addr[1]}"
        # 诊断：游戏端口段 UDP 首包打印头 32 字节（区分 UE 握手 / 反作弊 RTT）。
        # 同一 (client, dst) 只打一次，防日志爆量。
        if 60000 <= port <= 66000:
            fk = (client, dst)
            if fk not in self._udp_first_logged:
                self._udp_first_logged.add(fk)
                log.info("udp first %s -> %s:%d plen=%d head=%s",
                         client, host, port, len(payload), payload[:32].hex())
        loop = asyncio.get_running_loop()
        key = (addr, dst)
        # RFC 1928: response datagrams carry the *same* address block as the
        # request (tun2socks validates this against the associate BND).
        self.headers[key] = header
        self.last_seen[key] = time.monotonic()
        upstream = self.upstreams.get(key)
        if upstream is None:
            # 建立期间缓冲 payload，避免异步建立窗口内丢包（高频游戏 UDP 会触发）
            self._pending_bufs.setdefault(key, []).append(payload)
            if key not in self._opening:
                self._opening.add(key)
                loop.create_task(self._open_upstream(loop, key, host, port, addr))
        else:
            upstream.sendto(payload)
        # Forward/enqueue the network datagram before touching capture work.
        flow_id = self.relay._flow_key(host, port, "udp", client)
        flow = self.relay.store.open_flow(flow_id, host, port, "udp", client,
                                          user=self.user_by_ip.get(addr[0]))
        if flow is not None:
            flow.append("request", payload)

    async def _open_upstream(self, loop, key, host, port, addr) -> None:
        try:
            _, proto = await loop.create_datagram_endpoint(
                lambda: _UpstreamProtocol(self, key),
                remote_addr=(host, port),
            )
            self.upstreams[key] = proto.transport
            # 建立成功后 flush 缓冲，按到达顺序发送（不丢包）
            bufs = self._pending_bufs.pop(key, [])
            for p in bufs:
                proto.transport.sendto(p)
            log.info("udp upstream %s:%d open (%d active, flush %d)",
                     host, port, len(self.upstreams), len(bufs))
        except OSError as exc:
            log.warning("UDP upstream %s:%d failed: %r", host, port, exc)
            self._pending_bufs.pop(key, None)
        except Exception as exc:
            log.warning("UDP upstream %s:%d unexpected: %r", host, port, exc)
            self._pending_bufs.pop(key, None)
        finally:
            self._opening.discard(key)

    def datagram_received_from_upstream(self, data: bytes, key) -> None:
        self.last_seen[key] = time.monotonic()
        header = self.headers.get(key, b"")
        if self.transport and not self.transport.is_closing():
            self.transport.sendto(header + data, key[0])
        host, port = key[1]
        client = f"{key[0][0]}:{key[0][1]}"
        flow_id = self.relay._flow_key(host, port, "udp", client)
        flow = self.relay.store.open_flow(flow_id, host, port, "udp", client,
                                          user=self.user_by_ip.get(key[0][0]))
        if flow is not None:
            flow.append("response", data)

    def reap(self, ttl: float) -> int:
        """关闭静默超时的 UDP 上游 endpoint，清理其 headers/last_seen 表项。"""
        now = time.monotonic()
        dead = [k for k, tr in self.upstreams.items()
                if now - self.last_seen.get(k, 0.0) > ttl]
        for k in dead:
            tr = self.upstreams.pop(k, None)
            if tr is not None:
                try:
                    tr.close()
                except Exception:
                    pass
            self.headers.pop(k, None)
            self.last_seen.pop(k, None)
            self._pending_bufs.pop(k, None)
        return len(dead)


class _UpstreamProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: _UdpRelayProtocol, key):
        self.owner = owner
        self.key = key
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport
        _udp_buffers(transport)

    def error_received(self, exc) -> None:
        log.debug("upstream udp error for %s: %s", self.key, exc)

    def datagram_received(self, data: bytes, addr) -> None:
        self.owner.datagram_received_from_upstream(data, self.key)


async def periodic_flush(store: CaptureStore, interval: float):
    while True:
        await asyncio.sleep(interval)
        if isinstance(store, QueuedCaptureStore):
            await store.call("flush_all")
        else:
            store.flush_all()


async def relay_latency_monitor(store: QueuedCaptureStore):
    previous = report = time.monotonic()
    maximum = 0.0
    while True:
        await asyncio.sleep(0.05)
        now = time.monotonic()
        maximum = max(maximum, now - previous - 0.05)
        previous = now
        if now - report >= 30.0:
            packets, size = store.pending()
            log.info("relay latency: loop_max_ms=%.2f capture_packets=%d capture_bytes=%d capture_failed=%s",
                     maximum * 1000, packets, size, store._failed)
            report, maximum = now, 0.0


async def capture_pruner(store: CaptureStore, interval: float | None = None):
    """周期空间巡检：边抓包边滚动删除旧 run/旧 flow。

    磁盘写满曾让 relay 连抓包目录都建不出来（crash-loop -> 手机没网），
    转发是主功能，落盘必须自我限幅。

    0829 定版（全场周期性卡顿根因）：巡检绝不能阻塞事件循环——
      a) 全树 rglob+stat 在冷缓存+写争用下要 20-45s（树 22 万文件且持续
         增长），同步跑曾把**所有转发**冻结同样时长 = 每台手机每 5 分钟
         断一次；改 to_thread 后循环永不停摆；
      b) 清理工作在线程中按抓包总配额执行；即使磁盘还有很多空闲，也不会让
         captures 无限增长；
      c) 当前活跃 flow 受保护；只有结束且超过保留时间的 flow 才进入删除队列；
      d) 即使一轮清理暂时不够，也保持抓包开启并让失败流短暂退避重试。"""
    queued = store if isinstance(store, QueuedCaptureStore) else None
    if queued is not None:
        store = queued.storage
    if interval is None:
        interval = CAPTURE_PRUNE_INTERVAL
    event = asyncio.Event()
    store._prune_event = event
    try:
        await asyncio.sleep(5.0)   # 启动后先巡检一次（清历史积压）
        event.set()
        while True:
            try:
                await asyncio.wait_for(event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            event.clear()
            store._prune_requested = False
            if store.enabled and store.root:
                try:
                    protected_flows = (await queued.call("protected") if queued is not None
                                       else tuple(fw.flow_dir for fw in store.flows.values()))
                    n_runs, n_flows, remaining, free = await asyncio.to_thread(
                        _prune_capture_storage,
                        store.root,
                        CAPTURE_MAX_GB * 1e9,
                        CAPTURE_KEEP_RUNS,
                        store.run_dir,
                        protected_flows,
                        CAPTURE_MIN_FREE_GB * 1e9,
                    )
                    if n_runs or n_flows:
                        log.info(
                            "capture pruner: removed %d runs + %d flows "
                            "(capture %.1fGB, free %.1fGB)",
                            n_runs, n_flows, remaining / 1e9, free / 1e9)
                    if _capture_pressure(
                            remaining, free, CAPTURE_MAX_GB * 1e9,
                            CAPTURE_MIN_FREE_GB * 1e9):
                        log.warning(
                            "capture retention still under pressure "
                            "(capture %.1fGB, free %.1fGB); keep writing best-effort",
                            remaining / 1e9, free / 1e9)
                except Exception as exc:
                    log.warning("capture pruner error: %s", exc)
    finally:
        if store._prune_event is event:
            store._prune_event = None


async def janitor(store: CaptureStore, relay: "LightRelay", interval: float | None = None):
    """周期回收：静默流的文件句柄 + 静默 UDP 上游 endpoint（防句柄无限增长）。

    TTL/周期在函数体内按模块全局现读——允许运行时调整（测试也靠这个打补丁）。"""
    if interval is None:
        interval = JANITOR_INTERVAL
    while True:
        await asyncio.sleep(interval)
        try:
            if isinstance(store, QueuedCaptureStore):
                await store.call("reap_idle", FLOW_IDLE_TTL)
            else:
                store.reap_idle(FLOW_IDLE_TTL)
        except Exception as exc:
            log.warning("flow reaper error: %s", exc)
        proto = relay._udp_proto
        if proto is not None:
            try:
                proto.reap(UDP_UPSTREAM_TTL)
            except Exception as exc:
                log.warning("udp reaper error: %s", exc)
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Lightweight SOCKS5 pass-through relay")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=1080)
    ap.add_argument("--capture-dir", default=None,
                    help="抓包根目录（缺省 $ER_ROOT/captures，其次 ./captures）")
    ap.add_argument("--public-addr", default=None,
                    help="对外可达 IP（公网 IP 或局域网 IP；UDP ASSOCIATE 回 BND 用。缺省自动检测局域网出口 IP）")
    ap.add_argument("--require-auth", action="store_true",
                    help="强制 RFC1929 用户名认证（公开部署必开；否则是全网开放代理，被扫到即耗尽 fd）")
    ap.add_argument("--max-conns-per-user", type=int, default=0,
                    help="每个已认证账号并发连接上限（0=不限）；不同账号互不影响")
    ap.add_argument("--preauth-conns-per-ip", type=int, default=PREAUTH_CONN_LIMIT,
                    help="认证完成前的单 IP 握手连接上限（0=不限；默认 128）")
    # 旧 unit 在新代码先启动、unit 后更新时仍能平滑启动；语义映射到账号上限。
    ap.add_argument("--max-conns-per-ip", dest="legacy_max_conns_per_ip",
                    type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--no-capture", action="store_true")
    ap.add_argument("--flush-interval", type=float, default=1.0)
    args = ap.parse_args()
    max_conns_per_user = args.max_conns_per_user
    if args.legacy_max_conns_per_ip is not None:
        if max_conns_per_user and max_conns_per_user != args.legacy_max_conns_per_ip:
            ap.error("--max-conns-per-user 与旧 --max-conns-per-ip 不一致")
        max_conns_per_user = args.legacy_max_conns_per_ip

    capture_dir = args.capture_dir
    if not capture_dir:
        base = os.environ.get("ER_ROOT")
        capture_dir = os.path.join(base, "captures") if base else "captures"

    store = CaptureStore(capture_dir, enabled=not args.no_capture)
    if store.enabled:
        log.info(
            "capture run: %s (rolling prune: max %.1fGB, free reserve %.1fGB, "
            "keep %d runs, flow age %.0fs)",
            store.run_dir, CAPTURE_MAX_GB, CAPTURE_MIN_FREE_GB,
            CAPTURE_KEEP_RUNS, CAPTURE_FLOW_MIN_AGE_SEC)
        # Initial retention scan runs in capture_pruner after the listener starts.
    public_addr = args.public_addr or os.environ.get("ER_PUBLIC_ADDR")
    relay = LightRelay(args.host, args.port, store, public_addr=public_addr,
                       require_auth=args.require_auth,
                       max_conns_per_user=max_conns_per_user,
                       preauth_conns_per_ip=args.preauth_conns_per_ip)
    log.info("UDP BND address: %s:%d (public_addr=%s, detected=%s)",
             relay.public_addr or "auto", relay.udp_relay_port,
             public_addr, relay.public_addr)
    log.info("require_auth=%s max_conns_per_user=%s preauth_conns_per_ip=%s",
             relay.require_auth, relay.max_conns_per_user,
             relay.preauth_conns_per_ip)

    async def amain():
        queued = QueuedCaptureStore(store)
        relay.store = queued
        await relay.start()
        flush_task = asyncio.create_task(periodic_flush(queued, args.flush_interval))
        janitor_task = asyncio.create_task(janitor(queued, relay))
        pruner_task = asyncio.create_task(capture_pruner(queued))
        monitor_task = asyncio.create_task(relay_latency_monitor(queued))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if os.name == "posix":
            loop.add_signal_handler(signal.SIGTERM, stop.set)
        try:
            await stop.wait()
        finally:
            flush_task.cancel()
            janitor_task.cancel()
            pruner_task.cancel()
            monitor_task.cancel()
            await asyncio.gather(flush_task, janitor_task, pruner_task, monitor_task, return_exceptions=True)
            if relay._server is not None:
                relay._server.close()
                await relay._server.wait_closed()
            try:
                await asyncio.wait_for(queued.aclose(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("capture drain timed out during shutdown")

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
