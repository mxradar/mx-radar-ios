# -*- coding: utf-8 -*-
"""nic_capture.py - 本地网卡 UDP 抓包 → light_relay 同构 flow 目录（雷达本地版取数端）。

取数原理：
  PC 开热点（Windows 移动热点 = Microsoft Wi-Fi Direct Virtual Adapter），
  手机连热点直接打游戏——不装 APK、不配代理，游戏流量原路直连；
  Npcap 在热点虚拟网卡上被动抓包，游戏 UDP 载荷按五元组组装成
  light_relay 的 flow 目录（captures/run_*/flows/<n>/{meta.json,request.bin,
  response.bin,frames.jsonl}），er_radar.py --live 原样消费——
  战斗流握手门识别、S2C 解密自解析、增量解析、雷达页全部零改动复用。

与 relay 语义差异（均为本地化适配，不影响下游格式）：
  - 流量来源：SOCKS5 中继取载荷 → 网卡抓帧剥 Eth/IP/UDP 头取载荷（等价）；
  - frames.jsonl 的 ts 用 pcap 包时间戳（原 relay 用 append 时钟）；
  - 默认抓全部 UDP（噪音黑名单除外）：历史对局服务器端口散布 12961-64990，
    端口段白名单会漏局；战斗流分类交给下游握手门（引擎级判据，与 relay
    生产行为一致——relay 也是全量落盘后靠握手门分类）。`--ports` 可选收窄；
  - 不做磁盘配额巡检（--max-gb 显式开启，默认关闭——本地盘别误删历史 run）。

用法（管理员权限，需装 Npcap: https://npcap.com/#download）:
  python src/core/nic_capture.py                 # 自动选热点虚拟网卡
  python src/core/nic_capture.py --list          # 列出全部网卡
  python src/core/nic_capture.py --device 以太网  # 按描述/名字子串选网卡
                                             # （游戏跑在本机时选物理网卡）
配合: python src/core/er_radar.py --live  →  http://127.0.0.1:8888
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src/core"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from light_relay import (CaptureStore, FlowWriter, FLOW_IDLE_TTL,
                         FLOW_PROTECT_SEC, MAX_OPEN_FLOWS, prune_captures)

# ---------------------------------------------------------------- 端口预滤
# 默认不按端口滤（历史对局服务器端口散布 12961-64990，白名单必漏局），
# 黑名单只剔已知噪音：DNS/NTP/DHCP/LLMNR/mDNS/SSDP/WS-Discovery/STUN/QUIC/
# IPSec 等。热点下手机全部 UDP 可见，其余杂流交给下游握手门分类。
EXCLUDE_PORTS = {53, 123, 67, 68, 137, 138, 443, 500, 4500, 5353, 5355,
                 1900, 3702, 3478, 3479, 19302, 5060,
                 65010, 10001, 22000, 23000, 1883, 8011, 8013}

# ---------------------------------------------------------------- 报文解析
DLT_EN10MB = 1
DLT_NULL = 0
DLT_RAW = 101          # 12 (BSD) / 14 旧值同义，Npcap 只报 101
DLT_LINUX_SLL = 113
DLT_LINUX_SLL2 = 276    # tcpdump -i any 在新内核用 SLL2（20 字节头）


def _is_private(ip: str) -> bool:
    """RFC1918/环回/链路本地 判定；非 IPv4 一律按内网处理（下游会丢弃）。"""
    a = ip.split(".")
    if len(a) != 4:
        return True
    try:
        x, y = int(a[0]), int(a[1])
    except ValueError:
        return True
    return (x in (0, 10, 127) or (x == 172 and 16 <= y <= 31)
            or (x == 192 and y == 168) or (x == 169 and y == 254))


def parse_udp_frame(dlt: int, raw: bytes):
    """剥链路层 + IPv4 + UDP 头，返回 (src, sport, dst, dport, payload)。

    非 UDP / IPv6 / IP 分片 / 空载荷返回 None（分片报文载荷不完整，进管线
    会污染 bunch 流——relay 场景 tun2socks 已按完整数据报重组，这里等价拦截）。
    """
    if dlt == DLT_EN10MB:
        if len(raw) < 14:
            return None
        et = int.from_bytes(raw[12:14], "big")
        off = 14
        while et in (0x8100, 0x88A8):            # VLAN / Q-in-Q
            if len(raw) < off + 4:
                return None
            et = int.from_bytes(raw[off + 2:off + 4], "big")
            off += 4
        if et != 0x0800:
            return None
        ip = raw[off:]
    elif dlt in (DLT_RAW, 12, 14):
        ip = raw
    elif dlt == DLT_NULL:
        if len(raw) < 4:
            return None
        ip = raw[4:]
    elif dlt == DLT_LINUX_SLL:
        if len(raw) < 16:
            return None
        ip = raw[16:]
    elif dlt == DLT_LINUX_SLL2:
        # SLL2：20 字节头，协议号在头 2 字节，IP 从 20 起
        if len(raw) < 20:
            return None
        ip = raw[20:]
    else:
        return None
    if len(ip) < 20 or ip[0] >> 4 != 4:
        return None
    ihl = (ip[0] & 0xF) * 4
    if ihl < 20 or len(ip) < ihl:
        return None
    frag = int.from_bytes(ip[6:8], "big")
    if frag & 0x3FFF:                            # MF=1 或 frag_offset>0
        return None
    if ip[9] != 17:                              # proto != UDP
        return None
    u = ip[ihl:]
    if len(u) < 8:
        return None
    sp, dp, ln = struct.unpack_from(">HHH", u, 0)
    payload = u[8:max(ln, 8)]                    # ln 含 8B UDP 头
    src = ".".join(str(b) for b in ip[12:16])
    dst = ".".join(str(b) for b in ip[16:20])
    return src, sp, dst, dp, payload


def parse_ports(spec: str) -> set[int]:
    """'60000-66000,20000' -> set[int]。"""
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    return out


# ------------------------------------------------- 软路由直采（TCP 收 pcap 流）
# 拓扑：手机 → 软路由(中继) → 外网。中继器本来就在链路上，所以**不需要镜像**：
# 直接在它上面抓、把抓到的流回传即可。路由器侧不装我们的任何程序，一条命令：
#     tcpdump -i <手机所在口> -U -w - udp | nc <本机IP> <监听端口>
# 这里只把 pcap 流还原成五元组，喂给与网卡抓包**同一个** FlowAssembler，落盘格式
# 因此完全一致 —— 雷达侧一行都不用改，本机也不需要 Npcap。
# 键 = 首 4 字节按**小端**解读的值（标准微秒文件在盘上是 d4 c3 b2 a1）
# 值 = (struct 前缀, int.from_bytes 用的字节序名, 秒/单位)。两个名字都要留：
# struct 只认 "<"/">"，int.from_bytes 只认 "little"/"big" —— 混用过一次，踩了。
PCAP_MAGIC = {
    0xa1b2c3d4: ("<", "little", 1e-6),
    0xd4c3b2a1: (">", "big", 1e-6),
    0xa1b23c4d: ("<", "little", 1e-9),
    0x4d3cb2a1: (">", "big", 1e-9),
}
PCAP_HEADER_LEN = 24
PCAP_RECORD_LEN = 16
PCAP_MAX_FRAME = 0x40000     # 单帧上限；超过它基本可判定流错位，断开让对端重连


def _local_ips():
    """本机非环回 IPv4（只用于启动横幅里提示软路由该填哪个地址）。"""
    ips = []
    try:
        for row in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = row[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def _recv_exact(sock: socket.socket, n: int):
    """收满 n 字节；对端关闭或出错返回 None。"""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _recv_up_to(sock: socket.socket, n: int) -> bytes:
    """尽力收 n 字节，返回实际收到的（对端关闭/出错就提前返回，不报错）。"""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _serve_pcap_stream(conn, asm, flush_iv: float = 1.0 / 60.0):
    """一条连接的 pcap 流 → 帧 → asm.feed。返回 (n_seen, n_udp, n_bad, 结束原因)。"""
    n_seen = n_udp = n_bad = 0
    head = _recv_up_to(conn, PCAP_HEADER_LEN)
    if len(head) < PCAP_HEADER_LEN:
        # 单独区分「一个字都没发」和「发了但不够 24 字节」：后者通常是把 HTTP
        # 或别的东西连过来了，提示要具体才有用。
        return 0, 0, 0, ("对端没发数据就断开" if not head
                         else "首包只有 %d 字节（pcap 头需 %d）" % (len(head), PCAP_HEADER_LEN))
    magic = int.from_bytes(head[0:4], "little")
    if magic not in PCAP_MAGIC:
        return 0, 0, 0, "不是 pcap 流（首 4 字节 %s）" % head[:4].hex()
    endian, order, tscale = PCAP_MAGIC[magic]
    dlt = int.from_bytes(head[20:24], order)
    last_flush = time.time()
    while True:
        rec = _recv_exact(conn, PCAP_RECORD_LEN)
        if rec is None:
            return n_seen, n_udp, n_bad, "对端断开"
        _ts_s, _ts_f, caplen, _origlen = struct.unpack(endian + "IIII", rec)
        if caplen > PCAP_MAX_FRAME:
            return n_seen, n_udp, n_bad, "帧长异常(%d)，判定流错位" % caplen
        raw = _recv_exact(conn, caplen)
        if raw is None:
            return n_seen, n_udp, n_bad, "读帧数据时对端断开"
        n_seen += 1
        u = parse_udp_frame(dlt, raw)
        if u is None:
            n_bad += 1
        else:
            n_udp += 1
            # ts 用 pcap 记录里的时间戳（= 软路由抓到包的时刻），不是本机收到的时刻：
            # 下游按时间对齐的地方（两流配对、回放游标）都依赖它。
            asm.feed(*u, ts=_ts_s + _ts_f * tscale)
        now = time.time()
        if now - last_flush >= flush_iv:
            last_flush = now
            asm.store.flush_all()


def run_stream_listener(args) -> int:
    """软路由直采模式主循环：监听 → 收 pcap 流 → 落成 flow 目录。"""
    server_ports = parse_ports(args.ports) if args.ports else None
    capture_dir = args.capture_dir or str(ROOT / "captures")
    store = LocalStore(capture_dir,
                       "tcp-listen %s:%d (软路由直采)" % (args.listen_host, args.listen_port),
                       "all(noise-excluded)" if server_ports is None else args.ports)
    if not store.enabled:
        print("[错误] 抓包目录不可写: %s" % capture_dir, flush=True)
        return 1
    asm = FlowAssembler(store, server_ports)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((args.listen_host, args.listen_port))
    except OSError as exc:
        print("[错误] 监听 %s:%d 失败: %s" % (args.listen_host, args.listen_port, exc),
              flush=True)
        print("      端口被占用？换个端口，或先关掉旧实例。", flush=True)
        return 1
    srv.listen(4)

    print("=== 软路由直采 (er 本地版) ===", flush=True)
    print("  监听: %s:%d（等软路由把 pcap 流推过来）"
          % (args.listen_host, args.listen_port), flush=True)
    print("  抓包 run: %s" % store.run_dir, flush=True)
    ips = _local_ips()
    if ips:
        print("  在软路由上执行（<手机所在口> 换成它的 LAN/AP 接口）:", flush=True)
        for ip in ips:
            print("    tcpdump -i <手机所在口> -U -w - udp | nc %s %d"
                  % (ip, args.listen_port), flush=True)
    print("  雷达页: http://127.0.0.1:8888   Ctrl+C 停止", flush=True)

    running = True

    def janitor():
        last_prune = 0.0
        while running:
            time.sleep(15)
            try:
                store.reap_idle(FLOW_IDLE_TTL)
                if args.max_gb > 0 and time.time() - last_prune > 300:
                    last_prune = time.time()
                    n = prune_captures(capture_dir, args.max_gb * 1e9)
                    if n:
                        print("[prune] 删除 %d 个最旧 run" % n, flush=True)
            except Exception as exc:
                print("[janitor] %s" % exc, flush=True)

    threading.Thread(target=janitor, daemon=True).start()

    while True:
        try:
            conn, peer = srv.accept()
        except OSError as exc:
            print("[错误] accept: %s" % exc, flush=True)
            return 0
        print("[直采] 已连接 %s:%d" % (peer[0], peer[1]), flush=True)
        seen = udp = bad = 0
        why = ""
        try:
            conn.settimeout(60.0)     # 只是防完全卡死；tcpdump 一直在发就不会触发
            seen, udp, bad, why = _serve_pcap_stream(conn, asm)
        except OSError as exc:
            why = "连接异常: %s" % exc
        finally:
            try:
                conn.close()
            except OSError:
                pass
            store.flush_all()
        print("[直采] %s:%d 断开 —— 本次 %d 帧 / %d udp（非 UDP %d），%s"
              % (peer[0], peer[1], seen, udp, bad, why or "对端结束"), flush=True)


# ---------------------------------------------------------------- 落盘
class TsFlowWriter(FlowWriter):
    """light_relay.FlowWriter：append 接受显式时间戳（pcap 包时间戳）。

    frames.jsonl 的 ts 是回放 seq→sec 拟合与 live 脉冲的时间源，抓帧时钟比
    append 时钟更贴近 wire truth（突发窗口内不再被压成同一毫秒）。格式与
    relay 完全同构，下游零感知。"""

    def append(self, direction: str, data: bytes, ts: float | None = None) -> None:
        if not data or self.closed:
            return
        self.last_activity = time.monotonic()
        seq = self._seq
        self._seq += 1
        if ts is None:
            ts = time.time()
        fh = self._req if direction == "request" else self._resp
        try:
            offset = fh.tell()
            fh.write(data)
            self._frames.write(
                json.dumps({"seq": seq, "ts": round(ts, 6), "dir": direction,
                            "offset": offset, "length": len(data)},
                           separators=(",", ":")) + "\n")
        except OSError:
            if not self.closed:
                print(f"[capture] 写盘失败 flow={self.flow_id}，停写该流", flush=True)
                self.closed = True


class AdoptedFlowWriter(TsFlowWriter):
    """续写已存在的 flow 目录（跨抓包进程重启保留握手/身份/雷达跟随）。

    局中重启抓包后，游戏 socket 不重握手，新建目录会得到一条没有
    Hello/Welcome 的"半截流"——引擎门永远不认。续写旧目录则下游
    （flowlive tail-follow、LiveView、self 判定）全部无感。
    文件以 append 打开；seq 与 offset 从既有内容恢复；若上一进程被杀
    留下无换行的尾行，先补一个换行再追加，避免拼接毁行。"""

    def __init__(self, flow_dir: str, flow_id: str, meta: dict):
        self.flow_dir = flow_dir
        self.flow_id = flow_id
        self.meta = meta
        self.closed = False
        self.last_activity = time.monotonic()
        self._req = open(os.path.join(flow_dir, "request.bin"), "ab")
        self._resp = open(os.path.join(flow_dir, "response.bin"), "ab")
        self._frames = open(os.path.join(flow_dir, "frames.jsonl"),
                            "a", encoding="utf-8")
        fj = os.path.join(flow_dir, "frames.jsonl")
        size = os.path.getsize(fj)
        if size:
            with open(fj, "rb") as fh:
                fh.seek(size - 1)
                if fh.read(1) != b"\n":            # 被杀进程的无换行尾行
                    self._frames.write("\n")
                fh.seek(max(0, size - 65536))
                tail = fh.read().splitlines()
            for line in reversed(tail):
                try:
                    self._seq = json.loads(line)["seq"] + 1
                    break
                except Exception:
                    continue


class LocalStore(CaptureStore):
    """mode=nic_capture 的抓包库。run meta 记录网卡与端口过滤便于事后追溯；
    open_flow 与 light_relay 同语义（TTL 回收后同 flow_id 重开新目录）。"""

    def __init__(self, root: str, device_desc: str, port_spec: str):
        super().__init__(root, True)
        self.device_desc = device_desc
        self.port_spec = port_spec
        if self.run_dir:
            meta = {"created": time.time(),
                    "created_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                    # mode 记**实际**采集方式：镜像模式（软路由把流量镜像到本机网卡）
                    # 与热点模式在事后排查时完全不是一回事，两者都走这个 worker，
                    # 只能靠编排进程通过 ER_CAP_MODE 区分（缺省仍是 nic_capture）。
                    "run_id": Path(self.run_dir).name,
                    "mode": (os.environ.get("ER_CAP_MODE") or "nic_capture"),
                    "device": device_desc, "server_ports": port_spec}
            try:
                (Path(self.run_dir) / "meta.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass

    def open_flow(self, flow_id: str, host: str, port: int, proto: str, client: str,
                  user: str | None = None) -> TsFlowWriter | None:
        fw = self.flows.get(flow_id)
        if fw is not None and not fw.closed:
            return fw
        # 跨重启续写：同 5 元组在既往 run 里有 15min 内活跃的目录 → 接着写
        # （游戏 socket 不重握手，新建目录会产出引擎门不认的半截流）
        fw = self._adopt(flow_id)
        if fw is not None:
            self.flows[flow_id] = fw
            print(f"[capture] 续写旧流 {flow_id} -> {fw.flow_dir}", flush=True)
            return fw
        if self._count_open() >= MAX_OPEN_FLOWS:
            self.reap_idle(FLOW_PROTECT_SEC)
            if self._count_open() >= MAX_OPEN_FLOWS:
                return None
        while True:
            # 跳过已存在的目录（同秒重启撞 run / 残留目录）：绝无截断
            flow_dir = os.path.join(self.run_dir, "flows", str(self._dir_seq))
            self._dir_seq += 1
            if not os.path.exists(flow_dir):
                break
        os.makedirs(flow_dir, exist_ok=True)
        fw = TsFlowWriter(flow_dir, flow_id,
                          {"flow_id": flow_id, "host": host, "port": port,
                           "proto": proto, "client": client, "created": time.time()})
        self.flows[flow_id] = fw
        return fw

    def _adopt(self, flow_id: str) -> TsFlowWriter | None:
        """找同 flow_id 且仍新鲜（<15min）的既有目录续写（含当前 run——
        那是死进程的遗产；本进程活流都在 self.flows，不会被误接管）。

        索引缓存 30s：新流建立的高峰期（进局瞬间几十条杂流）不做全盘扫描。"""
        now = time.time()
        idx = getattr(self, "_adopt_index", None)
        if idx is None or now - self._adopt_index_at > 30:
            idx = {}
            root = Path(self.root)
            runs = sorted((d for d in root.glob("run_*") if d.is_dir()),
                          key=lambda d: d.stat().st_mtime, reverse=True)
            for run in runs[:4]:
                flows = run / "flows"
                if not flows.is_dir():
                    continue
                for fd in flows.iterdir():
                    meta, fj = fd / "meta.json", fd / "frames.jsonl"
                    try:
                        if not meta.is_file() or not fj.is_file():
                            continue
                        mt = fj.stat().st_mtime
                        if now - mt > 900:
                            continue
                        fid = json.loads(meta.read_text(encoding="utf-8")).get("flow_id")
                        # 同 flow_id 多目录（游戏中换服/多次重开）取 mtime 最新
                        if fid and (fid not in idx or mt > idx[fid][1]):
                            idx[fid] = (fd, mt)
                    except (OSError, ValueError):
                        continue
            self._adopt_index = idx
            self._adopt_index_at = now
        hit = idx.get(flow_id)
        if not hit:
            return None
        fd, _mt = hit
        try:
            meta = json.loads((fd / "meta.json").read_text(encoding="utf-8"))
            return AdoptedFlowWriter(str(fd), flow_id, meta)
        except (OSError, ValueError):
            return None


class FlowAssembler:
    """UDP 载荷 -> 方向判定 -> flow 目录。方向判定与 pcap_to_flows 同源：
    私网一端是客户端、公网一端是服务器（热点/本机两种部署都成立：
    手机=私网热点侧，游戏服=公网；本机游戏=服务器公网 vs 本机私网地址）。"""

    def __init__(self, store: LocalStore, server_ports: set[int] | None,
                 exclude: bool = True):
        self.store = store
        self.server_ports = server_ports
        self.exclude = EXCLUDE_PORTS if exclude else set()
        self.seen: set[str] = set()
        self.n_req = 0
        self.n_resp = 0

    def feed(self, src: str, sport: int, dst: str, dport: int, payload: bytes,
             ts: float | None = None) -> str | None:
        if not payload:
            return None
        sp, dp = _is_private(src), _is_private(dst)
        if sp and not dp:
            direction, server, client = "request", (dst, dport), (src, sport)
        elif dp and not sp:
            direction, server, client = "response", (src, sport), (dst, dport)
        else:
            return None            # 双私(手机↔PC/网关)或双公(隧道)均非战斗流量
        host, port = server
        if int(host.split(".")[0]) >= 224:       # 组播/广播
            return None
        if self.server_ports is not None:
            if port not in self.server_ports:
                return None
        elif port in self.exclude:
            return None
        flow_id = f"{client[0]}:{client[1]}->{host}:{port}/udp"
        if flow_id not in self.seen:
            self.seen.add(flow_id)
            print(f"[flow] #{len(self.seen)} {client[0]}:{client[1]} -> "
                  f"{host}:{port}", flush=True)
        fw = self.store.open_flow(flow_id, host, port, "udp",
                                  f"{client[0]}:{client[1]}")
        if fw is None:
            return None
        fw.append(direction, payload, ts=ts)
        if direction == "request":
            self.n_req += 1
        else:
            self.n_resp += 1
        return direction


# ---------------------------------------------------------------- Npcap 绑定
class _PcapPkthdr(ctypes.Structure):
    # Win64: timeval 的 long 是 4 字节（LLP64），与 hotspot_capture.js 注释一致
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long),
                ("caplen", ctypes.c_uint32), ("len", ctypes.c_uint32)]


class _Sockaddr(ctypes.Structure):
    _fields_ = [("sa_family", ctypes.c_ushort), ("sa_data", ctypes.c_ubyte * 14)]


class _PcapAddr(ctypes.Structure):
    _fields_ = [("next", ctypes.c_void_p), ("addr", ctypes.POINTER(_Sockaddr)),
                ("netmask", ctypes.c_void_p), ("broadaddr", ctypes.c_void_p),
                ("dstaddr", ctypes.c_void_p)]


class _PcapIf(ctypes.Structure):
    _fields_ = [("next", ctypes.c_void_p), ("name", ctypes.c_char_p),
                ("description", ctypes.c_char_p),
                ("addresses", ctypes.POINTER(_PcapAddr)), ("flags", ctypes.c_uint32)]


class _BpfInsn(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8),
                ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _BpfProgram(ctypes.Structure):
    _fields_ = [("bf_len", ctypes.c_uint32),
                ("bf_insns", ctypes.POINTER(_BpfInsn))]


WPCAP_CANDIDATES = (
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "wpcap.dll",
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "Npcap" / "wpcap.dll",
)
PCAP_NETMASK_UNKNOWN = 0xFFFFFFFF


def load_wpcap():
    """加载 wpcap.dll（兼容 WinPcap 兼容模式与 Npcap 默认目录布局）。"""
    for p in WPCAP_CANDIDATES:
        if not p.exists():
            continue
        try:
            if p.parent.name == "Npcap":     # 依赖 Packet.dll 同目录
                os.add_dll_directory(str(p.parent))
            return ctypes.CDLL(str(p))
        except OSError as exc:
            print(f"[错误] wpcap.dll 加载失败 {p}: {exc}")
            return None
    print("[错误] 未检测到 Npcap（缺少 wpcap.dll）")
    print("      请先安装: https://npcap.com/#download")
    return None


def list_devices(lib) -> list[tuple[str, str, list[str]]]:
    """[(name, description, [ip...])]。失败抛 RuntimeError。"""
    alldevs = ctypes.POINTER(_PcapIf)()
    errbuf = ctypes.create_string_buffer(256)
    if lib.pcap_findalldevs(ctypes.byref(alldevs), errbuf) != 0:
        raise RuntimeError(errbuf.value.decode("latin-1") or "pcap_findalldevs")
    out = []
    cur = alldevs
    while cur and cur.contents:
        c = cur.contents
        ips = []
        addr = c.addresses
        while addr and addr.contents:
            sa = addr.contents.addr
            if sa and sa.contents.sa_family == 2:      # AF_INET
                b = sa.contents.sa_data
                ips.append(".".join(str(x) for x in b[2:6]))
            addr = (ctypes.cast(addr.contents.next, ctypes.POINTER(_PcapAddr))
                    if addr.contents.next else None)
        out.append((c.name.decode("latin-1"),
                    (c.description or b"").decode("latin-1", "replace"), ips))
        nxt = c.next
        cur = ctypes.cast(nxt, ctypes.POINTER(_PcapIf)) if nxt else None
    lib.pcap_freealldevs(alldevs)
    return out


def print_devices(devs):
    for i, (name, desc, ips) in enumerate(devs):
        print(f"  [{i}] {name}")
        if desc:
            print(f"      {desc}")
        if ips:
            print(f"      IP: {', '.join(ips)}")


def pick_device(devs, arg: str | None):
    """自动选网卡：Wi-Fi Direct（热点虚拟网卡）优先 #2（通常带流量的那个）。"""
    if arg:
        arg_l = arg.lower()
        for name, desc, _ips in devs:
            if arg == name or arg_l in name.lower() or arg_l in desc.lower():
                return name, desc
        raise SystemExit(f"[错误] 找不到匹配 '{arg}' 的网卡（--list 查看）")
    cands = [(n, d) for n, d, _i in devs if "wi-fi direct" in d.lower()
             or "wi-fi direct" in n.lower()]
    if not cands:
        print("[错误] 未找到热点虚拟网卡 (Microsoft Wi-Fi Direct Virtual Adapter)")
        print("      请确认: 1) 已开启 Windows 移动热点  2) 手机已连接热点")
        print("      （游戏跑在本机时请 --device 指定物理网卡）可用网卡:")
        print_devices(devs)
        raise SystemExit(1)
    cands.sort(key=lambda t: -("#2" in t[1]))
    return cands[0]


# ---------------------------------------------------------------- 主循环
def main() -> int:
    ap = argparse.ArgumentParser(description="本地网卡 UDP 抓包 → flow 目录（雷达本地版）")
    ap.add_argument("--device", default=None,
                    help="网卡名/描述子串（缺省自动选 Wi-Fi Direct 热点虚拟网卡）")
    ap.add_argument("--list", action="store_true", help="列出网卡后退出")
    ap.add_argument("--capture-dir", default=None,
                    help="抓包根目录（缺省 ER_ROOT/captures）")
    ap.add_argument("--ports", default=None,
                    help="服务器端口白名单（'a-b,c-d' 多段；缺省不滤端口，"
                         "只套噪音黑名单——历史战斗服端口 12961-64990，段过滤会漏局）")
    ap.add_argument("--max-gb", type=float, default=0.0,
                    help="抓包目录配额 GB（超量删最旧 run；0=不巡检）")
    ap.add_argument("--listen-port", type=int, default=0,
                    help="软路由直采：在本机监听该 TCP 端口，接收软路由 "
                         "`tcpdump -w -` 推来的 pcap 流。给了它就不开本机网卡、"
                         "也不需要 Npcap。")
    ap.add_argument("--listen-host", default="0.0.0.0",
                    help="软路由直采的监听地址（缺省 0.0.0.0）")
    ap.add_argument("--snaplen", type=int, default=65535)
    ap.add_argument("--to-ms", type=int, default=100, help="pcap 读超时 ms")
    args = ap.parse_args()

    # 软路由直采走 TCP 收流，完全不碰网卡/Npcap —— 所以在 load_wpcap 之前分叉，
    # 没装 Npcap 的机器也能用这个模式。
    if args.listen_port:
        if not 1024 <= args.listen_port <= 65535:
            print("[错误] 监听端口需在 1024-65535")
            return 1
        return run_stream_listener(args)

    lib = load_wpcap()
    if lib is None:
        return 1
    for fn, restype, argt in (
            ("pcap_findalldevs", ctypes.c_int,
             [ctypes.POINTER(ctypes.POINTER(_PcapIf)), ctypes.c_char_p]),
            ("pcap_freealldevs", None, [ctypes.POINTER(_PcapIf)]),
            ("pcap_open_live", ctypes.c_void_p,
             [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
              ctypes.c_char_p]),
            ("pcap_next_ex", ctypes.c_int,
             [ctypes.c_void_p,
              ctypes.POINTER(ctypes.POINTER(_PcapPkthdr)),
              ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]),
            ("pcap_close", None, [ctypes.c_void_p]),
            ("pcap_geterr", ctypes.c_char_p, [ctypes.c_void_p]),
            ("pcap_datalink", ctypes.c_int, [ctypes.c_void_p]),
            ("pcap_compile", ctypes.c_int,
             [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
              ctypes.c_uint32]),
            ("pcap_setfilter", ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p]),
            ("pcap_freecode", None, [ctypes.c_void_p]),
    ):
        f = getattr(lib, fn)
        f.restype = restype
        f.argtypes = argt

    try:
        devs = list_devices(lib)
    except RuntimeError as exc:
        print(f"[错误] 枚举网卡失败: {exc}")
        return 1
    if args.list:
        print_devices(devs)
        return 0

    device, desc = pick_device(devs, args.device)
    server_ports = parse_ports(args.ports) if args.ports else None
    capture_dir = args.capture_dir or str(ROOT / "captures")

    store = LocalStore(capture_dir, f"{desc} ({device})",
                       "all(noise-excluded)" if server_ports is None else args.ports)
    if not store.enabled:
        print(f"[错误] 抓包目录不可写: {capture_dir}")
        return 1
    asm = FlowAssembler(store, server_ports)

    errbuf = ctypes.create_string_buffer(256)
    handle = lib.pcap_open_live(device.encode("latin-1"), args.snaplen, 1,
                                args.to_ms, errbuf)
    if not handle:
        err = errbuf.value.decode("latin-1", "replace")
        print(f"[错误] 打开网卡失败: {err}")
        print("      请以管理员身份运行（Npcap 默认仅管理员可抓包）")
        return 1
    dlt = lib.pcap_datalink(handle)
    prog = _BpfProgram()
    if lib.pcap_compile(handle, ctypes.byref(prog), b"udp", 1,
                        PCAP_NETMASK_UNKNOWN) == 0:
        if lib.pcap_setfilter(handle, ctypes.byref(prog)) != 0:
            print(f"[警告] BPF 过滤器设置失败: "
                  f"{lib.pcap_geterr(handle).decode('latin-1', 'replace')}（纯 Python 过滤）")
        lib.pcap_freecode(ctypes.byref(prog))
    else:
        print("[警告] BPF 编译失败（纯 Python 过滤）")

    print("=== 本地网卡抓包 (er 本地版) ===")
    print(f"  网卡: {desc} ({device}) dlt={dlt}")
    print(f"  服务器端口: {'全部(黑名单滤噪)' if server_ports is None else args.ports}")
    print(f"  抓包 run: {store.run_dir}")
    # 提示跟着"这个树里有什么"走：只有解析器的产物里没有 er_radar.py，
    # 别让人去找一个不存在的文件。
    if (ROOT / "src" / "core" / "er_radar.py").is_file():
        print(f"  请另开窗口运行: python src/core/er_radar.py --live")
        print(f"  雷达页: http://127.0.0.1:8888   Ctrl+C 停止抓包")
    else:
        print(f"  解析: python parse_flows.py {capture_dir}")
        print(f"        python parse_flows.py {capture_dir} --serve   # 起 HTTP 给前端轮询")
        print(f"  Ctrl+C 停止抓包")

    running = True

    def janitor():
        last_prune = 0.0
        while running:
            time.sleep(15)
            try:
                store.reap_idle(FLOW_IDLE_TTL)
                if args.max_gb > 0 and time.time() - last_prune > 300:
                    last_prune = time.time()
                    n = prune_captures(capture_dir, args.max_gb * 1e9)
                    if n:
                        print(f"[prune] 删除 {n} 个最旧 run", flush=True)
            except Exception as exc:
                print(f"[janitor] {exc}", flush=True)

    threading.Thread(target=janitor, daemon=True).start()

    hdr = ctypes.POINTER(_PcapPkthdr)()
    data = ctypes.POINTER(ctypes.c_ubyte)()
    n_seen = n_udp = n_bad = 0
    t0 = time.time()
    last_stat = t0
    last_flush = t0
    try:
        while True:
            r = lib.pcap_next_ex(handle, ctypes.byref(hdr), ctypes.byref(data))
            if r == 1:
                n_seen += 1
                h = hdr.contents
                ts = h.tv_sec + h.tv_usec / 1e6
                raw = ctypes.string_at(data, h.caplen)
                u = parse_udp_frame(dlt, raw)
                if u is None:
                    n_bad += 1
                else:
                    n_udp += 1
                    asm.feed(*u, ts=ts)
            elif r == 0:
                pass                                   # 超时，继续轮询
            elif r == -2:
                break
            else:
                err = lib.pcap_geterr(handle)
                print(f"[错误] pcap_next_ex: "
                      f"{err.decode('latin-1', 'replace') if err else 'unknown'}")
                break
            now = time.time()
            # 本地版 60Hz：flush 节拍 1/60s（云版 relay 1s 是解码可见延迟闸门，
            # 单机直连没必要）。FlowWriter.flush 顺序 req→resp→frames 保证了
            # 「见 frames 新行时字节必已落盘」的下游依赖，任何节拍下都成立。
            if now - last_flush >= 1.0 / 60.0:
                last_flush = now
                store.flush_all()
            if now - last_stat >= 5:
                last_stat = now
                mb = sum(fw._resp.tell() + fw._req.tell()
                         for fw in store.flows.values() if not fw.closed) / 1e6
                print(f"[{now - t0:6.0f}s] 帧={n_seen} udp={n_udp} "
                      f"req={asm.n_req} resp={asm.n_resp} 流={len(asm.seen)} "
                      f"buf={mb:.1f}MB", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        store.flush_all()
        store.close()
        lib.pcap_close(handle)
    print(f"抓包停止: 帧={n_seen} udp={n_udp} req={asm.n_req} resp={asm.n_resp} "
          f"流={len(asm.seen)} run={store.run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
