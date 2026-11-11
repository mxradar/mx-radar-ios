
# -*- coding: utf-8 -*-
"""er_live.py - deterministic network-layer name -> (x,y,z) driver.

自动识别战斗部服务器 + 纯确定性解析（无启发式/无扫描）。

Pipeline (bit-exact, verified against run_20260819_205832/flows/495):
  1. 战斗服自识别: 扫描 run 目录下全部 flow，取 PlayerState(arch=131) 通道最多的 flow。
  2. PlayerState 通道 -> 名字 / player_id / owner_guid / actor_guid (arch=131)。
  3. 角色 spawn (arch=105) -> h14 Owner / h18 PlayerState（内容块恒定在 ss+8，8-bit tail 之后）。
  4. 角色 spawn 位置 (SNA loc) -> movement slot 时间对齐 -> 亚米级 (x,y,z)。
  5. MovementReplicationActor 通道 (handle=21 数组) -> FRepMovementInfo 逐帧坐标。

Usage: python er_live.py <run_dir> [--json out.json]
"""
import json, sys
from collections import defaultdict
from pathlib import Path
import numpy as np

import os
ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "src/core"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from flowload import load_cached
from blocks import tile
from repdec4 import load_catalogs, load_propmeta, read_val as _r4_readval
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int, read_serialized_fname
import sna, repmove4 as rm4

ENUM_MAX = {"RemoteRole": 4, "Role": 4, "ReplicatedMovementMode": 8, "PlayerGender": 3}

def ser_int(r, vmax):
    v = 0; m = 1
    while (v + m) < vmax:
        if r.bit(): v |= m
        m <<= 1
    return v

def read_val(r, t, nm):
    if t in (3, 21): return bool(r.bit())
    if t == 4: return r.float32()
    if t == 5: return r.int32()
    if t == 6: return ser_int(r, ENUM_MAX[nm]) if nm in ENUM_MAX else r.uint8()
    if t == 7: return read_serialized_fname(r).as_dict()
    if t == 8: return read_dfm_packed_int(r)[0]
    if t == 9: return r.uint32()
    if t == 19: return r.fstring()
    if t == 20: return r.uint64()
    if t == 14:
        n, _ = read_dfm_packed_int(r); return bytes(r.uint8() for _ in range(n)).hex()
    if t in (0, 2): raise BitError("complex")
    raise BitError("t%d" % t)

def _decode_flat(payload, pstart, pend, cmds, parents, want, stop_after=None):
    """顺序 RepLayout 解码：bDoChecksum(1) + handle(packed) + 值... 提取 want 中的 handle。"""
    r = BitReader(payload, pend); r.position = pstart
    try: r.bit()
    except BitError: return {}
    cur = 0; out = {}
    try: h, _ = read_dfm_packed_int(r)
    except BitError: return out
    while True:
        c = None
        while cur < len(cmds):
            cur += 1
            cc = cmds[cur - 1]
            if cc["type"] == 0:
                cur = (cc["end_cmd"] or (cur + 1)) - 1; continue
            if cc["handle"] == h: c = cc; break
        if c is None: return out
        nm = parents[c["parent_index"]].get("name") if c["parent_index"] < len(parents) else "?"
        try: v = read_val(r, c["type"], nm)
        except BitError: return out
        if h in want: out[h] = v
        if stop_after is not None and h > stop_after: break
        try: h, _ = read_dfm_packed_int(r)
        except BitError: return out
        if h == 0: break
    return out


_PM_IDX = None


def _ps_prop_index():
    """BP_DFMPlayerState_C 的 prop_meta {cmd_index: meta}，懒加载一次。"""
    global _PM_IDX
    if _PM_IDX is None:
        try:
            m = load_propmeta().get("BP_DFMPlayerState_C") or []
            _PM_IDX = {it["index"]: it for it in m
                       if isinstance(it, dict) and "index" in it}
        except Exception:
            _PM_IDX = {}
    return _PM_IDX


def decode_playerstate_full(payload, pstart, pend, cmds, parents, want,
                            stop_after=63):
    """PlayerState 完整 RepLayout 解码（枚举感知 + 动态数组 walk）。

    _decode_flat 用 stop_after=30 是因为它的 read_val 遇到动态数组(type 0)
    会提前 return；TeamID(62)/Camp(63)/InnerTeamIndex(30) 在 30 之后（中间有
    MemberInfoList/SpectInfo 等动态数组），必须用完整 walk。

    本函数复刻 repdec6._props_r 的「cur 递增 + 数组 handle 跨元素展平（共享
    icur）」结构，但 read_val 用本模块的 read_val（type 6 枚举用 ser_int 变长
    编码，而非 uint8——repdec4/6 的 uint8 会让 RemoteRole 错位，实测 RemoteRole
    读成 113）。解到 stop_after 即停（62/63 之后是 SpectWeaponInfo/RTTReplayMs
    等 type 2 复杂字段，无需消费）。

    返回 {handle: value}（只含 want 中的 handle）。
    """
    r = BitReader(payload, pend); r.position = pstart
    pname = {i: p.get("name") for i, p in enumerate(parents)}
    out = {}
    pmi = _ps_prop_index()
    try:
        r.bit()  # bDoChecksum
        st = [read_dfm_packed_int(r)[0]]
    except BitError:
        return out

    def _props(i0, i1, cur, depth):
        if depth > 8:
            raise BitError("depth")
        i = i0
        while True:
            if cur[0] > stop_after:
                return
            cur[0] += 1
            c = cmds[i]
            if st[0] != cur[0]:
                if c["type"] == 0:
                    i = (c["end_cmd"] or (i + 2)) - 1
            else:
                if c["type"] != 0:
                    nm = pname.get(c["parent_index"], "?")
                    if c["type"] == 2:
                        # 通用 Property（prop_meta 驱动）：中途加入者的初始 PS 含
                        # h48 WeaponAmmoCount(UInt16)，旧码直接 BitError('complex')
                        # 炸掉 -> 排在其后的 TeamID(62)/Camp(63) 永远解不到（实测
                        # 4297 局 ch13/27/103 三人只有名字没队伍）。meta 缺失时
                        # Opaque(BitError 子类) 走原优雅降级路径。
                        v = _r4_readval(r, 2, pmi.get(c["index"]))
                    else:
                        v = read_val(r, c["type"], nm)
                    # 只在顶层（depth==0）写入 want 字段：数组（MemberInfoList
                    # 等）内部 handle 编号从 0 重新计数，与顶层 handle 编号
                    # 撞号（如数组元素 handle 21 == 顶层 bIsABot handle 21），
                    # 会把数组元素的值（38/796/...）误写入 out[21]，污染
                    # bot/pid/owner 判定（flow 1643 稳健医疗亡命峰被误判 AI）。
                    if depth == 0 and cur[0] in want:
                        out[cur[0]] = v
                    st[0] = read_dfm_packed_int(r)[0]
                else:
                    n = r.uint16()
                    st[0] = read_dfm_packed_int(r)[0]
                    if st[0]:
                        if n > 4096:
                            raise BitError("arraynum %d" % n)
                        end = (c["end_cmd"] or (i + 2)) - 1
                        icur = [0]   # 共享：数组 handle 跨元素展平
                        for _e in range(n):
                            _props(i + 1, end, icur, depth + 1)
                        if st[0]:
                            raise BitError("array unterminated h%d" % st[0])
                    i = (c["end_cmd"] or (i + 2)) - 1
                    st[0] = read_dfm_packed_int(r)[0]
            i += 1
            if i >= i1:
                return

    try:
        _props(0, len(cmds), [0], 0)
    except BitError:
        pass
    return out


def build_guid_paths(b):
    """全 flow export 段 -> guid -> path（类 GUID 按连接分配，必须用导出路径解析，
    不能硬编码 105/131 —— 不同连接编号不同，如 run_20260820_073050/272 角色原型=359）。"""
    paths = {}
    for ch in sorted(b):
        for x in b[ch]:
            if not x.open or not x.hx:
                continue
            bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
            try:
                info, _ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=False, frags=x.frags)
            except sna.Bad:
                continue
            for e in info.get("exports", []):
                p = e.get("path")
                if p:
                    g = e["guid"]
                    if g not in paths or len(p) > len(paths[g]):
                        paths[g] = p
    return paths


def resolve_class_archs(b):
    """从导出路径确定类 GUID 集合。原型引用既可能是完整包路径
    (/Game/.../BP_X.BP_X_C)，也可能是 CDO 名 (Default__BP_X_C)。
    返回 {"char":guids(人类), "ai":guids(AI角色), "ps":guids}。"""
    paths = build_guid_paths(b)
    archs = {"char": set(), "ai": set(), "ps": set()}
    for g, p in paths.items():
        tail = p.rsplit(".", 1)[-1]
        if tail.startswith("Default__"):
            tail = tail[9:]   # CDO 名 -> 类名
        if tail in ("BP_DFMCharacter_C", "BP_AZCharacter_C"):
            # 0904 新图(AZ-5)人类角色改用 BP_AZCharacter_C（RepLayout 基类
            # 布局与 BP_DFMCharacter_C 一致，h14/h18 同 handle，实证 80345）
            archs["char"].add(g)
        elif (tail.startswith(("BP_DFM", "BP_AZ")) and "Character" in tail
                and tail.endswith("_C")):
            archs["ai"].add(g)
        elif tail in ("BP_DFMPlayerState_C", "BP_SOLPlayerState_C"):
            # 0904 更新: PS 类改名 BP_SOLPlayerState_C（旧名兼容历史流量）
            archs["ps"].add(g)
    if not archs["char"]:
        # 旧数据兜底 105；但 0904 起该 guid 可能已让位给武器类
        # （80345 实证 105=BP_WeaponModular），仅当未解析成其他类时才用
        p105 = paths.get(105) or ""
        t105 = p105.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
        if not p105 or "Character" in t105:
            archs["char"].add(105)
    if not archs["ps"]:
        archs["ps"].add(131)
    return archs


def detect_battle_server(run_dir):
    """扫描 run 目录下全部 UDP flow，返回 PlayerState 通道最多的 flow 目录。"""
    run_dir = Path(run_dir)
    flows = run_dir / "flows"
    if not flows.exists():
        return None
    best = None; best_n = -1
    for fd in flows.iterdir():
        if not fd.is_dir(): continue
        try:
            b = load_cached(fd)
        except Exception:
            continue
        archs = resolve_class_archs(b)
        n = 0
        for ch in b.values():
            for x in ch:
                if not x.open: continue
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
                try: info, ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=False, frags=x.frags)
                except sna.Bad: continue
                if info.get("arch") in archs["ps"]: n += 1
                break
        if n > best_n:
            best_n = n; best = fd
    return best if best_n >= 1 else None


def extract_players(b, ps_archs=(131,)):
    """PlayerState -> 名字/player_id/owner/actor。"""
    CLS = load_catalogs()
    ps_cat = CLS["BP_DFMPlayerState_C"]
    ps_cmds = sorted(ps_cat["cmds"], key=lambda c: c["index"])
    ps_parents = ps_cat["parents"]
    players = {}
    for ch in sorted(b):
        for x in b[ch]:
            if not x.open: continue
            bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
            try: info, ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=False, frags=x.frags)
            except sna.Bad: continue
            if info.get("arch") not in ps_archs: continue
            blk = tile(x.payload, x.pbits, ss)
            if not blk: continue
            k = blk[0]
            out = decode_playerstate_full(x.payload, k["payload_start"], k["payload_end"],
                                          ps_cmds, ps_parents, {14, 18, 21, 27, 30, 62, 63, 137},
                                          stop_after=138)
            if 27 in out:
                players[info["actor"]] = {
                    "name": out[27], "player_id": out.get(18), "bot": out.get(21),
                    "actor_guid": info["actor"], "owner_guid": out.get(14), "channel": ch,
                    "team": out.get(62), "camp": out.get(63), "inner_team": out.get(30),
                    "hero_id": out.get(137)}
            break
    return players


def decode_character_identity(payload, pstart, pend, cmds, parents):
    """Decode the unchanged Character prefix through h60 MoveHandle.Index.

    Runtime CppStructOps NetSerialize at 0xC74D370 calls SerializeIntPacked.
    RemoteRole/Role are SerializeInt(4); ReplicatedMovementMode is a raw byte.
    Unknown native structs stop the walk, preserving only the proven prefix.
    """
    import repdec4
    r = BitReader(payload, pend)
    r.position = pstart
    fields = {}
    last = 0
    try:
        if r.bit():
            return fields  # checked properties have not been verified
        while True:
            h = read_dfm_packed_int(r)[0]
            if h == 0 or h > 60:
                return fields
            if h <= last or h > len(cmds):
                return {}  # malformed prefix cannot establish identity
            cmd = cmds[h - 1]
            if h == 60:
                if parents[cmd["parent_index"]]["name"] != "MoveHandle":
                    return fields
                value = read_dfm_packed_int(r)[0]
                if value > 0xffffffff:
                    return {}
                if value & 0x80000000:
                    value -= 1 << 32
            elif h in (6, 15):
                value = r.serialize_int(4)
            elif cmd["type"] == 8:
                value = read_dfm_packed_int(r)[0]
            else:
                value = repdec4.read_val(r, cmd["type"])
            if h in (14, 18, 60):
                fields[h] = value
            last = h
    except repdec4.Opaque:
        return fields
    except BitError:
        return {}  # truncated values never create a partial identity update


def merge_character_identity(link, fields, seq):
    for h, key in ((14, "owner"), (18, "ps_guid"), (60, "move_handle")):
        if h in fields and seq >= link.get(key + "_seq", -1):
            link[key] = fields[h]
            link[key + "_seq"] = seq
            if h == 60:
                history = link.setdefault("move_handle_history", [])
                if not history or history[-1] != (seq, fields[h]):
                    history.append((seq, fields[h]))


def explicit_slot_claims(links, at_seq=None):
    """Latest explicit assignment wins slot reuse; -1 never falls back to geometry."""
    candidates = {}
    current = {}
    for actor, link in links.items():
        slot = link.get("move_handle")
        assignment_seq = link.get("move_handle_seq", link.get("spawn_seq", -1))
        history = link.get("move_handle_history", [(assignment_seq, slot)])
        points = [point for point in history if at_seq is None or point[0] <= at_seq]
        if not points:
            continue
        current[actor] = points[-1][1]
        for stamp, slot in points:
            if not isinstance(slot, int) or slot < 0:
                continue
            order = (stamp, link.get("spawn_seq", -1))
            prev = candidates.get(slot)
            if prev is None or order > prev[0]:
                candidates[slot] = (order, actor)
            elif order == prev[0] and prev[1] != actor:
                candidates[slot] = (order, None)
    return {slot: actor for slot, (_, actor) in candidates.items()
            if actor is not None and current.get(actor) == slot}


def observed_move_slots(links):
    return {slot for link in links.values()
            for _, slot in link.get("move_handle_history", [(0, link.get("move_handle"))])
            if isinstance(slot, int) and slot >= 0}


def extract_char_links(b, char_archs=(105,), with_hero=True):
    """角色 -> h14 Owner / h18 PlayerState（内容块恒定 ss+8）。

    with_hero=True 时顺带从角色子对象 DFMCharacterFashionComponent 的
    currentCharacterHeroID 解出 hero_id（0904 新版 HeroId 不再走 PS h137，
    该组件新旧版本都在复制）写入 links[a]["hero"]；同一遍 parse 完成，
    不增加额外全量扫描。"""
    CLS = load_catalogs()
    char_cat = CLS["BP_DFMCharacter_C"]
    char_cmds = sorted(char_cat["cmds"], key=lambda c: c["index"])
    char_parents = char_cat["parents"]
    f_cat = CLS.get("DFMCharacterFashionComponent") if with_hero else None
    f_cmds = sorted(f_cat["cmds"], key=lambda c: c["index"]) if f_cat else None
    f_parents = f_cat["parents"] if f_cat else None
    want_h = _fashion_hero_handle(f_cmds, f_parents) if f_cat else None
    fname = ({i: p.get("name") for i, p in enumerate(f_parents)}
             if f_cat else {})
    links = {}   # char_actor -> (owner, ps_guid, spawn_seq, loc)
    paths = {}   # guid -> path（fashion 组件识别用，同一遍顺带收集）
    f_cand = []  # (actor, blk, payload_ref) fashion 块候选，出口统一解
    from replication_header import exports_prefix
    channels = {}
    for x in sorted((x for xs in b.values() for x in xs), key=lambda x: x.seq):
        actor = channels.get(x.ch)
        if not x.open:
            if actor is not None and not x.close:
                try:
                    _, ss = exports_prefix(x)
                    for bk in tile(x.payload, x.pbits, ss) or []:
                        if bk["is_actor"] and bk["has_rep"]:
                            out = decode_character_identity(x.payload,
                                bk["payload_start"], bk["payload_end"], char_cmds, char_parents)
                            merge_character_identity(links[actor], out, x.seq)
                except (sna.Bad, IndexError):
                    pass
            if x.close:
                actor = None
                channels.pop(x.ch, None)
            continue
        actor = None  # every open starts a new channel generation
        channels.pop(x.ch, None)
        bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
        try: info, ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=True, frags=x.frags)
        except sna.Bad: continue
        for e in info.get("exports", []):
            p = e.get("path")
            if p:
                g = e["guid"]
                if g not in paths or len(p) > len(paths[g]):
                    paths[g] = p
        if info.get("arch") not in char_archs: continue
        a = info.get("actor"); loc = info.get("loc")
        if not a: continue
        actor = a
        channels[x.ch] = a
        # 内容块在 ss+8（8-bit ProcessBunch tail 之后，确定性）
        blk = tile(x.payload, x.pbits, ss + 8, partial_ok=True)
        out = {}
        if blk and blk[0].get("is_actor") == 1 and blk[0].get("has_rep") == 1:
            out = decode_character_identity(x.payload, blk[0]["payload_start"],
                               blk[0]["payload_end"], char_cmds, char_parents)
        if with_hero and blk and want_h is not None:
            for bk in blk:
                if bk.get("deleted") or bk.get("sub") is None:
                    continue
                if bk["payload_end"] <= bk["payload_start"]:
                    continue
                f_cand.append((a, bk["sub"], x, bk))
        if a not in links:
            # 优先保留首个带 loc 的 spawn（时间对齐用），links 单独从首个带 h14 的 spawn 取
            links[a] = {"owner": out.get(14), "ps_guid": out.get(18), "spawn_seq": x.seq, "loc": loc, "num_exports": info.get("num_exports")}
        else:
            if links[a]["owner"] is None and out.get(14) is not None:
                links[a]["owner"] = out.get(14)
            if links[a]["ps_guid"] is None and out.get(18) is not None:
                links[a]["ps_guid"] = out.get(18)
        merge_character_identity(links[a], out, x.seq)
    if f_cand and want_h is not None:
        fashion = {g for g, p in paths.items()
                   if "CharacterFashion" in p.rsplit("/", 1)[-1]}
        for a, sub_g, x, bk in f_cand:
            if sub_g not in fashion:
                continue
            hid = _walk_fashion_hero(x.payload, bk["payload_start"], bk["payload_end"],
                                     f_cmds, fname, want_h)
            if hid is not None and links.get(a, {}).get("hero") is None:
                links[a]["hero"] = hid
    return links


def _fashion_hero_handle(cmds, parents):
    """DFMCharacterFashionComponent 里 currentCharacterHeroID 的顶层 wire handle。"""
    inner = set()
    for c in cmds:
        if c["type"] == 0 and c.get("end_cmd"):
            inner.update(range(c["index"] + 1, c["end_cmd"]))
    h = 0
    for c in cmds:
        if c["index"] in inner:
            continue
        h += 1
        pi = c.get("parent_index")
        if 0 <= pi < len(parents) and parents[pi].get("name") == "currentCharacterHeroID":
            return h
    return None


def _walk_fashion_hero(payload, pstart, pend, f_cmds, fname, want_h):
    """fashion 组件块 payload -> currentCharacterHeroID（88000000001..99 之外
    的值视为错位丢弃）。"""
    r = BitReader(payload, pend)
    r.position = pstart
    try:
        r.bit()
        st = [read_dfm_packed_int(r)[0]]
    except BitError:
        return None
    cur = [0]
    hid = None

    def _p(i0, i1, cu, d):
        nonlocal hid
        if d > 4:
            raise BitError("depth")
        i = i0
        while True:
            if cu[0] > want_h:
                return
            cu[0] += 1
            c = f_cmds[i]
            if st[0] != cu[0]:
                if c["type"] == 0:
                    i = (c.get("end_cmd") or (i + 2)) - 1
            else:
                if c["type"] != 0:
                    nm = fname.get(c.get("parent_index"), "?")
                    v = read_val(r, c["type"], nm)
                    is_hero = (cu[0] == want_h and isinstance(v, int)
                               and 88000000001 <= v <= 88000000099)
                    if is_hero:
                        hid = v
                    try:
                        st[0] = read_dfm_packed_int(r)[0]
                    except BitError:
                        if is_hero:
                            return
                        raise
                else:
                    n = r.uint16()
                    st[0] = read_dfm_packed_int(r)[0]
                    if st[0]:
                        end = (c.get("end_cmd") or (i + 2)) - 1
                        icu = [0]
                        for _e in range(n):
                            _p(i + 1, end, icu, d + 1)
                        if st[0]:
                            raise BitError("arr")
                    i = (c.get("end_cmd") or (i + 2)) - 1
                    st[0] = read_dfm_packed_int(r)[0]
            i += 1
            if i >= i1:
                return
    try:
        _p(0, len(f_cmds), cur, 0)
    except BitError:
        return hid
    except Exception:
        return hid
    return hid


def extract_char_hero(b, char_archs=None):
    """角色 actor -> hero_id（独立入口；与 extract_char_links 的 with_hero
    同一路径，避免二次全量扫描）。

    新版 HeroId 不再经 PlayerState h137 复制（PS 布局未变，但 join 全量后
    服务器不再发送 h129 之后的内容，实测如此），改由角色子对象
    DFMCharacterFashionComponent.currentCharacterHeroID 承载；旧版流量
    同样复制该组件，统一从这里读，PS h137 保留为兜底。
    返回 {char_actor_guid: hero_id}。"""
    CLS = load_catalogs()
    f_cat = CLS.get("DFMCharacterFashionComponent")
    if not f_cat:
        return {}
    f_cmds = sorted(f_cat["cmds"], key=lambda c: c["index"])
    f_parents = f_cat["parents"]
    want_h = _fashion_hero_handle(f_cmds, f_parents)
    if want_h is None:
        return {}
    fname = {i: p.get("name") for i, p in enumerate(f_parents)}
    fashion = {g for g, p in build_guid_paths(b).items()
               if "CharacterFashion" in p.rsplit("/", 1)[-1]}
    if not fashion:
        return {}
    out = {}
    for ch in sorted(b):
        for x in b[ch]:
            if not x.open or not x.hx:
                continue
            bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8),
                                 bitorder="little")[:x.pbits]
            try:
                info, ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=True, frags=x.frags)
            except sna.Bad:
                continue
            a = info.get("actor")
            if not a:
                continue
            try:
                blks = tile(x.payload, x.pbits, ss + 8, partial_ok=True)
            except Exception:
                continue
            if not blks:
                continue
            for bk in blks:
                if bk.get("deleted") or bk.get("sub") not in fashion:
                    continue
                if bk["payload_end"] <= bk["payload_start"]:
                    continue
                hid = _walk_fashion_hero(x.payload, bk["payload_start"],
                                         bk["payload_end"], f_cmds, fname, want_h)
                if hid is not None:
                    out.setdefault(a, hid)
    return out


def extract_slots(b, mra_ch):
    """MovementReplicationActor 通道 -> 每个 slot 的 (seq -> pos) 轨迹。"""
    slots = defaultdict(list)
    for x in b.get(mra_ch, []):
        blk = tile(x.payload, x.pbits, 0)
        if not blk: continue
        for k in blk:
            if not k["has_rep"] or k["deleted"] or not k["is_actor"]: continue
            r0 = BitReader(x.payload, k["payload_end"]); r0.position = k["payload_start"]
            try:
                if r0.bit(): continue
                h, _ = read_dfm_packed_int(r0)
                if h != 21: continue
                fr, _ = rm4.read_frame4(x.payload, r0.position, k["payload_end"])
            except Exception:
                continue
            for i, it in fr["items"].items():
                if "pos" in it:
                    slots[i].append((x.seq, it["pos"]))
    return slots


def find_mra_channel(b):
    best = None; best_n = 0
    for ch in sorted(b):
        n = 0
        for x in b.get(ch, []):
            blk = tile(x.payload, x.pbits, 0)
            if not blk: continue
            for k in blk:
                if not k["has_rep"] or k["deleted"] or not k["is_actor"]: continue
                r0 = BitReader(x.payload, k["payload_end"]); r0.position = k["payload_start"]
                try:
                    if r0.bit(): continue
                    h, _ = read_dfm_packed_int(r0)
                    if h == 21: n += 1
                except BitError: pass
        if n > best_n: best_n = n; best = ch
    return best, best_n


def main(run_dir, out_json=None):
    flow = detect_battle_server(run_dir)
    if flow is None:
        print("NO battle server flow found in", run_dir); return None
    print("battle server flow:", flow.name)
    b = load_cached(flow)
    mra_ch, mra_n = find_mra_channel(b)
    print("MovementReplicationActor channel:", mra_ch, "(blocks=%d)" % mra_n)

    archs = resolve_class_archs(b)
    players = extract_players(b, ps_archs=archs["ps"])
    links = extract_char_links(b, char_archs=archs["char"])
    slots = extract_slots(b, mra_ch)

    # join: char spawn loc -> nearest slot at spawn seq (temporal)
    results = []
    for actor, lk in links.items():
        ps_guid = lk["ps_guid"]; owner = lk["owner"]
        # resolve name via PlayerState actor_guid == h18, or owner_guid == h14
        name = None
        for psg, p in players.items():
            if ps_guid is not None and psg == ps_guid: name = p["name"]
        if name is None:
            for psg, p in players.items():
                if owner is not None and p["owner_guid"] == owner: name = p["name"]
        row = {"name": name, "char_actor": actor, "ps_guid": ps_guid, "owner": owner,
               "spawn_loc": lk["loc"], "spawn_seq": lk["spawn_seq"], "slot": None, "coord": None, "join_dist_cm": None,
               "num_exports": lk.get("num_exports")}
        direct = lk.get("move_handle")
        if isinstance(direct, int) and direct >= 0:
            row["slot"] = direct
            row["binding_source"] = "move_handle"
            if slots.get(direct):
                row["coord"] = [int(v) for v in slots[direct][-1][1]]
        elif "move_handle" not in lk and lk["loc"] and slots:
            S = lk["spawn_seq"]; loc = np.array(lk["loc"], dtype=float)
            best_slot = None; best_d = 1e18; best_pos = None
            for slot, tracks in slots.items():
                if not tracks: continue
                near = min(tracks, key=lambda t: abs(t[0] - S))
                pos = np.array(near[1], dtype=float)
                d = float(np.linalg.norm(loc - pos))
                if d < best_d: best_d = d; best_slot = slot; best_pos = near[1]
            if best_slot is not None and best_d < 500.0:
                row["slot"] = best_slot; row["join_dist_cm"] = round(best_d, 1)
                row["binding_source"] = "spawn_proximity"
                row["coord"] = [int(v) for v in best_pos]
        results.append(row)

    # 上行自机移动通道（确定性规则：317 位 ServerMove 家族 bunch 最多的 request 通道）
    self_uplink = None
    try:
        import struct as _s
        bq_req = load_cached(flow, "request")
        best_ch, best_n = None, 0
        for ch in sorted(bq_req):
            n317 = sum(1 for x in bq_req[ch] if x.pbits == 317)
            if n317 > best_n:
                best_n, best_ch = n317, ch
        if best_ch is not None and best_n >= 1000:
            mvs = [x for x in bq_req[best_ch] if x.pbits == 317]
            def _ts(x):
                raw = (int.from_bytes(x.payload, "little") >> 54) & 0xFFFFFFFF
                return _s.unpack("<f", _s.pack("<I", raw))[0]
            t_head = [_ts(x) for x in mvs[:50]]
            t_tail = [_ts(x) for x in mvs[-50:]]
            self_uplink = {"channel": best_ch, "moves": len(mvs),
                           "n317": best_n,
                           "ts_first": round(min(t_head), 2), "ts_last": round(max(t_tail), 2)}
    except Exception as e:
        print("self uplink probe failed:", e)

    # 识别 self（本地玩家）：arch=105 且 loc=None（loc 标志=0）且 export 数=28 的角色即"自己"。
    # 本地玩家自己的位置本地已知，不走网络复制，所以网络层没有"自己"的坐标（属正常）。
    # 其名字来自真实玩家(bot=null)中 player_id 最小者。char=72(DFMParachute, 29 exports) 是降落伞非玩家，排除。
    self_name = None
    for psg, p in players.items():
        if p.get("bot") is None and (self_name is None or (p.get("player_id") or 1 << 30) < self_name[1]):
            self_name = (p["name"], p.get("player_id") or 1 << 30)
    for r in results:
        if r["name"] is None and r["spawn_loc"] is None and r.get("num_exports") == 28 and self_name:
            r["name"] = self_name[0] + "(self)"
            r["is_self"] = True
            if self_uplink:
                r["uplink"] = self_uplink

    # print report
    print("\n=== 网络层 name -> (x,y,z) ===")
    for r in sorted(results, key=lambda r: (r["name"] is None, r["name"] or "")):
        coord = r["coord"]
        if coord:
            cstr = "[%d,%d,%d]" % tuple(coord)
        elif r["spawn_loc"]:
            cstr = "出生点[%d,%d,%d]" % tuple(int(v) for v in r["spawn_loc"])
        else:
            cstr = "(无数据)"
        print("  %-14s char=%-5s slot=%-3s %s  %s" % (r["name"], r["char_actor"],
              r["slot"] if r["slot"] is not None else "-", cstr,
              "join=%dcm" % r["join_dist_cm"] if r["join_dist_cm"] else ""))
    for r in results:
        if r.get("is_self") and r.get("uplink"):
            u = r["uplink"]
            print("  self uplink: ch=%s moves=%d(317bit) ts=[%s..%s]s  (位置字段编码待 GP 模块 dump，见 findings §14)"
                  % (u["channel"], u["moves"], u["ts_first"], u["ts_last"]))

    mapped = [r for r in results if r["coord"] is not None]
    print("\n%d players, %d name->char, %d name->(x,y,z)" % (len(players), len([r for r in results if r["name"]]), len(mapped)))

    if out_json:
        Path(out_json).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print("wrote", out_json)
    return results


if __name__ == "__main__":
    run = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "captures" / "run_20260819_205832"
    oj = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    main(run, oj)
