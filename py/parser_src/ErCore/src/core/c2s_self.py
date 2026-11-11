# -*- coding: utf-8 -*-
"""c2s_self.py —— 从上行（C2S）明文里解自机位姿（位置 / 速度 / 朝向 / 俯仰）。

数据来源：客户端 -> 服务器的 ch3 消息 parse_fgpmove_params（当前版本起点 bit 45），
移植自 udp_actors.h::ParseFgp。那份实现的缓冲与本项目 LB 的位前缀差 16 位，
实测口径为「32 位字的高 16 位 = yaw、低 16 位 = pitch」。

上行没有 S2C 那套混淆，直接按引擎格式解；但 ch3 的大 RPC 会跨包，要走
engine_bunch 的分片容错 + PartialBunchReassembler（与下行同一条路）。

实测校验（run_20260912_192140，flow 3，3030 个可配对样本）：
  · yaw 与下行自机 yaw 命中 2547/3030 = 84%
  · loc 与下行自机坐标：X/Y 中位差 0.0cm（抖动 5~8cm，即两流时间对齐误差），
    Z 为常量 +86cm、抖动 0.1cm（脚底/眼高口径差）

**2026-09-15 扩展：该消息族有两种尾型，`lite`（无 accel 段）原被误当解析失败丢弃。**
收下它以后自机位姿 7.4Hz → 15.0Hz（两局实测 2.0× / 3.3×），俯仰同步翻倍。
详见 `parse_fgp` 的 docstring。
"""
from __future__ import annotations

import struct

__all__ = ["parse_fgp", "FGP_START_BIT"]

FGP_START_BIT = 45          # 当前 1.201.37119 的 ch3 起点；旧记录可显式传 start=53
_LOC_SANE = 2.0e6           # |坐标| 上限（厘米）
_LOC_MIN_XY = 5000.0        # 世界坐标下界：小于 50m 视为没解对
_TS_MIN, _TS_MAX = 1.0, 1.0e6


class _BR:
    """位流读取（LSB-first，与 codec.bitstream / udp_actors.h 的 BitReader 同序）。"""

    __slots__ = ("d", "pos", "n", "error")

    def __init__(self, data, pos, nbits):
        self.d, self.pos, self.n, self.error = data, pos, nbits, False

    def remaining(self):
        return self.n - self.pos

    def read(self, width):
        if width < 0 or self.pos + width > self.n:
            self.error = True
            return 0
        v = 0
        d = self.d
        if not width:
            return 0
        if width == 1:
            v = (d[self.pos >> 3] >> (self.pos & 7)) & 1
        else:
            stop = (self.pos + width + 7) >> 3
            if stop > len(d):
                raise IndexError('bit reader past buffer')
            v = (int.from_bytes(d[self.pos >> 3:stop], 'little') >> (self.pos & 7)) & ((1 << width)-1)
        self.pos += width
        return v

    def read_int(self, max_value):
        """UE 的变长 SerializeInt（值 + 掩码 >= 上限就停）。"""
        if max_value <= 1:
            return 0
        if max_value & (max_value-1) == 0:
            return self.read(max_value.bit_length()-1)
        value, mask = 0, 1
        while value + mask < max_value:
            if self.read(1):
                value |= mask
            if self.error:
                return 0
            mask <<= 1
        return value


def _f32(bits):
    return struct.unpack("<f", struct.pack("<I", bits & 0xFFFFFFFF))[0]


def _packed_vec(r, max_bits, scale):
    """UE 的量化向量：先读位宽码（+2 位），再逐分量读。返回列表或 None。"""
    code = r.read_int(max_bits)
    if r.error:
        return None
    width = code + 2
    if width <= 0 or width >= 32:
        return None
    span = 1 << width
    out = []
    for _ in range(3):
        raw = r.read_int(span)
        if r.error:
            return None
        out.append((raw - span * 0.5) / scale)
    return out


def _angle(short16):
    """16 位压缩角 -> ±180 度。"""
    deg = short16 * (360.0 / 65536.0)
    return deg - 360.0 if deg > 180.0 else deg


def parse_fgp(payload, pbits, start=FGP_START_BIT):
    """解一条 ch3 自机移动消息。成功返回 dict，否则 None。

    返回：ts(秒，游戏内计时) / loc(世界厘米) / vel / accel / yaw / pitch / flags / tail。
    yaw、pitch 均为度，pitch 正 = 抬头。

    **尾部有两种形态**（同一消息族的两个变体，2026-09-15 团队模式一局实测）：
      · `full`：loc 之后是 acc 量化向量 + 8 位 flags（位宽 314/396/399/402…）
      · `lite`：loc 之后直接 8 位收尾（位宽 303/306…）
    两条都带 ts/vel/yaw/pitch/loc，只有尾部不同。原实现只收 `full`，于是把
    `lite` 整族当"结构校验失败"丢掉了 —— 而它才是 ch3 里**出现最多**的那一类
    （同一局 892s：303 有 5759 条、306 有 1081 条，比任何 full 变体都多）。
    收下它，自机位姿从 6594 条 → 13408 条（7.4Hz → 15.0Hz），其中 6428 条带
    非零 pitch —— 第一视角的俯仰只有这条上行能给。

    判据仍是"正好读尽"：先试 full，读不尽就回溯到 loc 之后试 lite。
    两种尾型的 loc 都拿下行自机槽位做过金标准比对：
      · 303 → 水平偏差中位 0.1cm / p90 21.3cm（与 full 型的 314 完全同档；
        这点偏差来自两条流的时间对齐误差，不是解码误差）
      · 306 → 中位 0.0cm / p90 3.6cm
    合并后相邻样本间隔 <10ms 的只占 0.3% —— 确认是**交错的两族**，不是同一次
    更新的重复广播。
    """
    # The full tail with flag 4 includes 696 more bits. Its valid messages are
    # larger than the former 800-bit guard (e.g. 1098 bits in captured traffic).
    if payload is None or pbits < 190 or pbits > 2048:
        return None
    r = _BR(payload, start, pbits)
    if r.read(1) != 1 or r.error:
        return None
    ts = _f32(r.read(32))
    if r.error or not (_TS_MIN <= ts <= _TS_MAX):
        return None
    vel = _packed_vec(r, 24, 10.0)
    if vel is None:
        return None
    r.read(32)
    yaw_pack = r.read(32)
    if r.error:
        return None
    loc = _packed_vec(r, 30, 100.0)
    if loc is None or any(abs(c) >= _LOC_SANE for c in loc):
        return None
    if max(abs(loc[0]), abs(loc[1])) < _LOC_MIN_XY:
        return None
    # loc 之后分岔：回溯到同一位点分别试两种尾型，谁正好读尽谁算数。
    tail_pos = r.pos
    for kind in ("full", "lite"):
        r.pos = tail_pos
        r.error = False
        r.read(8)
        r.read(32)
        r.read(8)
        if r.error:
            continue
        accel = None
        flags = None
        if kind == "full":
            accel = _packed_vec(r, 30, 100.0)
            if accel is None:
                continue
            flags = r.read(8)
            if r.error or (flags & 0xFB) != 0:
                continue
            left = r.remaining()
            if flags & 4:
                if left != 696:
                    continue
                r.read(696)
                if r.error or r.remaining() != 0:
                    continue
            elif left != 0:
                continue
        else:
            r.read(8)
            if r.error or r.remaining() != 0:
                continue
        return {
            "ts": ts,
            "loc": loc,
            "vel": vel,
            "accel": accel,
            "yaw": _angle((yaw_pack >> 16) & 0xFFFF),
            "pitch": _angle(yaw_pack & 0xFFFF),
            "flags": flags,
            "tail": kind,
        }
    return None
