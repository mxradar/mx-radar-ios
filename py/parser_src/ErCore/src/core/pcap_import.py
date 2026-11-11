# -*- coding: utf-8 -*-
"""pcap_import.py —— 把 pcap / pcapng 拆成 flow 目录（解析引擎的输入格式）。

解析引擎吃的是「一条连接一个目录」的 flow 目录，而买家手上常见的是
tcpdump / Wireshark 抓下来的 pcap 文件。本模块把 pcap 按五元组切开、
恢复成同一套目录结构，之后 `parse_flows.py` 原样就能解。

    <输出根>/run_pcap_<时间戳>/flows/<n>/
        meta.json       连接元信息（五元组、抓包时间）
        request.bin     客户端 → 服务端 载荷，按抓到的顺序首尾相接
        response.bin    服务端 → 客户端 载荷，同上
        frames.jsonl    {"seq","ts","dir","offset","length"} 逐帧索引

方向判定：一条连接里**时间最早的那个包的方向算 request**（对局流由客户端先发起），
反方向即 response。抓包从头开始就能正确分向；半路开始的流也能解，只是该流可能
缺握手帧而无法被认成对局流。

支持链路层：以太网（含 VLAN）、Linux cooked v1/v2（tcpdump -i any 用 SLL2）、
NULL/LOOP、RAW IP。只取 UDP；IPv6、分片报文、非 UDP 一律跳过（分片载荷不完整，
进管线会污染组包）。
"""

from __future__ import annotations

import json
import os
import struct
import time
from pathlib import Path

DLT_EN10MB = 1
DLT_NULL = 0
DLT_RAW = 101
DLT_LINUX_SLL = 113
DLT_LINUX_SLL2 = 276

# 文件头 magic -> (字节序, 时间戳单位)
PCAP_MAGIC = {
    0xA1B2C3D4: ("<", 1e-6),
    0xD4C3B2A1: (">", 1e-6),
    0xA1B23C4D: ("<", 1e-9),
    0x4D3CB2A1: (">", 1e-9),
}
PCAPNG_MAGIC = b"\x0a\x0d\x0d\x0a"


def parse_udp_frame(dlt: int, raw: bytes):
    """剥链路层 + IPv4 + UDP 头，返回 (src, sport, dst, dport, payload)；不适用返回 None。"""
    if dlt == DLT_EN10MB:
        if len(raw) < 14:
            return None
        et = int.from_bytes(raw[12:14], "big")
        off = 14
        while et in (0x8100, 0x88A8):                 # VLAN / Q-in-Q
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
    if int.from_bytes(ip[6:8], "big") & 0x3FFF:        # 分片
        return None
    if ip[9] != 17:                                    # 非 UDP
        return None
    u = ip[ihl:]
    if len(u) < 8:
        return None
    sp, dp, ln = struct.unpack_from(">HHH", u, 0)
    payload = u[8:max(ln, 8)]
    src = ".".join(str(b) for b in ip[12:16])
    dst = ".".join(str(b) for b in ip[16:20])
    return src, sp, dst, dp, payload


def iter_records(path):
    """逐条产出 (时间戳秒, 链路层类型, 报文数据)。pcap 与 pcapng 都认。"""
    with open(path, "rb") as f:
        head = f.read(4)
        if not head:
            return
        if head == PCAPNG_MAGIC:
            f.seek(0)                                # 块解析要整块头，从头读
            yield from _iter_pcapng(f)
            return
        f.seek(0)
        yield from _iter_pcap(f)


def _iter_pcap(f):
    hdr = f.read(24)
    if len(hdr) < 24:
        return
    magic = int.from_bytes(hdr[:4], "little")
    if magic not in PCAP_MAGIC:
        magic = int.from_bytes(hdr[:4], "big")
    endian, tscale = PCAP_MAGIC.get(magic, ("<", 1e-6))
    dlt = struct.unpack(endian + "I", hdr[20:24])[0]
    rec = struct.Struct(endian + "IIII")
    while True:
        b = f.read(16)
        if len(b) < 16:
            return
        ts_s, ts_u, incl, orig = rec.unpack(b)
        data = f.read(incl)
        if len(data) < incl:
            return
        yield ts_s + ts_u * tscale, dlt, data


def _iter_pcapng(f):
    """pcapng：SHB 定字节序，IDB 定链路类型/时间精度，EPB/SPB 出包。"""
    endian = "<"
    linktypes = []
    tsresol = []
    while True:
        b = f.read(8)
        if len(b) < 8:
            return
        if b[:4] == PCAPNG_MAGIC:
            bom = f.read(4)
            if len(bom) < 4:
                return
            endian = "<" if bom == b"\x4d\x3c\x2b\x1a" else ">"
            blen = struct.unpack(endian + "I", b[4:8])[0]
            if blen < 12:
                return
            f.read(blen - 12)                        # 已读 头8 + BOM4
            continue
        btype, blen = struct.unpack(endian + "II", b)
        if blen < 12:
            return
        body = f.read(blen - 12)
        f.read(4)                                    # 尾部长度
        if len(body) < blen - 12:
            return
        if btype == 1:                               # IDB
            lt = struct.unpack_from(endian + "H", body, 0)[0]
            linktypes.append(lt)
            resol = 1e-6
            off = 8
            while off + 4 <= len(body):
                code, olen = struct.unpack_from(endian + "HH", body, off)
                val = body[off + 4:off + 4 + olen]
                if code == 9 and val:                # if_tsresol
                    n = val[0]
                    resol = 10.0 ** (-n) if n < 128 else 2.0 ** (-(n - 128))
                off += 4 + ((olen + 3) // 4) * 4
            tsresol.append(resol)
        elif btype == 6:                             # EPB
            iface, tsh, tsl, caplen = struct.unpack_from(endian + "IIII", body, 0)
            data = body[20:20 + caplen]
            r = tsresol[iface] if iface < len(tsresol) else 1e-6
            lt = linktypes[iface] if iface < len(linktypes) else DLT_EN10MB
            ts = ((tsh << 32) | tsl) * r
            yield ts, lt, data
        elif btype == 3:                             # SPB（简单包，用 iface 0）
            if len(body) < 4:
                continue
            lt = linktypes[0] if linktypes else DLT_EN10MB
            r = tsresol[0] if tsresol else 1e-6
            yield 0.0, lt, body[4:]


class _FlowOut:
    def __init__(self, root: Path, flow_id: str, client: str, host: str, port: int, ts: float):
        self.dir = root / flow_id.replace(":", "_").replace("/", "_").replace("->", "_")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.meta = {"flow_id": flow_id, "host": host, "port": port, "proto": "udp",
                     "client": client, "created": ts}
        self.bins = {"request": bytearray(), "response": bytearray()}
        self.frames = []
        self.seq = 0

    def add(self, direction: str, payload: bytes, ts: float) -> None:
        off = len(self.bins[direction])
        self.bins[direction].extend(payload)
        self.frames.append({"seq": self.seq, "ts": ts, "dir": direction,
                            "offset": off, "length": len(payload)})
        self.seq += 1

    def flush(self) -> None:
        (self.dir / "request.bin").write_bytes(bytes(self.bins["request"]))
        (self.dir / "response.bin").write_bytes(bytes(self.bins["response"]))
        (self.dir / "meta.json").write_text(
            json.dumps(self.meta, ensure_ascii=False), encoding="utf-8")
        with (self.dir / "frames.jsonl").open("w", encoding="utf-8") as f:
            for rec in self.frames:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def import_pcap(pcap_path, out_root, *, min_packets: int = 8, ports=None,
                run_name: str | None = None, quiet: bool = False):
    """把一份 pcap 拆成 flow 目录，返回 (run 目录, 统计 dict)。"""
    pcap_path = Path(pcap_path)
    if not pcap_path.is_file():
        raise SystemExit("找不到文件：%s" % pcap_path)
    run_dir = Path(out_root) / (run_name or ("run_pcap_" + time.strftime("%Y%m%d_%H%M%S")))
    flows_dir = run_dir / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)

    conns = {}          # 规范化五元组 -> _FlowOut
    first_ts = {}       # 规范化五元组 -> 最早时间戳（用于定方向）
    n_pkt = n_udp = n_skip = 0
    for ts, dlt, raw in iter_records(pcap_path):
        n_pkt += 1
        got = parse_udp_frame(dlt, raw)
        if not got:
            n_skip += 1
            continue
        src, sp, dst, dp, payload = got
        if not payload:
            n_skip += 1
            continue
        if ports and sp not in ports and dp not in ports:
            n_skip += 1
            continue
        n_udp += 1
        a, b = (src, sp), (dst, dp)
        key = (a, b) if a <= b else (b, a)
        fwd = a <= b                                  # 规范化方向：小的一端在前
        f = conns.get(key)
        if f is None:
            # 第一个包的方向 = request（对局流由客户端先发起）
            client_is_a = a == (src, sp) if a <= b else b == (src, sp)
            client = src if fwd else dst
            host, port = (dst, dp) if fwd else (src, sp)
            f = _FlowOut(flows_dir, "%s:%d->%s:%d/udp" % (src, sp, dst, dp),
                         client, host, port, ts)
            conns[key] = f
            first_ts[key] = ts
            f._client_side = (src, sp)                # 第一个包的源端口侧 = 客户端
        direction = "request" if (src, sp) == f._client_side else "response"
        f.add(direction, payload, ts)

    kept = []
    for f in conns.values():
        if len(f.frames) < min_packets:
            continue
        f.flush()
        kept.append(f)
    # flows/<n> 用序号命名（与抓包侧一致），便于 --flow 指定
    for i, f in enumerate(sorted(kept, key=lambda x: x.meta["created"])):
        target = flows_dir / str(i)
        if f.dir != target:
            if target.exists():
                for p in target.iterdir():
                    p.unlink()
                target.rmdir()
            try:
                f.dir.rename(target)
            except OSError:                            # 目标已存在/占用：退回复制
                target.mkdir(parents=True, exist_ok=True)
                for p in f.dir.iterdir():
                    p.replace(target / p.name)
    stats = {"packets": n_pkt, "udp": n_udp, "skipped": n_skip,
             "flows": len(kept), "out": str(run_dir)}
    if not quiet:
        print("[pcap] %s：报文 %d，其中 UDP %d，跳过 %d；落成 %d 条流 → %s"
              % (pcap_path.name, n_pkt, n_udp, n_skip, len(kept), run_dir))
    return run_dir, stats
