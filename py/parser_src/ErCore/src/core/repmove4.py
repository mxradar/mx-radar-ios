
# -*- coding: utf-8 -*-
"""repmove4.py - FRepMovementInfo item reader with FULL mask modelling (0x02, 0x04).

Mask 0x02 (from ida_sub_F1FD0A4.c @0xf1fdd38):
    NetGUID (SerializeIntPacked) -> +56
    FName  (Ar<<FName, vtable+16) -> +64
    1 bit  -> +97
    sub_F1FF2D8 : 3x u32 -> +84,+88,+92
    1 bit  -> +96
    1 bit  -> +98
Mask 0x04 (@0xf1fdd3c):
    1 bit  -> +132 ; 1 bit -> +134
    if (+132 or +134):
        if +132: 1 byte -> +133 ; PackedVector/10 -> +116..124
        if +134: 1 bit -> +135 ; 1 byte -> +136 ; PackedVector/10 -> +104..112
Mask 0x08 : u32 +144 ; 1b +142 ; 1b +140 ; 1b +141
Mask 0x20 : 32 bits -> +160
Mask 0x10 : sub_EAFCF10 object read (still stop)
"""
import sys
from pathlib import Path
import os
ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "src"))
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int, read_serialized_fname
from repmove3 import zz, Ctx, read_header, SEL_MASK, _pv, _rot, read_frame  # reuse frame logic

def read_item(r, ctx, allowed_masks=None, mask_bits=13):
    sel = r.bits(2)
    m = SEL_MASK.get(sel)
    # Current explicit masks occupy 13 bits. The old 12-bit read shifted
    # positions and every subsequent item; compact selector masks are unchanged.
    if m is None: m = r.bits(mask_bits)
    if allowed_masks is not None and m not in allowed_masks:
        raise BitError('unverified current movement mask 0x%x' % m)
    o = {"sel": sel, "mask": m}
    if m & 0x1000:
        return o, "m&0x1000"
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
        # f1fd0a4_pseudo.c @0xf1fdd38: sub_F1FCF50(NetGUID packed) ; vtable+16 FName ;
        #   bit(+97) ; sub_F1FF2D8 ; bit(+96) ; bit(+98).
        # sub_F1FF2D8 consumes 6x u32 (192 bits), NOT 3: measured on flow495 seq1291
        # item6 (mask 0xc3) — item7 true start = bit 814 = 619+1+192+1+1. 3xu32 left a
        # 96-bit residue that desynced every later item in the frame.
        o["guid"] = read_dfm_packed_int(r)[0]
        o["fname64"] = read_serialized_fname(r).as_dict()
        o["b97"] = r.bit()
        o["u32_84"] = [r.uint32() for _ in range(6)]
        o["b96"] = r.bit()
        o["b98"] = r.bit()
    if m & 0x004:
        # f1fd0a4_pseudo.c @0xf1fe2c0..0xf1fe528 (mask&4 section, engine-exact):
        #   bit(+132) ; bit(+134)
        #   if (+132||+134):
        #     if +132: byte(+133) ; PackedVector -> +116..124
        #     if +134: PackedVector -> +104..112 ; bit(+135) ; byte(+136)
        # NOTE: +104 vector is read BEFORE +135/+136 (0xf1fe4d8 -> 0xf1fe4f4 -> 0xf1fe520);
        # reading 135/136 first desyncs every later item in the frame.
        o["b132"] = r.bit()
        o["b134"] = r.bit()
        if o["b132"] or o["b134"]:
            if o["b132"]:
                o["b133"] = r.uint8()
                o["vec116"] = _pv(r)
            if o["b134"]:
                o["vec104"] = _pv(r)
                o["b135"] = r.bit()
                o["b136"] = r.uint8()
    if m & 0x008:
        o["u144"] = r.bits(32)
        o["b142"] = r.bit(); o["b140"] = r.bit(); o["b141"] = r.bit()
    if m & 0x020:
        o["u160"] = r.bits(32)
    if m & 0x010:
        return o, "m&0x10"
    return o, None

def read_frame4(payload, start, end, max_count=512, allowed_masks=None, mask_bits=13):
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
        it, stop = read_item(r, ctx, allowed_masks, mask_bits)
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
        it, stop = read_item(r, ectx, allowed_masks, mask_bits)
        fr["embedded"] = it
        if stop:
            fr["stopped"] = ("embedded", stop); fr["pos"] = r.position
            fr["left"] = end - r.position; return fr, r
    fr["tail32"] = r.bits(32)
    fr["pos"] = r.position; fr["left"] = end - r.position
    return fr, r
