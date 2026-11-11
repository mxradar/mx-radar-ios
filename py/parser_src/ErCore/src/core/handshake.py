# -*- coding: utf-8 -*-
"""handshake.py - UE4 控制通道握手扫描器（战斗流识别的引擎级判据）。

游戏逻辑（与客户端完全一致，非启发式）：
  UE4 客户端把一条 UDP 连接当作对局会话，当且仅当控制通道(ch0)上完成了
  握手状态机：

      C->S  NMT_HELLO      (open bunch, 引擎版本)
      S->C  NMT_CHALLENGE  (hex 挑战串)
      C->S  NMT_LOGIN      (FString URL "/Game/...")
      S->C  NMT_WELCOME    (FString 地图路径 "/Game/...")   <-- 引擎门
      C->S  NMT_JOIN       (单字节)

  客户端只有在收到 NMT_WELCOME 后才开始复制 Actor（PlayerState 等）；
  被拒连接收到 NMT_FAILURE 后直接断开。因此：

      战斗流 ⇔ S->C ch0 出现 NMT_WELCOME 且载荷含 "/Game/" 字符串

  这是引擎协议判定，不是统计启发式。

DFM 的 NMT 枚举值由全部历史抓包逐字节钉死（与标准 UE4 编号不同）：
  HELLO=0, WELCOME=1, CHALLENGE=3, LOGIN=5, FAILURE=6, JOIN=9
实测恒定特征（仅记录，不作判据——版本号会随游戏更新变化）：
  Hello 载荷 80 bit，含版本常数 2651415809
  Login  载荷含 "/Game/Maps/Login/Login?PlayerId=" 或直接 Iris_Entry URL
  Join   载荷恰 8 bit
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from contextlib import ExitStack

from codec.engine_packet import decode_engine_packet

NMT_HELLO = 0
NMT_WELCOME = 1
NMT_CHALLENGE = 3
NMT_LOGIN = 5
NMT_FAILURE = 6
NMT_JOIN = 9

# 握手必在连接最初几帧内完成（实测全部战斗流 <=8 帧）；96 帧是宽裕上限。
HS_MAX_FRAMES = 96


@dataclass
class Handshake:
    hello: bool = False
    challenge: str | None = None      # 挑战串（hex）
    login: bool = False               # 收到 Login（C->S）
    welcome: str | None = None        # 地图路径（S->C），None=未进入对局
    join: bool = False                # 客户端 Join
    failure: str | None = None        # 被拒原因（如有）
    welcome_frame: int = -1           # Welcome 所在帧号（双向合并计数）
    rounds: int = 0                   # 完整握手轮数（重连/换图会重跑）

    @property
    def ok(self) -> bool:
        """引擎门：客户端视角"这是对局会话"。"""
        return self.welcome is not None

    @property
    def full(self) -> bool:
        return self.ok and self.hello and self.login and self.join \
            and self.challenge is not None

    def as_dict(self):
        return {"ok": self.ok, "full": self.full, "welcome": self.welcome,
                "challenge": self.challenge, "failure": self.failure,
                "rounds": self.rounds, "welcome_frame": self.welcome_frame}


def _fstring_at(payload: bytes, off: int) -> str | None:
    """UE4 FString（i32 长度含结尾 \\0，ASCII）。"""
    if len(payload) < off + 4:
        return None
    n = int.from_bytes(payload[off:off + 4], "little")
    if n <= 1 or n > 512:
        return None
    raw = payload[off + 4:off + 4 + n - 1]
    if len(raw) != n - 1:
        return None
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        return None


def _scan_bunch(payload: bytes, pbits: int):
    """返回 (nmt, detail)。detail: challenge 串 / welcome 地图 / failure 文本。"""
    if pbits < 8 or not payload:
        return None, None
    nmt = payload[0]
    if nmt == NMT_CHALLENGE:
        s = _fstring_at(payload, 1)
        return (nmt, s) if s else (None, None)
    if nmt == NMT_WELCOME:
        # Welcome = FString MapName [+ FString GameName ...]；地图以 /Game/ 开头
        s = _fstring_at(payload, 1)
        if s and s.startswith("/Game/"):
            return nmt, s
        # 兜底：载荷前 128 字节内找 "/Game/"（防字段顺序差异）
        j = payload.find(b"/Game/")
        if 0 <= j < 128:
            return nmt, payload[j:].split(b"\x00")[0].decode("ascii", "replace")
        return None, None
    if nmt == NMT_FAILURE:
        s = _fstring_at(payload, 1)
        return nmt, s
    return nmt, None


def scan_control(flow_dir, max_frames=HS_MAX_FRAMES, *, auto_s2c=True) -> Handshake:
    """扫描双向 ch0 控制 bunch，重建握手状态机。确定性、只读前 max_frames 帧。"""
    fd = Path(flow_dir)
    hs = Handshake()
    table = None
    if auto_s2c:
        from s2c_auto import resolve_flow
        decision = resolve_flow(fd)
        if not decision.ready:
            return hs
        table = decision.table
    idx = {"request": 0, "response": 0}
    # 握手必在最初几帧内：双向各超过 max_frames 即可停读（不解析整个
    # frames.jsonl——增长中的流每轮重扫时这是主要成本）。
    try:
        fh = (fd / "frames.jsonl").open(encoding="utf-8")
    except OSError:
        return hs
    recs = []
    with fh:
        for line in fh.read(512 * 1024).splitlines():
            if not line.strip():
                continue
            if idx["request"] >= max_frames and idx["response"] >= max_frames:
                break
            try:
                rec = json.loads(line)
            except Exception:
                continue
            d = rec.get("dir")
            if d in idx and idx[d] < max_frames:
                recs.append(rec)
                idx[d] += 1
    idx = {"request": 0, "response": 0}
    seen_round = set()
    with ExitStack() as stack:
        sources = {}
        for d in ("request", "response"):
            path = fd / (d + ".bin")
            if path.exists():
                sources[d] = stack.enter_context(path.open("rb"))
        for rec in recs:
            d = rec.get("dir")
            if d not in sources or idx[d] >= max_frames:
                continue
            i = idx[d]; idx[d] += 1
            if rec["offset"] < 0 or not 1 <= rec["length"] <= 65535:
                continue
            sources[d].seek(rec["offset"])
            raw = sources[d].read(rec["length"])
            if len(raw) != rec["length"]:
                continue
            if d == "response" and table is not None:
                from s2c_dec import decode_s2c
                raw = decode_s2c(raw, table)
            try:
                tr = decode_engine_packet(int(rec.get("seq", 0)), d, raw)
            except Exception:
                continue
            bp = tr.bunch_parse
            if tr.error or bp is None or bp.error:
                continue
            for bn in bp.bunches:
                if bn.error or bn.channel_index != 0 or bn.partial:
                    continue
                nmt, det = _scan_bunch(bn.payload, bn.payload_bit_count)
                if nmt is None:
                    continue
                if nmt == NMT_HELLO:
                    hs.hello = True
                elif nmt == NMT_CHALLENGE:
                    hs.challenge = det
                elif nmt == NMT_LOGIN:
                    hs.login = True
                elif nmt == NMT_WELCOME:
                    if hs.welcome is None:
                        hs.welcome = det
                        hs.welcome_frame = i
                    hs.rounds += 1
                elif nmt == NMT_JOIN:
                    hs.join = True
                elif nmt == NMT_FAILURE and hs.failure is None:
                    hs.failure = det
    return hs
