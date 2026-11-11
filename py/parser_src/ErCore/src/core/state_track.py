# -*- coding: utf-8 -*-
"""state_track.py - 角色状态 / 死亡 / 消失 的确定性逆向提取。

逆向结论（flow 495 实证 + UE4.26 客户端源码 ue_DataChannel.cpp / engine_bunch.py）：

1. MRA presence 是「移动驱动」的 —— 蹲点不动的活人也会长时间静默
   （实测 gap 最长 23510 seq ≈ 7 分钟，位置 dist=0 完全不变）。
   因此「静默时长」**不能**用来判定角色消失：蹲点活人和死人/离开者都静默。

2. 角色「消失」的权威信号 = 角色自身通道的 **close bunch 的 close_reason**：
   UE4 的 EChannelCloseReason 枚举（engine_bunch.py 已解析）：
       0 = Destroyed      （销毁）
       1 = Dormancy       （休眠 —— 子对象/组件，角色保留）
       2 = LevelUnloaded  （关卡卸载）
       3 = Relevancy      （离开更新距离 —— 点消失）
       4 = TearOff        （死亡 tearoff —— 点消失 + 盒子）
   客户端 UActorChannel::CleanUp 按 close_reason 分流：
       TearOff -> Actor->TornOff()（留盒子）
       Dormancy -> DORM_DormantAll（保留，冻结）
       Destroyed/Relevancy -> DestroyActorAndComponents()（销毁）
   故移除/保留规则：
       close_reason ∈ {0,2,3,4} -> 角色消失，点消失
       close_reason == 1 或无 close -> 角色保留（蹲点/休眠）

3. 死亡盒 BP_Inventory_DeadBody_C 的 spawn 是死亡盒的权威位置（客户端只渲染
   盒子不渲染尸体）。死亡盒 spawn 位置 ≈ 角色最终采样位置（实测 0cm 级吻合）。

4. 角色状态（FSM / 战斗状态）
   DFMCharacterFSM_Main_C.R_ActiveStates + GPFightStateComponent 的
   CurrentFightStateType / CurrentMotionType / CurrentAlertState / CurrentInCoverState。
   目录来自 subobj_replayouts_R32.json（round 43 活取，97 类）。
"""
from __future__ import annotations
from pathlib import Path
import numpy as np

import os
ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])

# 死亡盒与 slot 最终位置的匹配半径（cm）。实测 0~231cm，取 300 覆盖。
DEATH_MATCH_CM = 300.0

# 角色消失的 close_reason 集合（UE4 EChannelCloseReason）。
# 1=Dormancy 是子对象休眠，角色保留（蹲点），排除在外。
# 原理依据（dfm-ue4 工作区 PRINCIPLES_er_support.md §1）：
#   _reflect.json 0x7bf286e648/0x7bf286e718 = UChannel::CloseReason /
#   FChannelCloseContext（UActorChannel::CleanUp 实参），close 事件确实携带
#   close_reason 到达客户端；engine_bunch.py:266 wire 侧 serialize_int(15) 解析。
GONE_CLOSE_REASONS = {0, 2, 3, 4}

# 「死亡/销毁」类 close_reason（点永久消失，槽可能被新人复用）。
# Relevancy(3) 不在内：离开更新距离只是看不到，角色本体还活着，
# 实测 495 局 slot4 Relevancy@34836 后槽仍收新采样 4000+ seq（槽被复用）。
# 原理依据：UE4 EChannelCloseReason 枚举 4=TearOff（死亡）+ 角色 RepLayout
#   parent3=bTearOff（character_replayout_catalog_live.json）+ TornOffOldLocation
#   （properties_all.json 角色类 0x7bf3d1d2f0）= TearOff 冻结最后位置 → 死亡盒。
DEAD_CLOSE_REASONS = {0, 4}

# EFSMCharacterState 枚举 → 中文显示名（dfm-ue4 regscan_targets.txt 实证，
# 64 值顺序 = 枚举 ordinal）。用于前端把 fsm_states 的数字渲染成可读状态。
# 原理依据：DFMCharacterFSM_Main_C.R_ActiveStates_Client: TArray<FGuid>
#   （dfm-ue4 _reflect.json 0x7bf28e7370 off=1800）携带活动状态实例；
#   实际状态语义 = EFSMCharacterState（FSMPlayerState_* 64 值）。
FSM_STATE_NAMES = {
    0: "无", 1: "瞄准", 2: "探身", 3: "换弹", 4: "开火", 5: "检视武器",
    6: "架枪", 7: "卧倒", 8: "蹲伏", 9: "站立", 10: "冲刺",
    11: "滑铲", 12: "特殊移动", 13: "游泳", 14: "冲刺游泳", 15: "标记",
    16: "搜物资", 17: "背尸体", 18: "放下尸体", 19: "救援", 20: "被救援",
    21: "交互", 22: "精确交互", 23: "延迟交互", 24: "游戏操作", 25: "技能",
    26: "双手技能", 27: "左手技能", 28: "右手技能", 29: "腿部技能", 30: "特殊技能",
    31: "动作技能", 32: "自定义技能1", 33: "自定义技能2", 34: "自定义技能3", 35: "载具",
    36: "场景武器", 37: "使用物品", 38: "灭火", 39: "检视高价值物品", 40: "头盔",
    41: "受击", 42: "濒死", 43: "死亡", 44: "过场", 45: "暗杀",
    46: "被暗杀", 47: "单手手势", 48: "双手手势", 49: "表情", 50: "喷涂",
    51: "左臂动画", 52: "潜水", 53: "占旗", 54: "被动移动", 55: "等待开始",
    56: "退出游戏", 57: "受控", 58: "冻结", 59: "地震摔倒", 60: "推倒",
    61: "麻醉摔倒", 62: "交互表情", 63: "拖拽救援",
}
# 未知值兜底
FSM_STATE_UNKNOWN = "?"

# wire 类尾名 -> 目录类名（subobj_replayouts_R32.json 的 key）
TAIL2CAT = {
    "GPAttributeSetHealth": "GPAttributeSetHealth",
    "GPAttributeSetSpeed": "GPAttributeSetSpeed",
    "GPAttributeSetOther": "GPAttributeSetOther",
    "HealthComp": "SOLHealthDataComponent",
    "FightStateComp": "GPFightStateComponent",
    "DFMCharacterFSM_Main_C": "DFMCharacterFSM_Main_C",
    "AttributeCenter": "DFMCharacterAttributeCenterComponent",
    "GPRagDollPhysicsComponentKinematicHip": "GPRagDollPhysicsComponentKinematicHip",
    # 护甲装备 + 武器组件（6870 局实证：导出路径只带叶子名——角色身上的
    # 组件变量名 CharacterEquip / WeaponReplicate；个别实例带全类名）。
    "CharacterEquip": "SOLCharacterEquipComponent",
    "WeaponReplicate": "WeaponReplicateComponent",
    # 背包/物品管理器（RepItemArray 携带完整 11 位 item id + 实例 GUID）
    "InventoryManager": "InventoryManager",
    # 多武器管理器（WeaponIDList/WeaponIdentityList/WeaponSlot = 武器 ID 权威源）
    "WeaponManager": "MultiWeaponManagerComponent",
    # 角色时装组件（0904 起 currentCharacterHeroID 从 PS h137 移到这里复制）
    "CharacterFashionComponent": "DFMCharacterFashionComponent",
}


def slot_staleness(slots, cur_seq):
    """slots: {slot: [(seq, ts, x, y, z, yaw), ...]} -> {slot: 静默 seq 差}。

    返回每个 slot 距最后一次采样的 seq 差（>0 表示已静默）。cur_seq 为当前游标。
    注意：这只是信息字段（蹲点活人也会静默），**不用于移除判定**——
    移除由角色通道 close_reason 驱动（extract_gone_actors）。
    """
    out = {}
    for s, tr in slots.items():
        if not tr:
            out[s] = None
            continue
        last = tr[-1][0]
        out[s] = max(0, cur_seq - last)
    return out


def correlate_death_boxes(boxes, slots, match_cm=DEATH_MATCH_CM,
                          _cache=None):
    """boxes: [(seq, [x,y,z]), ...]; slots: {slot: [(seq,ts,x,y,z,yaw),...]}。

    返回 {slot: (box_seq, dist_cm, [x,y,z])} —— 该 slot 角色「死在该处」。

    性能：每次 /api/state（~2.5Hz）都会调用。判定只依赖 (slot 最终采样, boxes)，
    两者在 poll 间隙几乎不变，故按 (len(tr), boxes_len, match_cm) 指纹做
    字典缓存——指纹未变即返回缓存结果，避免每次全量 O(slots×boxes) 距离扫描。
    （bit-exact：结果与无缓存路径完全一致；指纹变化即重算。）
    """
    if _cache is None:
        # 兼容旧调用（无缓存）：行为与原版一致（每次全量算）
        return _correlate_death_boxes_uncached(boxes, slots, match_cm)
    key = []
    for s, tr in slots.items():
        key.append((s, len(tr), tr[-1][0] if tr else -1))
    key.sort()
    key.append((-1, len(boxes), int(match_cm)))
    key = tuple(key)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    res = _correlate_death_boxes_uncached(boxes, slots, match_cm)
    _cache[key] = res
    # 防膨胀：指纹无界增长时丢弃最旧一半
    if len(_cache) > 256:
        for k in list(_cache)[:128]:
            _cache.pop(k, None)
    return res


def _correlate_death_boxes_uncached(boxes, slots, match_cm=DEATH_MATCH_CM):
    """(无缓存实现 — 与 correlate_death_boxes 语义完全一致)

    匹配规则两级：
      ① 盒子晚于最终采样（旧规则）——盒子在角色停止移动之后出现。
      ② 冻结回退（6870 局实证补充）：死亡后 MRA 会继续重播冻结位置数百 seq，
         最后采样常晚于盒子 seq 但位置从未移动（1560: 盒@9336 与最终点 0cm
         重合、last_seq=9374）。此时只要盒子之后的**全部**采样都贴着盒子
         (≤match_cm)，同样判死；一旦有采样远离盒子 = 人后来还在动，拒绝。
    """
    dead = {}
    for s, tr in slots.items():
        if not tr:
            continue
        last_seq, _ts, lx, ly, _lz, _yaw = tr[-1]
        best = None
        bd = match_cm
        for bseq, bloc in boxes:
            bx, by, _bz = bloc
            if bseq >= last_seq:
                d = ((lx - bx) ** 2 + (ly - by) ** 2) ** 0.5
                if d < bd:
                    bd, best = d, bseq
                continue
            # 盒子早于最终采样：检查其后是否全程贴盒（冻结）
            moved = False
            for samp in tr:
                if samp[0] <= bseq:
                    continue
                dd = ((samp[2] - bx) ** 2 + (samp[3] - by) ** 2) ** 0.5
                if dd > match_cm:
                    moved = True
                    break
            if moved:
                continue
            d = ((lx - bx) ** 2 + (ly - by) ** 2) ** 0.5
            if d < bd:
                bd, best = d, bseq
        if best is not None:
            dead[s] = (best, bd, [bx, by, _bz])
    return dead


def correlate_death_boxes_owned(boxes_by_owner, slots, slot_of_actor,
                                match_cm=DEATH_MATCH_CM, _cache=None):
    """actor 感知死亡盒关联：slot 只与「属于该 slot 角色」的死亡盒匹配。

    boxes_by_owner: {owner_actor: [(seq, [x,y,z]), ...]}（owner = 死亡者 Character，
        协议级 handle14 Owner，经 CarryBody 穿透回原角色）。
    slot_of_actor: {actor: slot}（claim 的 actor->slot 映射）。
    返回 {slot: (box_seq, dist_cm, [x,y,z])} —— 该 slot 角色死在该处。

    与旧 correlate_death_boxes 的差异：盒子必须 owner 匹配该 slot 的角色才判死。
    旧实现纯几何匹配任意盒子，会把「最终位置附近有他人/AI 死亡盒」的活人误判
    死亡（6870 局误判敌人死亡）。owner 匹配后，敌人/AI 的盒子不再误杀活人。
    """
    if _cache is None:
        return _correlate_death_boxes_owned_uncached(
            boxes_by_owner, slots, slot_of_actor, match_cm)
    key = []
    for s, tr in slots.items():
        key.append((s, len(tr), tr[-1][0] if tr else -1))
    key.sort()
    nbox = sum(len(b) for b in boxes_by_owner.values())
    maxbseq = max((b[0] for bl in boxes_by_owner.values() for b in bl), default=-1)
    key.append((-1, nbox, maxbseq, int(match_cm)))
    key = tuple(key)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    res = _correlate_death_boxes_owned_uncached(
        boxes_by_owner, slots, slot_of_actor, match_cm)
    _cache[key] = res
    if len(_cache) > 256:
        for k in list(_cache)[:128]:
            _cache.pop(k, None)
    return res


def _correlate_death_boxes_owned_uncached(boxes_by_owner, slots, slot_of_actor,
                                          match_cm=DEATH_MATCH_CM):
    dead = {}
    for s, tr in slots.items():
        if not tr:
            continue
        actor = None
        for a, sl in slot_of_actor.items():
            if sl == s:
                actor = a
                break
        if actor is None:
            continue
        boxes = boxes_by_owner.get(actor)
        if not boxes:
            continue
        last_seq, _ts, lx, ly, _lz, _yaw = tr[-1]
        best = None
        bd = match_cm
        for bseq, bloc in boxes:
            if bseq < last_seq:
                continue  # 死亡盒早于最终采样 => 角色之后还在动，未死（或已复活）
            bx, by, _bz = bloc
            d = ((lx - bx) ** 2 + (ly - by) ** 2) ** 0.5
            if d < bd:
                bd, best = d, bseq
        if best is not None:
            dead[s] = (best, bd, [bx, by, _bz])
    return dead


def extract_gone_actors(b, char_archs):
    """从角色通道的 close bunch 提取「已消失」的角色。

    返回 {actor_guid: (close_seq, close_reason)}。

    依据（UE4 EChannelCloseReason）：
        close_reason ∈ {0=Destroyed, 2=LevelUnloaded, 3=Relevancy, 4=TearOff}
        -> 角色消失（死亡/离开更新距离/销毁），点消失。
        close_reason == 1 (Dormancy) -> 子对象休眠，角色保留（蹲点），忽略。

    语义：按角色事件时间线判定「最后状态」——spawn(open) 清除消失、close 标记
    消失、复活（再次 spawn）清除。同一 actor 可能在多通道出现（channel 复用），
    故按事件 seq 归并，取最后一个事件决定在场/消失。
    """
    import sna
    ch_actor = {}          # 通道 -> 当前角色本体 actor（close 归属）
    actor_events = {}      # actor -> list of (seq, 'open'|'close', reason)
    for ch in sorted(b):
        for x in b[ch]:
            if x.open:
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
                try:
                    info, _ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=True, frags=x.frags)
                except sna.Bad:
                    continue
                if info.get("arch") in char_archs and info.get("actor"):
                    a = info.get("actor")
                    ch_actor[x.ch] = a
                    actor_events.setdefault(a, []).append((x.seq, "open", None))
            elif x.close:
                reason = x.close_reason
                if reason is None or reason not in GONE_CLOSE_REASONS:
                    continue  # Dormancy 等 = 不构成消失
                # 关闭即清空通道归属：通道关闭后同通道的后续 close 是子对象（组件）
                # 的 Dormancy/Destroyed，不是角色本体消失，不能再归到已关闭角色
                # 头上（否则「离开相关性 Relevancy」被误判成「销毁 Destroyed」）。
                a = ch_actor.pop(x.ch, None)
                if a is None:
                    continue
                actor_events.setdefault(a, []).append((x.seq, "close", reason))
    # 判定：按 seq 排序，最后一个事件是 close -> 消失
    gone = {}
    for a, evs in actor_events.items():
        evs.sort(key=lambda e: e[0])
        if evs and evs[-1][1] == "close":
            gone[a] = (evs[-1][0], evs[-1][2])
    return gone


def extract_fsm_state(b, guid_paths, catalogs):
    """从角色通道的子对象块解码 FSM / 战斗状态。

    返回 {actor_guid: {"fsm_states": [...], "fight": {...}}}。
    子对象块按 cls guid 匹配目录；actor 归属用同通道最近的角色 spawn。
    """
    from blocks import tile
    import repdec6

    # cls guid -> catalog name
    guid2cat = {}
    for g, p in guid_paths.items():
        t = p.rsplit(".", 1)[-1]
        if t.startswith("Default__"):
            t = t[9:]
        if t in TAIL2CAT:
            guid2cat[g] = TAIL2CAT[t]

    # 通道 -> 角色 actor guid（该通道最近一次角色 spawn）
    import sna
    ch_actor = {}
    for ch in sorted(b):
        for x in b[ch]:
            if not x.open:
                continue
            bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
            try:
                info, _ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=True, frags=x.frags)
            except sna.Bad:
                continue
            a = info.get("actor")
            if a:
                ch_actor[ch] = a

    state = {}
    for ch in sorted(b):
        actor = ch_actor.get(ch)
        for x in b[ch]:
            blk = tile(x.payload, x.pbits, 0)
            if not blk:
                continue
            for k in blk:
                if k.get("is_actor") or k.get("deleted"):
                    continue
                cg = k.get("cls")
                catname = guid2cat.get(cg)
                if not catname:
                    continue
                cat = catalogs.get(catname)
                if not cat:
                    continue
                ev, _pos, status = repdec6.decode_block(x.payload, k, cat, {})
                if status not in ("exact", "short"):
                    continue
                rec = state.setdefault(actor, {"fsm_states": [], "fight": {}})
                par = {p["index"]: p["name"] for p in cat.get("parents", [])}
                for e in ev:
                    nm = par.get(e["parent_index"], "?")
                    if catname == "DFMCharacterFSM_Main_C" and nm == "R_ActiveStates":
                        rec["fsm_states"].append(e["value"])
                    elif catname == "GPFightStateComponent":
                        rec["fight"][nm] = e["value"]
    return state
