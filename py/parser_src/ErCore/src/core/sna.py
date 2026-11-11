
# -*- coding: utf-8 -*-
"""sna.py - deterministic UE4 open-bunch prefix decoder (engine-exact).

Verified against captured data (see round54_findings.md):

UNetConnection::ReceivedRawBunch
    if (Bunch.bHasPackageMapExports)
        UPackageMapClient::ReceiveNetGUIDBunch(Bunch)
            1 bit  bHasRepLayoutExport      <-- the bit that was missing
            if bHasRepLayoutExport: ReceiveNetFieldExportsCompat(); return
            int32  NumGUIDsInBunch
            N x InternalLoadObject(exporting=true)
UActorChannel::ReceivedBunch
    if (Bunch.bHasMustBeMappedGUIDs)
        uint16 NumMustBeMappedGUIDs ; N x packed FNetworkGUID
UActorChannel::ProcessBunch -> UPackageMapClient::SerializeNewActor
    SerializeObject(Actor)
    if NetGUID.IsDynamic():
        SerializeObject(Archetype) ; SerializeObject(ActorLevel)
        ConditionallySerializeQuantizedVector(Location)
        1b bSerializeRotation [+ FRotator::SerializeCompressedShort]
        ConditionallySerializeQuantizedVector(Scale)
        ConditionallySerializeQuantizedVector(Velocity)
-> content blocks

InternalLoadObject:
    packed FNetworkGUID ; if 0 -> return
    if (guid == 1 (default) or exporting) uint8 ExportFlags
    if ExportFlags.bHasPath:
        InternalLoadObject(outer) ; FString PathName
        if ExportFlags.bHasNetworkChecksum: uint32
"""
import struct, sys
from pathlib import Path
import os

import numpy as np
ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])


class Bad(Exception):
    pass


def rbits(bits, pos, n):
    if pos < 0 or pos + n > len(bits):
        raise Bad("eof")
    if n <= 0:
        return 0
    if n == 1:
        return int(bool(bits[pos]))
    return int.from_bytes(np.packbits(bits[pos:pos+n], bitorder='little').tobytes(), 'little')


def packed(bits, pos):
    v = 0; cnt = 0
    while True:
        b = rbits(bits, pos, 8); pos += 8
        v += (b >> 1) << (7 * cnt); cnt += 1
        if not (b & 1):
            return v, pos
        if cnt > 5:
            raise Bad("packed>5")


def ser_int(bits, pos, vmax):
    v = 0; mask = 1
    while (v + mask) < vmax:
        if pos >= len(bits):
            raise Bad("eof serint")
        if bits[pos]:
            v |= mask
        pos += 1; mask <<= 1
    return v, pos


def fstring(bits, pos, maxu=4096):
    n = rbits(bits, pos, 32); pos += 32
    if n >= 0x80000000:
        n -= 1 << 32
    u = abs(n)
    if u > maxu:
        raise Bad("fstring len %d" % n)
    if u == 0:
        return "", pos
    w = 8 if n > 0 else 16
    nb = u * w
    if pos + nb > len(bits):
        raise Bad("eof fstring")
    # 批量读：packbits(little) 把位序数组按 LSB-first 组字节，与逐 unit
    # rbits(w).to_bytes(w//8,'little') 逐字节一致（原实现 u 次 Python 循环，
    # 名字解析在追赶期是 sna 热点之一）
    raw = np.packbits(bits[pos:pos + nb], bitorder="little").tobytes()
    pos += nb
    s = raw.decode("utf-8" if w == 8 else "utf-16-le", errors="replace").rstrip("\x00")
    return s, pos


def load_object(bits, pos, exporting, depth=0, sink=None):
    """UPackageMapClient::InternalLoadObject."""
    if depth > 16:
        raise Bad("recursion")
    g, pos = packed(bits, pos)
    if g == 0:
        return g, pos
    flags = 0
    if g == 1 or exporting:
        flags = rbits(bits, pos, 8); pos += 8
        if flags & 0xF8:
            raise Bad("exportflags %02x" % flags)
    if flags & 1:
        og, pos = load_object(bits, pos, exporting, depth + 1, sink)
        path, pos = fstring(bits, pos)
        if flags & 4:
            pos += 32
        if sink is not None:
            sink.append({"guid": g, "outer": og, "path": path, "flags": flags})
    elif sink is not None:
        sink.append({"guid": g, "outer": None, "path": None, "flags": flags})
    return g, pos


def read_exports(bits, pos, sink):
    """UPackageMapClient::ReceiveNetGUIDBunch."""
    has_replayout = bits[pos]; pos += 1
    if has_replayout:
        raise Bad("bHasRepLayoutExport")
    n = rbits(bits, pos, 32); pos += 32
    if n < 0 or n > 2048:
        raise Bad("numguids %d" % n)
    for _ in range(n):
        load_object(bits, pos_holder := pos, True, 0, sink)
        _, pos = load_object(bits, pos, True, 0, None) if False else (None, pos)
        # (re-walk avoided: do it properly below)
        raise Bad("unreachable")
    return n, pos


def read_exports2(bits, pos, sink):
    has_replayout = bits[pos]; pos += 1
    if has_replayout:
        raise Bad("bHasRepLayoutExport")
    n = rbits(bits, pos, 32); pos += 32
    if n < 0 or n > 2048:
        raise Bad("numguids %d" % n)
    for _ in range(n):
        _, pos = load_object(bits, pos, True, 0, sink)
    return n, pos


def cond_vector(bits, pos, quantize_bit, scale, maxbits):
    f = bits[pos]; pos += 1
    if not f:
        return None, pos
    q = 1
    if quantize_bit:
        q = bits[pos]; pos += 1
    if q:
        nb, pos = ser_int(bits, pos, maxbits)
        bias = 1 << (nb + 1); w = nb + 2
        v = []
        for _ in range(3):
            v.append((rbits(bits, pos, w) - bias) / float(scale)); pos += w
        return v, pos
    v = []
    for _ in range(3):
        v.append(struct.unpack("<f", rbits(bits, pos, 32).to_bytes(4, "little"))[0]); pos += 32
    return v, pos


def rot_short(bits, pos):
    # FRotator_NetSerialize_3x16_short: per axis 1 flag bit (bIsZero) + u16 short only if flag.
    v = []
    for _ in range(3):
        nz = bits[pos]; pos += 1
        if nz:
            v.append(rbits(bits, pos, 16) * 360.0 / 65536.0); pos += 16
        else:
            v.append(0.0)
    return v, pos


def parse_prefix(bits, has_exports, has_must, quantize_bit=False, nq_scale=10.0, nq_bits=24, frags=None):
    """Return (info, content_block_start_bit). Raises Bad on engine-illegal reads.

    frags: 可选 [(start_bit, hx, hm), ...] —— partial bunch 的 fragment 边界。
    partial bunch 里每个 bHasPackageMapExports 的 fragment 各带一个独立 exports 段
    （ReceiveNetGUIDBunch 每 fragment 调用一次），必须逐段读，否则 must/actor 段错位。
    """
    pos = 0
    info = {"exports": [], "must": []}
    num_exports = 0
    if has_exports:
        if frags is not None:
            last_end = pos
            for fstart, fhx, _fhm in frags:
                if not fhx:
                    continue
                n, last_end = read_exports2(bits, fstart, info["exports"])
                num_exports += n
            pos = last_end
        else:
            n, pos = read_exports2(bits, pos, info["exports"])
            num_exports = n
    info["num_exports"] = num_exports
    if has_must:
        must_start = pos
        if frags is not None:
            for fstart, _fhx, fhm in frags:
                if fhm:
                    must_start = fstart
                    break
        nm = rbits(bits, must_start, 16); pos = must_start + 16
        if nm > 256:
            raise Bad("nmust %d" % nm)
        for _ in range(nm):
            g, pos = packed(bits, pos)
            info["must"].append(g)
    a, pos = load_object(bits, pos, False)
    info["actor"] = a
    if a == 0:
        raise Bad("actor guid 0")
    if a & 1:
        info["static_actor"] = True
        info["stream_start"] = pos
        return info, pos
    ar, pos = load_object(bits, pos, False)
    lv, pos = load_object(bits, pos, False)
    info["arch"] = ar; info["level"] = lv
    info["loc"], pos = cond_vector(bits, pos, quantize_bit, nq_scale, nq_bits)
    rf = bits[pos]; pos += 1
    info["rot"] = None
    if rf:
        info["rot"], pos = rot_short(bits, pos)
    info["scale"], pos = cond_vector(bits, pos, quantize_bit, nq_scale, nq_bits)
    info["vel"], pos = cond_vector(bits, pos, quantize_bit, nq_scale, nq_bits)
    info["stream_start"] = pos
    return info, pos
