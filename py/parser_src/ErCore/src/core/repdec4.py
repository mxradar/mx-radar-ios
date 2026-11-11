
# -*- coding: utf-8 -*-
"""repdec4.py - engine-exact FRepLayout::ReceiveProperties decoder.

Derived from the binary (sub_EAE4550 / RepLayout_ReceiveProperties_r @0xeae8ad4):
  * property handle  = FArchive vtable+128 = SerializeIntPacked   (LSB continuation)
  * dynamic ArrayNum = Serialize(&u16, 2)                          (raw uint16)
  * inner array handles are FLATTENED across elements: element i's inner cmd j
    has handle i*K + j (K = inner cmd count); a 0 handle closes the array scope.
  * the receiver walks every cmd in order incrementing CurrentHandle and only
    consumes the cmd whose handle == ReadHandle.
"""
import json, os, sys
from functools import lru_cache
from pathlib import Path
ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "src"))
import asset_crypto
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int, read_serialized_fname

TYPE = {0:"DynArray",1:"Return",2:"Property",3:"Bool",4:"Float",5:"Int",6:"Byte",7:"Name",
        8:"Object",9:"UInt32",10:"Vector",11:"Rotator",12:"Plane",13:"Vector100",14:"NetId",
        15:"RepMovement",16:"VecNormal",17:"Vector10",18:"VectorQ",19:"String",20:"UInt64",21:"NativeBool"}
POSTYPES = {10, 13, 15, 17, 18}

class Opaque(BitError): pass

# 类名别名：0904 更新 PS 类 BP_DFMPlayerState_C -> BP_SOLPlayerState_C，
# RepLayout 布局未变（80804 流实证 15 人全解出），仅类名/NetGUID 变。
# 新名缺 catalog 时回退旧名布局，保证新旧流量共用一份解码。
_CLASS_ALIAS = {"BP_SOLPlayerState_C": "BP_DFMPlayerState_C"}

@lru_cache(maxsize=1)
def load_catalogs():
    cls = {}
    for f in ("replayouts_R31.json", "subobj_replayouts_R32.json"):
        p = ROOT / "src/core" / f
        if p.exists():
            for cn, c in json.loads(asset_crypto.read_text(p)).get("classes", {}).items():
                cls[cn] = c
    for new, old in _CLASS_ALIAS.items():
        if new not in cls and old in cls:
            cls[new] = cls[old]
    return cls

@lru_cache(maxsize=1)
def load_propmeta():
    p = ROOT / "src/core" / "prop_meta_R36.json"
    pm = json.loads(asset_crypto.read_text(p)) if p.exists() else {}
    for new, old in _CLASS_ALIAS.items():
        if new not in pm and old in pm:
            pm[new] = pm[old]
    return pm

def cmd_list(cat):
    return sorted(cat["cmds"], key=lambda c: c["index"])

def pv(r, s, mb):
    h = r.serialize_int(mb); w = h + 2; bias = 1 << (h + 1)
    return [(r.bits(w) - bias) / s for _ in range(3)]
def rot_c(r, w=8):
    u = 360.0 / (1 << w)
    return [(r.bits(w) * u if r.bit() else 0.0) for _ in range(3)]

def st_quat(r):  return [r.float32() for _ in range(4)]
def st_vec2d(r): return [r.float32() for _ in range(2)]

def st_fastweapbp(r):
    """GPFastWeaponBPClassPath::NetSerialize（0829 反汇编定案 @0x7be99a5dc 区域）:
    u8 type; type!=0 → u64 fast_id（type=1=连接级类注册 ID，type=2/3 内联
    武器类 ID，可能与实例物品 ID 不同，不能作为 WeaponId 的回退）；type==0 → 全路径
    序列化（辅助调用 vtable+0x40，流内未观测——出现即 Opaque 保守停住，
    之前的 ev 保留、仅块标记 opaque，不猜位宽）。"""
    t = r.uint8()
    if t == 0:
        raise Opaque("FastWeaponBPClassPath type=0")
    return {"type": t, "fast_id": r.uint64()}

def st_invmgrrep(r):
    """InvMgrRepData::NetSerialize（0825 反汇编 @0xf841a24 定版布局）:
    bool32 + 6×U64raw + u32 f50 + u32 N1 + N1×{u32 a, u32 b, u32 N2,
    N2×{u32×5, u8, u8}}。sub stride=176 bits，元素无尾填充。
    a=槽类别(108001/107001/199997/109001 跨局恒定)，b=递增计数器，
    内层 u32×5 疑似物品 ID/数量/GUID 组。背包物品权威源。"""
    out = {"flags": r.uint32(),
           "u64s": [r.uint64() for _ in range(6)],
           "f50": r.uint32()}
    groups = []
    n1 = r.uint32()
    if n1 > 32: raise BitError("inventory group count")
    for _ in range(n1):
        g = {"a": r.uint32(), "b": r.uint32()}
        n2 = r.uint32()
        if n2 > 128: raise BitError("inventory item count")
        g["n2"] = n2
        g["items"] = [[r.uint32() for _ in range(5)] + [r.uint8(), r.uint8()]
                      for _ in range(n2)]
        groups.append(g)
    out["groups"] = groups
    return out

from weapon_native import weapon_extra, weapon_skin, weapon_dynamic_base, weapon_dynamic

STRUCTS = {"Quat": st_quat, "Vector2D": st_vec2d,
           "GPFastWeaponBPClassPath": st_fastweapbp,
           "WeaponReplicateExtraData": weapon_extra,
           "WeaponSkinInfo": weapon_skin,
           "ModularWeaponDynamicAttributesBase": weapon_dynamic_base,
           "ModularWeaponDynamicAttrbuites": weapon_dynamic,
           "InvMgrRepData": st_invmgrrep}

def read_prop(r, meta):
    if meta is None: raise Opaque("Property(no meta)")
    pc = meta.get("prop_class")
    if pc in ("MapProperty", "SetProperty"): return None
    if pc == "Int64Property":  return r.uint64()
    if pc == "UInt16Property": return r.bits(16)
    if pc == "Int16Property":
        v = r.bits(16); return v - 65536 if v & 0x8000 else v
    if pc == "Int8Property":
        v = r.uint8(); return v - 256 if v & 0x80 else v
    if pc == "StructProperty":
        fn = STRUCTS.get(meta.get("struct"))
        if fn: return fn(r)
        raise Opaque("struct " + str(meta.get("struct")))
    raise Opaque("propclass " + str(pc))

def read_val(r, t, meta=None):
    if t in (3, 21): return bool(r.bit())
    if t == 4: return r.float32()
    if t == 5: return r.int32()
    if t == 6: return r.uint8()
    if t == 7: return read_serialized_fname(r).as_dict()
    if t == 8: return {"guid": read_dfm_packed_int(r)[0]}
    if t == 9: return r.uint32()
    if t == 10: return [r.float32() for _ in range(3)]
    if t == 11: return rot_c(r)
    if t == 12: return [r.float32() for _ in range(4)]
    if t == 13: return pv(r, 100.0, 30)
    if t == 14:
        n, _ = read_dfm_packed_int(r)
        if n > 64: raise BitError("netid")
        return [r.uint8() for _ in range(n)]
    if t == 15:
        f = r.bits(2); loc = pv(r, 10.0, 24); rot = rot_c(r); vel = pv(r, 10.0, 24)
        return {"location": loc, "rotation": rot, "vel": vel}
    if t == 16: return [(r.bits(16) - 32768) for _ in range(3)]
    if t == 17: return pv(r, 10.0, 24)
    if t == 18: return pv(r, 1.0, 20)
    if t == 19: return r.fstring()
    if t == 20: return r.uint64()
    if t == 2: return read_prop(r, meta)
    raise BitError("type %d" % t)

class St:
    __slots__ = ("h",)
    def __init__(self, h): self.h = h

def _props_r(r, cmds, i0, i1, st, cur, ev, path, meta, depth):
    if depth > 8: raise BitError("depth")
    i = i0
    if i >= i1: return
    while True:
        cur[0] += 1
        c = cmds[i]
        if st.h != cur[0]:
            if c["type"] == 0:
                i = (c["end_cmd"] or (i + 2)) - 1
        else:
            if c["type"] != 0:
                v = read_val(r, c["type"], meta.get(c["index"]))
                ev.append({"path": list(path), "handle": cur[0], "type": c["type"],
                           "tname": TYPE.get(c["type"]), "value": v,
                           "cmd_index": c["index"], "parent_index": c["parent_index"]})
                st.h, _ = read_dfm_packed_int(r)
            else:
                n = r.uint16()
                st.h, _ = read_dfm_packed_int(r)
                if st.h:
                    if n > 2048: raise BitError("arraynum %d" % n)
                    end = (c["end_cmd"] or (i + 2)) - 1
                    icur = [0]
                    for e in range(n):
                        _props_r(r, cmds, i + 1, end, st, icur, ev, path + [(cur[0], e)], meta, depth + 1)
                    if st.h: raise BitError("array unterminated h%d" % st.h)
                i = (c["end_cmd"] or (i + 2)) - 1
                st.h, _ = read_dfm_packed_int(r)
        i += 1
        if i >= i1: return

def decode_block(payload, blk, cat, meta):
    cmds = cmd_list(cat)
    r = BitReader(payload, blk["payload_end"]); r.position = blk["payload_start"]
    ev = []
    try:
        h0, _ = read_dfm_packed_int(r)
        st = St(h0)
        _props_r(r, cmds, 0, len(cmds), st, [0], ev, [], meta, 0)
        if st.h != 0:
            return ev, r.position, "leftover-h%d" % st.h
    except Opaque:
        return ev, r.position, "opaque"
    except BitError:
        return ev, r.position, "error"
    if r.position == blk["payload_end"]: return ev, r.position, "exact"
    return ev, r.position, ("short" if r.position < blk["payload_end"] else "over")
