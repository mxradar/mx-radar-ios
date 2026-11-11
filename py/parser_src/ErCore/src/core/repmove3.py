
# -*- coding: utf-8 -*-
"""repmove3.py - FRepMovementInfo (h21) frame + item reader, near-complete.

Item field order (libUE4 offsets = execution order):
  sel=2bits -> mask {0:0x41,1:0x20,2:0x61} else 12 bits
  0x040 : scalar  (32-ctx.bits[3])  zigzag/10000            -> item+164
  0x001a: 3 comps (32-ctx.bits[k])  zigzag/10               -> item+24..32   (POSITION)
  0x001b: PackedVector h=SerializeInt(24), Max=1<<(h+2), 3x SerializeInt(Max)-1<<(h+1) -> item+0..8
  0x080 : 1 byte -> +168 | 0x100: 1 bit -> +169 | 0x200: 1 bit -> +170
  0x800 : 1 bit  -> +172 | 0x400: 1 bit -> +171
  0x001c: 2 bits (+52) + FRotator::SerializeCompressed(byte)  -> +40,44,48
  0x002 : SerializeIntPacked NetGUID -> +56 ; Ar<<FName -> +64 ; 1b ; sub_F1FF2D8 ; 2b
  0x004 : 1b(+132) 1b(+134) [1 byte(+133)] PackedVector(+116..124) [PackedVector(+104..112)] 1 byte(+136)
  0x008 : u32 -> +144 ; 3 bits -> +142,+140,+141
  0x020 : 32 bits -> +160
  0x010 : sub_EAFCF10 object read -> +152

Frame:
  u32 Count ; u32 mask[ceil(Count/32)]
  present items (header re-read whenever (i>>4) changes)
  1 bit ; 1 bit hasBitArray [Count bits] ; 1 bit hasEmbedded [header + item]
  u32 Count2 ; u32 mask2[ceil(Count2/32)]
"""
import os, sys
from pathlib import Path
ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "src"))
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int

SEL_MASK = {0: 0x41, 1: 0x20, 2: 0x61}
UNMODELLED = 0x002 | 0x004 | 0x010

def zz(v):
    v &= 0xFFFFFFFF
    s = ((v >> 1) ^ (-(v & 1) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return s - (1 << 32) if s & 0x80000000 else s

class Ctx:
    __slots__ = ("val", "bits", "flags")
    def __init__(self):
        self.val = [0, 0, 0, 0]; self.bits = [-1, -1, -1, -1]; self.flags = 0

def read_header(r, ctx):
    ctx.flags = r.bits(2)
    if ctx.flags & 1:
        for k in (0, 1, 2):
            nb = r.bits(5); ctx.bits[k] = nb
            raw = r.bits(nb) if nb else 0
            ctx.val[k] = ((raw << (32 - nb)) & 0xFFFFFFFF) if nb else 0
    if ctx.flags & 2:
        nb = r.bits(5); ctx.bits[3] = nb
        raw = r.bits(nb) if nb else 0
        ctx.val[3] = ((raw << (32 - nb)) & 0xFFFFFFFF) if nb else 0

def _pv(r):
    h = r.serialize_int(24)
    mx = 1 << (h + 2); bias = 1 << (h + 1)
    return [r.serialize_int(mx) - bias for _ in range(3)]

def _rot(r):
    return [(r.uint8() * 360.0 / 256.0 if r.bit() else 0.0) for _ in range(3)]

def read_item(r, ctx):
    """returns (dict, stop_reason_or_None). On stop the caller must abandon the stream."""
    sel = r.bits(2)
    m = SEL_MASK.get(sel)
    if m is None: m = r.bits(12)
    o = {"sel": sel, "mask": m}
    if m & 0x40:
        n = ctx.bits[3]
        if not (0 <= n <= 32): raise BitError("bits3=%d" % n)
        w = 32 - n
        o["scalar"] = zz((r.bits(w) if w else 0) | ctx.val[3]) / 10000.0
    if m & 0x01:
        pos = []
        for k in (0, 1, 2):
            n = ctx.bits[k]
            if not (0 <= n <= 32): raise BitError("bits%d=%d" % (k, n))
            w = 32 - n
            pos.append(zz((r.bits(w) if w else 0) | ctx.val[k]) / 10.0)
        o["pos"] = pos
        o["pv"] = _pv(r)
    if m & 0x080: o["b168"] = r.uint8()
    if m & 0x100: o["b169"] = r.bit()
    if m & 0x200: o["b170"] = r.bit()
    if m & 0x800: o["b172"] = r.bit()
    if m & 0x400: o["b171"] = r.bit()
    if m & 0x001:
        o["f52"] = r.bits(2)
        o["rot"] = _rot(r)
    if m & 0x002:
        o["guid"] = read_dfm_packed_int(r)[0]
        return o, "m&2"
    if m & 0x004:
        return o, "m&4"
    if m & 0x008:
        o["u144"] = r.bits(32)
        o["b142"] = r.bit(); o["b140"] = r.bit(); o["b141"] = r.bit()
    if m & 0x020:
        o["u160"] = r.bits(32)
    if m & 0x010:
        return o, "m&0x10"
    return o, None

def read_frame(payload, start, end, max_count=512):
    r = BitReader(payload, end); r.position = start
    C = r.bits(32)
    if C > max_count: raise BitError("count %d" % C)
    mask = [r.bits(32) for _ in range((C + 31) // 32)]
    present = [i for i in range(C) if (mask[i // 32] >> (i % 32)) & 1]
    fr = {"count": C, "present": present, "items": {}, "stopped": None}
    ctx = Ctx(); grp = -1
    for i in present:
        if (i >> 4) != grp:
            grp = i >> 4
            read_header(r, ctx)
        it, stop = read_item(r, ctx)
        fr["items"][i] = it
        if stop:
            fr["stopped"] = ("item%d" % i, stop); fr["pos"] = r.position
            fr["left"] = end - r.position; return fr, r
    fr["bit_a"] = r.bit()
    fr["has_bits"] = r.bit()
    if fr["has_bits"] and C: fr["bitarray"] = r.bits(C)
    fr["has_embedded"] = r.bit()
    if fr["has_embedded"]:
        ectx = Ctx(); read_header(r, ectx)
        it, stop = read_item(r, ectx)
        fr["embedded"] = it
        if stop:
            fr["stopped"] = ("embedded", stop); fr["pos"] = r.position
            fr["left"] = end - r.position; return fr, r
    fr["tail32"] = r.bits(32)
    fr["pos"] = r.position; fr["left"] = end - r.position
    return fr, r
