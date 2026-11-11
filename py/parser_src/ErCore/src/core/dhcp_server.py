# -*- coding: utf-8 -*-
"""dhcp_server.py - 迷你 DHCP 服务器（热点网段，治 ICS DHCP 坏死的机器）。

背景：本机实测 Windows 移动热点的 ICS DHCP 在服务重启/网络重置后始终不起
（手机关联认证正常、DISCOVER 广播上线上可见、系统永不回 OFFER）。本服务在
UDP :67 上应答 DHCP，把 IP 发出去；出网靠 WinNAT（New-NetNat）+ 网卡转发，
与 ICS 完全无关。

协议面（最小集，实测覆盖手机全流程）：
  DISCOVER(1) -> OFFER(2)      广播应答（flags 恒带广播位）
  REQUEST(3)  -> ACK(5)        选型期广播 / 续租单播（ciaddr）都接
  opt54 非本服务器（客户端选了别人）时让路不回包
  租约按 MAC 稳定分配（重启不变），持久化 leases.json

用法: python src/core/dhcp_server.py [--ip 192.168.137.1] [--pool 100 200]
前置: 热点已开（本机有 <ip> 地址）；出网需 WinNAT + 转发（见 LOCAL_RADAR.md）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAGIC = b"\x63\x82\x53\x63"
LEASE_SEC = 86400
DNS = ["223.5.5.5", "114.114.114.114"]


def _ip(s: str) -> list[int]:
    return [int(x) for x in s.split(".")]


def parse_discover(pkt: bytes):
    """DHCP 请求 -> {xid, mac, flags, ciaddr, opt53, opt54}；非请求返回 None。"""
    if len(pkt) < 240 or pkt[0] != 1 or pkt[236:240] != MAGIC:
        return None
    opts: dict[int, bytes] = {}
    i = 240
    while i < len(pkt):
        t = pkt[i]
        if t == 0:
            i += 1
            continue
        if t == 255 or i + 2 > len(pkt):
            break
        ln = pkt[i + 1]
        if ln == 0 and t != 255:
            i += 2
            continue
        opts[t] = pkt[i + 2:i + 2 + ln]
        i += 2 + ln
    return {
        "xid": pkt[4:8],
        "mac": pkt[28:34].hex(":"),
        "flags": struct.unpack(">H", pkt[10:12])[0],
        "ciaddr": ".".join(str(b) for b in pkt[12:16]),
        "opt53": opts.get(53, b"\x00")[0],
        "opt54": ".".join(str(b) for b in opts.get(54, b"\x00\x00\x00\x00")),
    }


def build_reply(xid: bytes, mac_hex: str, yiaddr: str, mtype: int, server: str,
                lease: int, mask: str, router: str, dns: list[str], broadcast: bool) -> bytes:
    pkt = struct.pack(">BBBBIHH", 2, 1, 6, 0, int.from_bytes(xid, "big"), 0,
                      0x8000 if broadcast else 0)
    pkt += struct.pack(">I", 0)                     # ciaddr
    pkt += bytes(_ip(yiaddr))                       # yiaddr
    pkt += bytes(_ip(server))                       # siaddr
    pkt += struct.pack(">I", 0)                     # giaddr
    pkt += bytes.fromhex(mac_hex.replace(":", "")) + b"\x00" * 10   # chaddr
    pkt += b"\x00" * 64 + b"\x00" * 128             # sname / file
    pkt += MAGIC
    opts = [(53, [mtype]), (54, _ip(server)), (51, list(struct.pack(">I", lease))),
            (1, _ip(mask)), (3, _ip(router)), (6, sum((_ip(d) for d in dns), []))]
    for t, v in opts:
        pkt += bytes([t, len(v)]) + bytes(v)
    pkt += b"\xff"
    return pkt


class Leases:
    """MAC -> IP 稳定租约（持久化；重启后同一手机拿同一 IP）。"""

    def __init__(self, path: Path, server: str, pool_lo: int, pool_hi: int):
        self.path = path
        self.server = server
        self.lo, self.hi = pool_lo, pool_hi
        self.table: dict[str, str] = {}
        try:
            self.table = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self.table = {}

    def get(self, mac: str) -> str:
        ip = self.table.get(mac)
        if ip:
            return ip
        used = set(self.table.values())
        for n in range(self.lo, self.hi + 1):
            cand = f"192.168.137.{n}"
            if cand not in used:
                self.table[mac] = cand
                try:
                    self.path.write_text(json.dumps(self.table, indent=1), encoding="utf-8")
                except OSError:
                    pass
                return cand
        raise RuntimeError("地址池耗尽")


def hotspot_ip() -> str | None:
    """当前热点侧网关 IP（SoftAP 每次开关都会重建，IP 可能暂时不存在）。"""
    import re
    import subprocess
    try:
        out = subprocess.run(["ipconfig"], capture_output=True, text=True,
                             encoding="gbk", errors="replace").stdout
    except Exception:
        return None
    m = re.findall(r"192\.168\.137\.\d{1,3}", out or "")
    return m[0] if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=None,
                    help="本机热点侧 IP（缺省自动探测 192.168.137.x；热点未开则等待）")
    ap.add_argument("--pool", nargs=2, type=int, default=[100, 200], metavar=("LO", "HI"))
    ap.add_argument("--mask", default="255.255.255.0")
    ap.add_argument("--wait", type=float, default=120.0,
                    help="等待热点就绪的秒数（0=不等待）")
    args = ap.parse_args()
    mask = args.mask

    router = args.ip or hotspot_ip()
    deadline = time.time() + args.wait
    while router is None and time.time() < deadline:
        print("[dhcp] 热点未开（无 192.168.137.x 地址），3s 后重试...", flush=True)
        time.sleep(3)
        router = args.ip or hotspot_ip()
    if router is None:
        print("[dhcp] ! 热点一直未开，退出（先开移动热点再运行本服务）")
        return 1
    pool = f"192.168.137.{args.pool[0]}-192.168.137.{args.pool[1]}"
    # 冻结 exe 里 __file__ 在临时解包目录，租约要落 exe 旁才跨重启稳定
    data_dir = Path(os.environ.get("ER_DATA") or Path(__file__).resolve().parent)
    leases = Leases(data_dir / "dhcp_leases.json", router, *args.pool)

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)      # 收：0.0.0.0:67（单播续租）
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("0.0.0.0", 67))
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)      # 发：源址=网关 IP
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    tx.bind((router, 0))

    def handle(pkt: bytes, src: str) -> None:
        req = parse_discover(pkt)
        if not req or req["opt53"] not in (1, 3):
            return
        if req["opt53"] == 3 and req["opt54"] not in ("0.0.0.0", router):
            return                       # 客户端选了别的服务器，让路
        try:
            yiaddr = leases.get(req["mac"])
        except RuntimeError as exc:
            print(f"[dhcp] ! {exc}")
            return
        mtype = 2 if req["opt53"] == 1 else 5      # OFFER / ACK
        unicast = req["ciaddr"] != "0.0.0.0"
        reply = build_reply(req["xid"], req["mac"], yiaddr, mtype, router,
                            LEASE_SEC, mask, router, DNS, not unicast)
        try:
            tx.sendto(reply, (req["ciaddr"] if unicast else "255.255.255.255", 68))
        except OSError as exc:
            print(f"[dhcp] send err: {exc}")
            return
        name = {1: "DISCOVER->OFFER", 3: "REQUEST->ACK"}[req["opt53"]]
        print(f"[{time.strftime('%H:%M:%S')}] {name}  {req['mac']}  ->  {yiaddr}"
              f"  ({src})", flush=True)

    # 主路收包：pcap 直抓热点网卡（广播 DISCOVER 不经系统 UDP 栈也能收到——
    # 本机实测 SoftAP 的广播不投递给 UDP 套接字，ICS 同死于这类栈层问题）
    pcap_loop = None
    try:
        import ctypes
        from nic_capture import (load_wpcap, list_devices, parse_udp_frame,
                                 _PcapPkthdr, _BpfProgram, PCAP_NETMASK_UNKNOWN)
        lib = load_wpcap()
        if lib is not None:
            for fn, (rt, at) in {
                    "pcap_open_live": (ctypes.c_void_p, [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_char_p]),
                    "pcap_next_ex": (ctypes.c_int, [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_PcapPkthdr)), ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]),
                    "pcap_close": (None, [ctypes.c_void_p]),
                    "pcap_compile": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint32]),
                    "pcap_setfilter": (ctypes.c_int, [ctypes.c_void_p, ctypes.c_void_p]),
                    "pcap_freecode": (None, [ctypes.c_void_p]),
                    "pcap_datalink": (ctypes.c_int, [ctypes.c_void_p]),
            }.items():
                f = getattr(lib, fn)
                f.restype, f.argtypes = rt, at
            dev = next((n for n, d, ips in list_devices(lib)
                        if args.ip in ips), None)
            if dev:
                errbuf = ctypes.create_string_buffer(256)
                h = lib.pcap_open_live(dev.encode(), 65535, 1, 100, errbuf)
                if h:
                    prog = _BpfProgram()
                    if lib.pcap_compile(h, ctypes.byref(prog),
                                        b"udp and dst port 67", 1,
                                        PCAP_NETMASK_UNKNOWN) == 0:
                        lib.pcap_setfilter(h, ctypes.byref(prog))
                        lib.pcap_freecode(ctypes.byref(prog))
                    hdr = ctypes.POINTER(_PcapPkthdr)()
                    data = ctypes.POINTER(ctypes.c_ubyte)()

                    def pcap_loop():
                        while True:
                            if lib.pcap_next_ex(h, ctypes.byref(hdr),
                                                ctypes.byref(data)) == 1:
                                raw = ctypes.string_at(data, hdr.contents.caplen)
                                u = parse_udp_frame(lib.pcap_datalink(h), raw)
                                if u and u[4] and 67 in (u[1], u[3]):
                                    handle(u[4], "pcap")
                    import threading
                    threading.Thread(target=pcap_loop, daemon=True).start()
                    print(f"[dhcp] pcap rx on {dev} (broadcast direct)")
    except Exception as exc:
        print(f"[dhcp] pcap rx unavailable ({exc}); UDP socket only")

    print(f"[dhcp] listening :67  server={router}  pool={pool}  dns={','.join(DNS)}")
    print("[dhcp] 等手机连热点即可自动发 IP（配合 WinNAT 出网）")
    while True:
        pkt, addr = rx.recvfrom(2048)
        handle(pkt, f"udp:{addr[0]}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
