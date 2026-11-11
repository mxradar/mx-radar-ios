# -*- coding: utf-8 -*-
"""field13.py — NetMulticast_CharacterLocationNew（小队位置广播）确定性解码。

语义（round75 定版）：一次广播 3 条 GPS 轨迹 = 小队 3 人（我 + 2 队友），
与游戏内小地图小队标记一致；服务器直发，不依赖 MRA 槽位认领。
vals64 为各轨迹的 wire id；"我"的 wire id 跨局稳定（0xBE3732C5A970451E/1F，
flag 0/1），既作帧定位锚也作自机轨迹识别。
"""
from __future__ import annotations
import numpy as np

SELF_WIRE = (0xBE3732C5A970451E, 0xBE3732C5A970451F)
PATTERNS = tuple(v.to_bytes(8, "little") for v in SELF_WIRE)


def payload_hit(payload):
    """廉价预过滤：只命中「全量帧」（含"我"的 wire id）。
    field13 另有增量帧（仅坐标、无 vals64），预过滤抓不到 —— 需全量解码。"""
    return any(p in payload for p in PATTERNS)


def self_index(vals64):
    """vals64 里"我"的下标；找不到返回 0（对局惯例 locs[0]=自机）。"""
    for i, v in enumerate(vals64 or ()):
        if v in SELF_WIRE:
            return i
    return 0


def _read_bits(bits, pos, n):
    if pos < 0 or pos + n > len(bits):
        raise ValueError("eof")
    seg = bits[pos:pos + n]
    v = 0
    for i in range(n):
        if seg[i]:
            v |= 1 << i
    return v


def _dfm_packed(bits, pos, max_bytes=6):
    val = 0; shift = 0; nb = 0
    while True:
        if pos + 8 > len(bits) or nb >= max_bytes:
            raise ValueError("overflow")
        enc = _read_bits(bits, pos, 8)
        pos += 8; nb += 1
        val |= (enc >> 1) << shift
        if enc & 1 == 0:
            return val, pos
        shift += 7


def decode(payload, nbits):
    """-> {"vals64":[...], "vals32":[...], "locs":[[x,y,z](cm), ...]} 或 None。

    帧结构（495 局 478 帧逐帧实测，全部一致）：
        [field_index 4bit][content_len packed][payload_len packed]
        [n64 packed][vals64 = n64×64bit][n32 packed][vals32 = n32×32bit]
        [2bit gap][坐标 codec: 3×axes(5bit 头+prefix)][num_locs][locs]
    坐标 codec 起始 = 帧头解析后的当前位置（n64/n32 恒定 → 恒为 322，
    但按帧头动态计算更稳，消除对局差异风险）。
    vals32 = 运动相关字段（实测移动时变化、静止时恒定，非朝向角度），
    暂未用于定位，随解码返回备用。
    """
    bits = np.unpackbits(np.frombuffer(payload, dtype=np.uint8), bitorder="little")[:nbits]
    content_bits, pos = _dfm_packed(bits, 2)
    if content_bits is None or pos + content_bits != len(bits):
        return None
    content = bits[pos:pos + content_bits]
    if len(content) < 4 or _read_bits(content, 0, 4) != 13:
        return None
    payload_bits, pos2 = _dfm_packed(content, 4)
    if payload_bits is None or pos2 + payload_bits != len(content):
        return None
    fp = content[pos2:pos2 + payload_bits]
    n64, p = _dfm_packed(fp, 0)
    if n64 is None or not 0 <= n64 <= 3:
        return None
    vals64 = [_read_bits(fp, p + i * 64, 64) for i in range(n64)]
    p += n64 * 64
    n32, p = _dfm_packed(fp, p)
    if n32 is None or not 0 <= n32 <= 3:
        return None
    vals32 = [_read_bits(fp, p + i * 32, 32) for i in range(n32)]
    p += n32 * 32
    p += 2                              # 2bit gap（478 帧实测恒定；引擎保留位）
    axes = []
    for _ in range(3):
        pb = _read_bits(fp, p, 5); p += 5
        prefix = _read_bits(fp, p, pb); p += pb
        axes.append((pb, prefix))
    num_locs, p = _dfm_packed(fp, p)
    if num_locs is None or not 1 <= num_locs <= 3:
        return None
    if n64 and n64 != num_locs:
        return None
    locs = []
    for _ in range(num_locs):
        loc = []
        for pb, prefix in axes:
            rw = 32 - pb
            raw = _read_bits(fp, p, rw); p += rw
            combined = (prefix << rw) | raw
            sv = (combined >> 1) ^ -(combined & 1)   # zigzag
            loc.append(sv * 100.0)
        locs.append(loc)
    if p != len(fp):
        return None
    return {"vals64": vals64, "vals32": vals32, "locs": locs}
