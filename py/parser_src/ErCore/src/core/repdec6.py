
# -*- coding: utf-8 -*-
"""repdec6.py - engine-exact FRepLayout::ReceiveProperties decoder (FINAL framing).

Recovered from the binary:
  sub_EAE4550 @0xeae469c : bDoChecksum = Bunch.ReadBit()      <-- ONE leading bit
  handle      = FArchive vtable+128 = SerializeIntPacked (LSB continuation)
  if bDoChecksum: 4 extra bytes (uint32 checksum) after every handle
  ArrayNum    = Serialize(&uint16, 2)
  array inner handles are FLATTENED across elements (shared CurrentHandle)
  receiver walks every cmd incrementing CurrentHandle, consuming only the match
  stream ends with handle 0
"""
import json, os, sys
from pathlib import Path
ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "src"))
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int
from repdec4 import TYPE, POSTYPES, Opaque, load_catalogs, load_propmeta, cmd_list, read_val

class St:
    __slots__ = ("h", "cs")
    def __init__(self, h, cs): self.h = h; self.cs = cs

def _rh(r, st):
    h, _ = read_dfm_packed_int(r)
    if st.cs: r.bits(32)
    return h

def _props_r(r, cmds, i0, i1, st, cur, ev, path, meta, depth,
             value_reader, include_arrays):
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
                v = value_reader(r, c["type"], meta.get(c["index"]))
                ev.append({"path": list(path), "handle": cur[0], "type": c["type"],
                           "tname": TYPE.get(c["type"]), "value": v,
                           "cmd_index": c["index"], "parent_index": c["parent_index"]})
                st.h = _rh(r, st)
            else:
                n = r.uint16()
                if n > 4096: raise BitError("arraynum %d" % n)
                ae = {"path": list(path), "handle": cur[0], "type": 0,
                      "tname": "DynArray", "value": n,
                      "cmd_index": c["index"], "parent_index": c["parent_index"]}
                if include_arrays:
                    ev.append(dict(ae, kind="array_begin"))
                st.h = _rh(r, st)
                if st.h:
                    end = (c["end_cmd"] or (i + 2)) - 1
                    icur = [0]
                    for e in range(n):
                        _props_r(r, cmds, i + 1, end, st, icur, ev,
                                 path + [(cur[0], e)], meta, depth + 1,
                                 value_reader, include_arrays)
                    if st.h: raise BitError("array unterminated h%d" % st.h)
                if include_arrays:
                    ev.append(dict(ae, kind="array_end"))
                i = (c["end_cmd"] or (i + 2)) - 1
                st.h = _rh(r, st)
        i += 1
        if i >= i1: return

_PM_CACHE = {}

def _cls_meta(cls_name):
    """类名 -> {cmd index: prop meta}（prop_meta_R36.json，懒加载缓存）。"""
    if cls_name in _PM_CACHE:
        return _PM_CACHE[cls_name]
    meta = {}
    try:
        for it in load_propmeta().get(cls_name, []):
            if isinstance(it, dict) and "index" in it:
                meta[it["index"]] = it
    except Exception:
        meta = {}
    _PM_CACHE[cls_name] = meta
    return meta

def decode_block(payload, blk, cat, meta, *, include_arrays=False, value_reader=None):
    cmds = cmd_list(cat)
    if not meta:
        # 0829 装备链路修复：历史调用全部传 {}，prop_meta 从未接线——
        # StructProperty（GPFastWeaponBPClassPath 等）一律 Opaque，CurrActive
        # WeapInfoArr 的当前武器信息被整块丢弃（0825 误判"从不上 wire"根因）。
        # 无调用方 meta 时按类名自动挂 prop_meta_R36 的 {index: prop}。
        meta = _cls_meta(cat.get("class"))
    r = BitReader(payload, blk["payload_end"]); r.position = blk["payload_start"]
    ev = []
    try:
        cs = r.bit()
        st = St(0, cs)
        st.h = _rh(r, st)
        _props_r(r, cmds, 0, len(cmds), st, [0], ev, [], meta, 0,
                 value_reader or read_val, include_arrays)
        if st.h != 0: return ev, r.position, "leftover"
    except Opaque:
        return ev, r.position, "opaque"
    except BitError:
        return ev, r.position, "error"
    if r.position == blk["payload_end"]: return ev, r.position, "exact"
    return ev, r.position, ("short" if r.position < blk["payload_end"] else "over")
