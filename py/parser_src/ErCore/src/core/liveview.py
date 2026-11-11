# -*- coding: utf-8 -*-
"""liveview.py - 实时增量视图（对接 light_relay 抓包）。

与 BattleView 同构的确定性解析，但：
  - 用 IncrementalFlowLoader 增量重组（保持 reassembler 状态，只处理新增帧）；
  - 只对新 LB 做 spawn 检测 / MRA 槽位解析（O(新增块)，非全量重扫）；
  - 类 GUID 从导出路径增量累积解析（不硬编码 105/131）；
  - join/self/ents 每次 poll 重算（O(links×slots×log)，极廉价）。
"""
from __future__ import annotations
import bisect, os, threading, time
from collections import OrderedDict
from itertools import islice
from operator import itemgetter
from pathlib import Path

import numpy as np

from flowlive import IncrementalFlowLoader
from blocks import tile
from codec.bitstream import BitReader, BitError, slice_bits, read_bits_at
from codec.engine_bunch import read_dfm_packed_int
import sna
import er_live as nl
import repmove4 as rm4
import state_track as st
import ai_name_table as ant
import field13 as f13
import repdec6
from property_health import HealthBatchTracker, operator_health
from death_state import DeathStateTracker
from death_box_state import DeathBoxTracker, box_entity
from container_state import ContainerTracker
from box_contents import BoxContentsTracker
from playerstate_protocol import select_profile, decode_playerstate
from weapon_state import WeaponTracker, weapon_fields
from team_state import TeamTracker
from squad_state import resolve_squad, squad_id
from pickup_state import PickupTracker
from replication_header import exports_prefix

_SAMPLE_SEQ = itemgetter(0)

# 坐标合法性判据 —— **引擎级 sanity，不是地图边界**。
#
# 历史：本判据两次以「地图边界」形式硬编码，两次因上新图而整图失效：
#   ① 零号大坝窄窗 (x∈[250k,450k], y∈[-820k,-700k]) → 航天基地 (x≈669k,
#      y≈-446k) 全灭；
#   ② 改成「DFM 全地图边界」(X[50k,700k] Y[-1.1M,-200k] Z[-200k,100k]) →
#      2026-08-29 新图 (X∈[169k,243k], **Y∈[-226k,-152k]**) 再次全灭：
#      flow 73933 的 20396 条 MRA 样本 100% 被丢（slots=0），flow 73976 丢
#      96%（81677→3295），表现为「雷达有人头数但谁都不动/不更新」。
# 证据：金标准局 495 实测 30926/30926 条样本全部落在旧窗内，即该窗**从未拦下
#      任何东西**——过滤收益为零，代价却是每上一张新图就全盘失效。
# 结论：地图边界不可枚举（新图随版本更新），改用引擎自身的硬约束：UE4 世界
#      坐标上限 WORLD_MAX = 2^21 cm ≈ 21 km，超出即引擎级非法。真正的垃圾解码
#      由上游拦截（tile 块头校验 / handle==21 校验 / repmove4.read_frame4），
#      不靠这里的坐标窗。
WORLD_MAX = 2097152.0          # UE4 WORLD_MAX = 2^21 cm

def WORLD(p):
    """坐标是否可用：非空、有限、非占位原点、且在 UE4 世界坐标上限内。"""
    if not p or len(p) < 3:
        return False
    x, y, z = p[0], p[1], p[2]
    if x == 0.0 and y == 0.0 and z == 0.0:
        return False           # (0,0,0) = 未设置占位（实测 SpawnLocation 用它）
    for c in (x, y, z):
        if c != c or c == float("inf") or c == float("-inf"):
            return False       # NaN / Inf
        if c < -WORLD_MAX or c > WORLD_MAX:
            return False
    return True
MEMBER_MIN_POINTS = 16
_DEBUG = os.environ.get("ER_DEBUG") == "1"   # 调试日志总开关（默认关，正式包静默）

# 槽停更隐藏门限（秒）：出更新距离（Relevancy gone(3)）的真人槽按现行规则保留
# 身份/轨迹缓存，但槽停更超过此时长 = 位置是过期情报，从雷达隐藏；恢复采样
# （静默回落）自动重新上屏。用户定版（0827 实测 6335 局：听风/每1把出红 Relevancy
# 后停更 7min+ 仍钉屏 = 「四个人一直不更新」现象）。
# 用真实时间戳（_seq_ts）而非 seq 差——seq rate 逐局不同（6335 实测 66 seq/s）。
# 死亡（dead_map）豁免：尸体点/死亡盒是权威落点情报，必须保留。
STALE_HIDE_SEC = 300.0

# 上行自机位姿的"陈旧"门限（seq 差）。上行位姿只在客户端活着时持续发；
# 倒地/阵亡/丢帧后 loader.self_pose 会停在最后一帧，而 MRA 槽位仍在更新
# （实测 185435 局：阵亡后 pose 冻在 seq 19766，槽已到 20213）。此时拿陈旧
# 位姿去比最近的槽没有意义——它会把自机认成站在旧位置附近的队友。
# 健康局的实测滞后为 0~10 seq（poseseq 与最新槽采样几乎同拍），256 seq
# （≈4.5s）已极其宽松，只会拦掉真正停发的位姿。
POSE_STALE_SEQ = 256

# 上行位姿 -> 自机槽的"无歧义"裕度（cm）。自机与队友贴在一起时（实测
# pose 距队友槽 113cm、距真自机槽 146cm），纯最近邻会认错人，整帧把自机
# 名字/位置换成队友。要求最近槽比次近槽至少近这么多（或近一倍以上）才认。
POSE_MARGIN_CM = 150.0

# 血条部位（wire: GPAttributeSetHealth.<Part>Health，前端分段长条按此顺序）。
# HP_PART_MAX 默认上限为跨局实测众数（另：wire 上偶发 Max<Part>Health，
# 出现时覆盖默认；流内仅增量同步，未出现过的部位前端按满血渲染）。
HP_PARTS = (
    ("head", "HeadHealth"),
    ("thorax", "ThoraxHealth"),
    ("abdomen", "AbdomenHealth"),
    ("larm", "LeftArmHealth"),
    ("rarm", "RightArmHealth"),
    ("lleg", "LeftLegHealth"),
    ("rleg", "RightLegHealth"),
)
HP_PART_MAX = {"head": 40.0, "thorax": 50.0, "abdomen": 30.0,
               "larm": 30.0, "rarm": 30.0, "lleg": 40.0, "rleg": 30.0}
# ↑ 495 局 wire 实测 Max<Part>Health 与此表逐项对齐（左腿 40/右腿 30 的不对称
#   也是 wire 原值）。这些部位值仅用于部位信息，不作为总血量分母。

# 血量属性脏值门（0901 用户报「HP 显示不准」的根因）：GPAttributeSetHealth 的
# float 偶发位型脏值——495 局实测 AbdomenHealth=2.9e-39（非规格化浮点，前端
# 算作 0% → 健康玩家显示断肢）、MaxThoraxHealth=2.8e-17（前端 cur/mx 溢出后
# 截断成 100% → 重伤玩家显示满血）。两个方向都会让血条撒谎，且脏值一旦入库
# 会覆盖上一拍的好值。故在**入库处**丢弃不可信值（保留上一拍好值），只作用于
# 名字以 Health 结尾的属性（IsAlive/ImpendingDeathNum 等计数属性不动）。
HP_VAL_MIN = 0.01
HP_VAL_MAX = 10000.0


def health_val_ok(name, v):
    """GPAttributeSetHealth 数值是否可信（脏值门，见上方注释）。"""
    if not name.endswith("Health"):
        return True
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    v = float(v)
    if v != v or v in (float("inf"), float("-inf")):    # NaN / ±inf
        return False
    return v == 0.0 or (HP_VAL_MIN <= v <= HP_VAL_MAX)


def current_health_event(event, catalog):
    """Select CurrentValue, not BaseValue, from a two-float attribute."""
    parent = next((p for p in catalog["parents"]
                   if p["index"] == event["parent_index"]), None)
    if parent is None:
        return False
    members = [c for c in catalog["cmds"]
               if parent["cmd_start"] <= c["index"] < parent["cmd_end"]]
    return (len(members) == 2 and all(c["type"] == 4 for c in members)
            and event.get("cmd_index") == max(c["index"] for c in members))


# PVE AI 类名 -> 中文兵种名（0901 用户报「PVE AI 没有显示」）。AI 子类无
# PlayerState，ai_key 也常缺席（495 局 55 个 AI 里 39 个 ai_key=None），旧实现
# 让它们 name=None，快照层「无名标记不上屏」把它们整体吞掉。类名来自导出路径
# （确定性信息），按兵种给名比整体丢弃更有情报价值。
AI_CLS_NAMES = (
    ("RPG", "AI·火箭兵"),
    ("ShielderHeavy", "AI·重盾兵"),
    ("Shielder", "AI·盾兵"),
    ("Firebat", "AI·火焰兵"),
    ("Sniper", "AI·狙击手"),
    ("Boss", "AI·首领"),
    ("Elite", "AI·精英"),
    ("Medic", "AI·医疗兵"),
    ("MG", "AI·机枪手"),
)


def ai_cls_name(cls):
    """AI 角色类名 -> 展示名；未命中兵种表的 AI 类统一「AI·士兵」。"""
    if not cls:
        return None
    s = cls.rsplit(".", 1)[-1]
    if s.startswith("Default__"):
        s = s[9:]
    low = s.lower()
    for key, nm in AI_CLS_NAMES:
        if key.lower() in low:
            return nm
    return "AI·士兵" if "ai" in low else None


def confirmed_player_bot(player, matched_hero=False):
    """Confirm a bot only when no known operator identity was recovered."""
    if matched_hero:
        return False
    if not isinstance(player, dict) or player.get("bot") is not True:
        return False
    pid, uin, camp = player.get("player_id"), player.get("uin"), player.get("camp")
    signals = 0
    try:
        signals += int(pid) >= 8000
    except (TypeError, ValueError):
        pass
    if pid is not None and uin is not None and str(pid).strip() == str(uin).strip():
        signals += 1
    try:
        signals += int(camp) == 106
    except (TypeError, ValueError):
        pass
    return signals >= 2


# PVE AI 出生点标记保留时长（秒）：AI 不在 MRA 槽位数组里（495/2490 两局实测
# 槽位只覆盖真人，AI 认领命中 0/13、0/10），唯一坐标来源就是出生点。AI 在 DFM
# 里绕据点活动、不远走，"最近 N 秒出现的 AI 出生点"是可用情报——但必须**明示
# 是出生点而非实时位置**：快照给 spawn_mark/age_sec，前端画空心标记 + 龄。
AI_SPAWN_KEEP_SEC = 240.0

# Long-running live views must not retain one Python tuple per movement sample
# forever.  Keep the recent tail at full resolution, and sparsely retain older
# points so position lookup, trails, death-box correlation, and team matching
# still have a useful history without making memory proportional to match time.
TRACK_HISTORY_MAX = max(
    1024, int(os.environ.get("ER_TRACK_HISTORY_MAX", "4096")))
TRACK_HISTORY_RECENT = min(
    TRACK_HISTORY_MAX - 256,
    max(768, int(os.environ.get("ER_TRACK_HISTORY_RECENT", "3072"))))
SEQ_TS_MAX = max(
    16384, int(os.environ.get("ER_SEQ_TS_MAX", "100000")))

# 致命部位归零→濒死计时(ImpendingDeathHealth) wire 上报的宽限窗（秒）：
# 5130 局实测 4.4s（143.2 归零→147.6 imp=92）。窗内不判死，防「倒地假死」闪标。
VITAL_GRACE_SEC = 8.0

# 濒死倒计时「仍在递减」的判定窗（秒）：5130 局 4708 实测上报间隙可达 15.8s
# （51@362.0 → 37.4@377.8），阈值须盖过间隙；上报终止（死亡/出更新距离）后
# 超窗即熄灭。FSM 濒死(42)/倒地(5) 在部分局整局缺失（5130 R_ActiveStates
# 零上报、fight 枚举错位 88/90），imp 递减是「倒地」状态的唯一兜底信号
# （0828 user 二次定版：只用 ImpendingDeathHealth，部位归零不做依据）。
IMP_ACTIVE_SEC = 20.0

def _name_table_paths():
    """item_names_cn.json 的候选位置：本模块旁 / 仓库根 / 数据根（打包后 ER_ROOT=exe 目录）。

    Nuitka 打包后 __file__ 是伪造路径，前两个候选可能都不存在，必须带 ER_ROOT 兜底。
    """
    here = Path(__file__).resolve()
    cands = [here.parent / "item_names_cn.json"]
    if len(here.parents) > 2:
        cands.append(here.parents[2] / "item_names_cn.json")
    env_root = os.environ.get("ER_ROOT")
    if env_root:
        cands.append(Path(env_root) / "item_names_cn.json")
    return cands


# 干员名表（item_names_cn.json v5 operators: HeroId(880000000xx) -> 姓名）
HERO_NAMES = {}
try:
    import json as _json
    for _p in _name_table_paths():
        if _p.exists():
            _ops = (_json.loads(_p.read_text(encoding="utf-8")).get("operators"))
            _items = _ops.items() if isinstance(_ops, dict) else [
                ((o or {}).get("hero_id"), o) for o in (_ops or [])]
            for _k, _o in _items:
                try:
                    _hid = int((_o or {}).get("hero_id") or _k)
                    _nm = (_o or {}).get("Name") or (_o or {}).get("name")
                    if _hid and _nm:
                        HERO_NAMES[_hid] = str(_nm)
                except Exception:
                    continue
            break
except Exception:
    HERO_NAMES = {}

# 名表覆盖：item_names_cn.json 是游戏资源导出，个别 HeroId 尚未收录（实测
# 88000000051 不在那 41 条里）或显示名与官方不一致时，在这里补。键是 wire 上的
# HeroId，值是显示名；用 update 就地改，所有 import 了 HERO_NAMES 的模块都会看到。
# 放代码里而不是数据文件里 —— 重新导出名表时不会被覆盖掉。
HERO_NAME_OVERRIDE = {
    88000000051: "旅人",
    88000000036: "佐娅",     # 原「蛊」；头像文件是 佐娅.png（蛊.png 与它是同一张图）
}
HERO_NAMES.update(HERO_NAME_OVERRIDE)

# 武器显示名优先用 v15 的 weapon_names（按完整模板 ID 审核的规范名称）。
# items 是物品/配件名称，不能直接替代武器名称；未收录 ID 才走原过滤兜底。
# 地面物品仍独立用 items，避免把作为物资出现的机匣改名成整枪。
# WEAPON_NAMES_RAW 保留未过滤名（含机匣名）：组装枪（GPFastWeaponBPClassPath
# type=1）的当前武器现在通过 WeaponReplicatedExtraDataList 按 identity 精确解析。
_WEAP_NAME_BAD = ("虚空", "枪管", "护木", "枪托", "握把", "弹匣", "消音",
                  "瞄具", "导轨", "枪口", "下挂", "机匣", "节套", "击败",
                  "外观", "沿用", "是一款")
WEAPON_NAMES = {}
WEAPON_NAMES_RAW = {}
try:
    import json as _json
    for _p in _name_table_paths():
        if _p.exists():
            _weapon_doc = _json.loads(_p.read_text(encoding="utf-8"))
            _its = _weapon_doc.get("items")
            if isinstance(_its, dict):
                for _k, _o in _its.items():
                    if len(_k) != 11 or _k[:2] not in ("18", "21") \
                            or not isinstance(_o, dict) or not _o.get("name"):
                        continue
                    _nm = str(_o["name"])
                    WEAPON_NAMES_RAW[int(_k)] = _nm
                    if len(_nm) < 2 \
                            or _nm.startswith(("_", "（废弃）", "（停用", "的")) \
                            or any(_w in _nm for _w in _WEAP_NAME_BAD):
                        continue
                    WEAPON_NAMES[int(_k)] = _nm
            _canonical = _weapon_doc.get("weapon_names")
            if isinstance(_canonical, dict):
                for _k, _nm in _canonical.items():
                    if len(_k) == 11 and _k.isascii() and _k.isdecimal() \
                            and _k[:2] in ("18", "21") \
                            and isinstance(_nm, str) and _nm.strip():
                        WEAPON_NAMES[int(_k)] = _nm.strip()
            break
except Exception:
    WEAPON_NAMES = {}
    WEAPON_NAMES_RAW = {}

# 露天物资名表（item_names_cn.json items 段全量：11 位模板 ID -> 名字）。
# 与 WEAPON_NAMES（仅 18xx/21xx + 配件黑名单过滤）不同：物资段 10xx~42xx
# 全收（地面散落的电池/药品/工具都是这些段；武器本体掉落也走同表）。
LOOT_NAMES = {}
LOOT_META = {}   # id -> {"grade":1-6, "dgjz":单格价值, "price":价格}（orzice 物价表，按 ID/名双路并入）
# 对局销毁判定：live 实时流超过该秒数无新帧 = 游戏已退出/对局销毁，
# 物资停止上报（清单/地图清空）。对局内流量连续，20s 全静默只可能是已退出。
GAME_END_STALE_SEC = 20.0
try:
    import json as _json
    for _p in _name_table_paths():
        if _p.exists():
            _its = _json.loads(_p.read_text(encoding="utf-8")).get("items")
            if isinstance(_its, dict):
                for _k, _o in _its.items():
                    if isinstance(_o, dict) and _o.get("name"):
                        LOOT_NAMES[str(_k)] = str(_o["name"])
                        if _o.get("grade"):
                            LOOT_META[str(_k)] = {
                                "grade": int(_o["grade"]),
                                "dgjz": _o.get("dgjz"),
                                "price": _o.get("price"),
                            }
            break
except Exception:
    LOOT_NAMES = {}
    LOOT_META = {}

# 装备名表（item_names_cn.json v15 gear_names 段：11 位装备模板 ID -> 显示名）。
# 段位：1101 头盔 / 1105 防弹衣 / 1107 胸挂 / 1108 背包 / 1109 安全箱 / 1112
# 门禁卡包。当前只消费 1107/1108（干员台背包/胸挂名，user 定版 0831），
# 其余段进表备用。
GEAR_NAMES = {}
try:
    import json as _json
    for _p in _name_table_paths():
        if _p.exists():
            _gns = _json.loads(_p.read_text(encoding="utf-8")).get("gear_names")
            if isinstance(_gns, dict):
                for _k, _nm in _gns.items():
                    GEAR_NAMES[str(_k)] = str(_nm)
            break
except Exception:
    GEAR_NAMES = {}


# 停驻只提供候选标记。休眠后仍可能通过新通道发送 destruction-info；
# PickupTracker 用旧 Actor NetGUID 接收该通知，并用背包同 GID 转移确认拾回。
# 不把贴脸、休眠或关卡卸载当作拾取成功。
LOOT_PICK_RADIUS_CM = 120.0
LOOT_PICK_VMAX = 80.0        # cm/s，低于=停驻
LOOT_PICK_INFER = True       # 是否增加停驻候选；不控制权威生命周期

# PlayerState fields which are useful to identity and life-state consumers.
# Keep the wire handle list explicit: a missing field in an UE replication
# delta means "unchanged", not "default".
PS_ID_HANDLES = {
    14, 18, 21, 27, 28, 30, 62, 63, 137, 138,
}
PS_LIFE_HANDLES = {
    31, 32, 33, 34, 35, 36, 37, 126, 127, 129,
}
PS_WANT_HANDLES = PS_ID_HANDLES | PS_LIFE_HANDLES
PS_LIFE_FIELDS = {
    31: "bDead",
    32: "DeadTickNum",
    33: "DeathWaitRescueTime",
    34: "DeathWaitRescueStartTime",
    35: "DeathWaitRescueEndTime",
    36: "bFinishGame",
    37: "bDeathWaitingRescueEnd",
    126: "PlayerGamePlayState",
    127: "CurrentCharacterLiveStatus",
    129: "bIsDeadBox",
}

# CurrentCharacterLiveStatus observed on the live PlayerState wire:
# 0/1 are non-dead states, 2 is the terminal/dead state.  bDead remains the
# strongest signal; this enum is only a secondary source when a delta omits it.
PS_DEAD_LIVE_STATUS = 2
PS_ALIVE_LIVE_STATUSES = {1}


def mark_picked_by_standing(loot, slots):
    """回放全量版：给 loot 打拾取候选时刻，返回标记数。

    loot: {actor: {"loc","seq",...}}；slots: {slot: [(seq, ts, x, y, z, yaw)]}。
    判据：任一 slot 的相邻样本对（后点在物资 spawn 之后）速度 <LOOT_PICK_VMAX
    且后点距物资 ≤LOOT_PICK_RADIUS_CM —— 记后点 seq 为
    pickup_candidate_seq（取各 slot 最早命中）。这是行为候选，不是协议确认；
    只有 close reason 0/2/4 才会写 gone_seq。"""
    if not LOOT_PICK_INFER:
        return 0
    n = 0
    for a, lr in loot.items():
        if lr.get("pickup_candidate_seq") is not None \
                or lr.get("gone_seq") is not None:
            continue
        lx, ly = lr["loc"][0], lr["loc"][1]
        spawn = lr["seq"]
        best = None
        best_slot = None
        for slot, tr in slots.items():
            for i in range(1, len(tr)):
                q = tr[i]
                if q[0] < spawn:
                    continue
                dx = q[2] - lx
                dy = q[3] - ly
                if dx * dx + dy * dy > LOOT_PICK_RADIUS_CM * LOOT_PICK_RADIUS_CM:
                    continue
                p = tr[i - 1]
                dt = q[1] - p[1]
                if dt <= 0:
                    continue
                v = (((q[2] - p[2]) ** 2 + (q[3] - p[3]) ** 2
                      + (q[4] - p[4]) ** 2) ** 0.5) / dt
                if v >= LOOT_PICK_VMAX:
                    continue
                if best is None or q[0] < best:
                    best = q[0]
                    best_slot = slot
                break     # 该轨迹按 seq 递增，首个命中即本轨迹最早
        if best is not None:
            lr["pickup_candidate_seq"] = best
            lr["pickup_candidate_slot"] = best_slot
            lr["pickup_confidence"] = "candidate"
            n += 1
    return n


def extract_loot_full(b, pk_archs):
    """Replay the same authoritative item lifecycle used by LiveView."""
    tracker = PickupTracker(pk_archs, LOOT_NAMES, LOOT_META, keep_history=True)
    for x in sorted((x for xs in b.values() for x in xs), key=lambda x: (x.seq, x.ch)):
        tracker.consume(x)
    return tracker.records


def loot_display_fields(record):
    """Share item presentation fields between live snapshots and cursor playback."""
    count = record.get("stack_count")
    if count is None:
        count = (record.get("inventory_info") or {}).get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        count = None
    gid = record.get("inventory_gid")
    meta = LOOT_META.get(str(record.get("item_id"))) or {}
    return {
        "item_id": record.get("item_id"),
        "grade": record.get("grade"), "price": record.get("price"),
        "price_per_slot": meta.get("dgjz"),
        "stack_count": count,
        "inventory_gid": str(gid) if gid is not None else None,
        "updated_seq": record.get("update_seq", record.get("seq")),
        "item_state": record.get("item_state"),
        "source_type": record.get("source_type"),
        "channel_state": record.get("channel_state"),
        "pickup_status": record.get("pickup_confidence", "visible"),
        "pickup_candidate_seq": record.get("pickup_candidate_seq"),
    }


def _scan_pickup_item_id(bits, lo=40, hi=640):
    """扫 Pickup actor 属性块里的物资 ID FString，返回 11 位 ID 串或 None。

    逆向定案（2026-08-30 round，flow 2426 全 40 样本实证）：
      - 带物资的 Pickup 在 handle52 写一个 FName（字符串形式）：
        [1bit hardcoded=0][i32 len][len-1 位十进制模板 ID + NUL][i32 number=0]
        ID 段位 1402/1405/1502/1503/1504/1506/1508…（item_names_cn items 段）。
      - FString 的绝对 bit 位随物资类型浮动（主族 @126、1503 族 @137、
        1680 族 @135——前置字段数不同），必须任意偏移扫描。
      - 无物资变体（块长 205/357 位两族，handle53 硬编码 FName idx=0）
        全块无数字串，返回 None → 显示层给通用「物资」点。
    命中即返回（实测每块至多一个数字串）。
    """
    n = len(bits)
    end = min(n, hi)
    for off in range(lo, end - 48):
        ln = 0
        for i in range(32):
            ln |= int(bits[off + i]) << i
        if not (8 <= ln <= 20):
            continue
        base = off + 32
        if base + ln * 8 > n:
            continue
        chars = []
        ok = True
        for k in range(ln):
            by = 0
            for j in range(8):
                by |= int(bits[base + k * 8 + j]) << j
            if k < ln - 1:
                if not (48 <= by <= 57):
                    ok = False
                    break
                chars.append(chr(by))
            elif by != 0:
                ok = False
                break
        if ok and len(chars) >= 7:
            return "".join(chars)
    return None


def fight_status(fight, fsm_states=()):
    """GPFightStateComponent + FSM 状态 -> 前端展示用 (label, key)。

    死亡/倒地优先；CurrentFightStateType 是主状态
    （GPFightStateComponent.CurrentFightStateType 枚举：0=待机 1=战斗 2=警戒
    3=掩体 4=移动 5=倒地 6=救援）；FSM R_ActiveStates 含死亡(43)/濒死(42)。
    42 与 5 同为倒地流血态，统一显示「倒地」，key 统一 down。
    """
    if not fight:
        return None, None
    try:
        ftype = int(fight.get("CurrentFightStateType"))
    except (TypeError, ValueError):
        ftype = None
    # FSM 死亡(43)/濒死(42) 最高优先：角色已死/倒地，非移动类状态可比
    states = set(fsm_states or ())
    if 43 in states:
        return "死亡", "dead"
    if 42 in states:
        return "倒地", "down"
    if ftype == 5:
        return "倒地", "down"
    if ftype == 6:
        return "救援", "revive"
    return None, None

class LiveDetector:
    """战斗流探测器（引擎级判据，与游戏客户端逻辑一致）。

    判定标准钉死为 UE4 控制通道握手（见 handshake.py）：
        C->S Hello -> S->C Challenge -> C->S Login -> S->C Welcome("/Game/..") -> C->S Join
    战斗流 ⇔ 收到 Welcome（客户端只有收到 Welcome 才开始复制 Actor）。
    PlayerState 通道计数仅作排序辅助（握手通过后才有意义）。

    增量重组 + 导出路径累积；锁定后把 loader/guid_paths 交给 LiveView 接管
    （不重复重组）。
    """

    def __init__(self, flow_dir):
        self.flow_dir = Path(flow_dir)
        # 探测阶段不开 c2s_self：探测器只判握手门，而上行自机位姿只有锁定后的视图
        # 需要（视图接管时由 er_radar 调 loader.enable_c2s_self()）。否则每个候选流
        # 都要多解一遍上行，tick 被拖慢、解析批次也更碎。
        self.loader = IncrementalFlowLoader(self.flow_dir, auto_s2c=True)
        self.guid_paths = {}
        self.ps_archs = set()
        self._path_idx = {}     # channel -> 已扫描路径的 LB 数
        self._ps_count = 0
        self._ps_seen = set()   # 已计数的 channel
        self.done = False       # 探测完成（握手收敛或前 N 帧覆盖全部 PS 通道）

    @property
    def handshake_ok(self):
        return self.loader.hs.ok

    def hs_info(self):
        return self.loader.hs.as_dict()

    def poll(self, max_frames=None, early_frames=None):
        """early_frames: PS 计数的扫描窗口上限（握手通过后才需要）。"""
        if self.done:
            return self._ps_count
        self.loader.poll(max_frames=max_frames)
        # ---- 引擎门（与游戏一致）：握手收敛但无 Welcome ⇒ 非战斗流，立即判死
        if self.loader.ctrl_done and not self.loader.hs.ok:
            self.done = True
            return 0
        b = self.loader.b
        # 增量累积导出路径
        for ci, lst in b.items():
            idx = self._path_idx.get(ci, 0)
            for x in lst[idx:]:
                if not x.open or not x.hx:
                    continue
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
                try:
                    entries, _ss = exports_prefix(x)
                except sna.Bad:
                    continue
                for e in entries:
                    p = e.get("path")
                    if p:
                        g = e["guid"]
                        if g not in self.guid_paths or len(p) > len(self.guid_paths[g]):
                            self.guid_paths[g] = p
            self._path_idx[ci] = len(lst)
        # 解析 ps_archs
        self.ps_archs = set()
        for g, p in self.guid_paths.items():
            tail = p.rsplit(".", 1)[-1]
            if tail.startswith("Default__"):
                tail = tail[9:]
            if tail in ("BP_DFMPlayerState_C", "BP_SOLPlayerState_C"):
                self.ps_archs.add(g)
        ps_resolved = bool(self.ps_archs)
        if not self.ps_archs:
            self.ps_archs = {131}
        # 增量计数 PlayerState 通道（每通道只看首个 open bunch）
        for ci, lst in b.items():
            if ci in self._ps_seen:
                continue
            for x in lst:
                if not x.open:
                    continue
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
                try:
                    info, _ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=False, frags=x.frags)
                except sna.Bad:
                    continue
                if info.get("arch") in self.ps_archs:
                    self._ps_count += 1
                self._ps_seen.add(ci)
                break
        # done 判定（移到解析/计数之后）：握手通过（对战会话）时 PS 可能晚
        # spawn（地图加载慢，实测航天基地 PS 在 86 帧才出现），early_frames
        # 到点不能立即 done——否则 n_ps=0 被缓存，对战流永久误判为非战斗流。
        # 规则：
        #   - 流耗尽 → done（无论 n_ps 多少）
        #   - 握手已收敛（ctrl_done）但无 Welcome（真非战斗流）+ early_frames
        #     到点 → done（早停）
        #   - 握手未收敛（ctrl_done=False，relay 异步写盘/分片重组中）→ 不 done。
        #     2426→2832 换局实证：early_frames 到点时 ch0 握手 bunch 尚未拼完，
        #     hs.ok=False 被误当「真非战斗流」判死 → _probe 缓存 n_ps=0 → 该局
        #     永不锁定（同局队友视角 2838 正常锁定，gwh 全程 detecting）。
        #   - 握手通过 + PS 已解析（ps_resolved）+ early_frames 到点 → done
        #   - 握手通过 + PS 未解析 + 流仍在增长 → 不 done（下次 poll 继续）
        if self.loader.exhausted():
            self.done = True
        elif early_frames is not None and self.loader.n_frames >= early_frames:
            if not self.loader.hs.ok:
                if self.loader.ctrl_done:
                    self.done = True   # 握手走完且无 Welcome = 确定非战斗流
            elif ps_resolved:
                self.done = True
        return self._ps_count


# ---- 0830 三轮：装备数组结构判定提取器 ----
# spawn 重组超 bunch 的开局全量装备数组，tile 切块多落入错归属块/块间隙，
# repdec6 handle 链走不通。元素结构（44741 flow + exact 解码双实证）：
#   元素 k 字段句柄 = 9k+{1:itemId u64, 2:uid u64, 5:dur_cur f32, 6:dur_max f32}
#   （子 cmds 3..11 共 9 条扁平化；稀疏槽→句柄跳号；h=0 链终止）
# 提取 = itemId 锚定 + 严格链验证（句柄值精确匹配角色序列、uid≥2^50、
# dur∈[0,500]），非盲扫：布局来自 repdec6 exact 解码反推。
_EQ_FAMILIES = ("1101", "1105", "1107", "1108", "1109")


def _scan_equip_arrays(payload, ps, pe, cache=None):
    # Normalize only this component's bits. Previously each component unpacked
    # its entire parent packet again. Cache ownership is per LiveView/flow;
    # cloned accounts cannot share cached results or distort capacity tests.
    pe = min(pe, len(payload) * 8)
    if ps < 0 or pe - ps < 250:
        return []
    length = pe - ps
    segment = slice_bits(payload, ps, length)
    key = (length, segment)
    if cache is not None and key in cache:
        cache.move_to_end(key)
        return [dict(item) for item in cache[key]]
    result = _scan_equip_arrays_uncached(segment, 0, length)
    if cache is not None and len(segment) <= 4096:
        cache[key] = tuple(tuple(item.items()) for item in result)
        if len(cache) > 256:
            cache.popitem(last=False)
    return result


def _scan_equip_arrays_uncached(payload, ps, pe):
    """在 payload[ps:pe) 内按上述结构提取装备数组元素。
    返回 [{"item_id","uid","dur","dur_max","k"}]；无通过验证的链返回 []。"""
    import struct as _s
    if pe - ps < 250:
        return []
    u = np.unpackbits(np.frombuffer(payload, dtype=np.uint8), bitorder="little")
    seg = u[ps:pe]
    L = len(seg)

    def rd(pos, n):
        if pos >= 0 and pos+n <= L:
            return read_bits_at(payload, ps+pos, n)
        v = 0
        for j in range(n):
            v |= int(seg[pos + j]) << j
        return v

    def packed(pos):
        v = 0
        sh = 0
        for g in range(3):
            if pos + (g + 1) * 8 > L:
                return None, None
            enc = rd(pos + g * 8, 8)
            v |= (enc >> 1) << sh
            sh += 7
            if not (enc & 1):
                return v, (g + 1) * 8
        return None, None

    def f32(pos):
        return _s.unpack("<f", rd(pos, 32).to_bytes(4, "little"))[0]

    # 锚点：u64-LSBfirst ∈ 装备族（64 相位向量化）
    if L < 128:
        return []
    # Build byte-aligned windows once, then interleave their eight bit phases.
    # This preserves every candidate offset without 64 full-size temporaries.
    packed_bits = np.packbits(seg, bitorder="little")
    padded = np.pad(packed_bits, (0, 8))
    words = np.zeros(len(packed_bits), dtype=np.uint64)
    for i in range(8):
        words |= padded[i:i + len(words)].astype(np.uint64) << np.uint64(i * 8)
    acc = np.empty(L - 63, dtype=np.uint64)
    for phase in range(8):
        count = len(acc[phase::8])
        values = words[:count] >> np.uint64(phase)
        if phase:
            values |= padded[8:8 + count].astype(np.uint64) << np.uint64(64 - phase)
        acc[phase::8] = values
    anchors = []
    for idx in np.nonzero((acc >= np.uint64(10 ** 10))
                          & (acc < np.uint64(10 ** 12)))[0]:
        c = int(idx)
        if str(int(acc[c]))[:4] in _EQ_FAMILIES:
            anchors.append(c)

    out = []
    covered = []
    for c in anchors:
        if any(abs(c - t) < 96 for t in covered):
            continue
        # 从锚点反向走链：fields[k] = {1: itemId, 2: uid, 5: dur, 6: dur_max}
        fields = {}
        order = []
        # 起点回退：锚点前找 itemId 句柄（h≡1 mod 9，8/16 位组）
        start = None
        for back in (8, 16):
            h0, g0 = packed(c - back)
            if h0 is not None and c - back + g0 == c \
                    and h0 >= 1 and (h0 - 1) % 9 == 0 and (h0 - 1) // 9 <= 40 \
                    and int(acc[c]) == rd(c, 64):
                start = c - back
                break
        if start is None:
            continue
        pos = start
        for _ in range(40):
            hh, gg = packed(pos)
            if hh is None:
                break
            if hh == 0:
                pos += gg
                break
            role = hh % 9          # 1/2/5/6
            k = hh // 9
            if role == 1:
                v = int(acc[pos + gg]) if gg == 1 else rd(pos + gg, 64)
                if not (10 ** 10 <= v < 10 ** 12) \
                        or str(v)[:4] not in _EQ_FAMILIES:
                    break
                p2 = pos + gg + 64
            elif role == 2:
                v = rd(pos + gg, 64)
                if v < 2 ** 50:
                    break
                p2 = pos + gg + 64
            elif role in (5, 6):
                v = f32(pos + gg)
                if not (0.0 <= v <= 500.0):
                    break
                p2 = pos + gg + 32
            else:
                break
            if k not in fields:
                fields[k] = {}
                order.append(k)
            if role in fields[k]:
                break              # 同字段重复 = 链非法
            fields[k][role] = v
            pos = p2
        # 组元素：itemId+uid 必备，dur 成对
        got = []
        for k in order:
            f = fields[k]
            if 1 in f and 2 in f:
                got.append({"item_id": f[1], "uid": f[2],
                            "dur": f.get(5), "dur_max": f.get(6), "k": k})
        if len(got) >= 2:
            out.extend(got)
            covered.append(c)
    return out


def u64v(seg, pos):
    v = 0
    for j in range(64):
        v |= int(seg[pos + j]) << j
    return v


class _GuidPaths(dict):
    """Track added/replaced exports without rescanning the whole path table."""

    def __init__(self, source=()):
        super().__init__(source)
        self._changed = dict.fromkeys(self)

    def __setitem__(self, key, value):
        if key not in self or self[key] != value:
            self._changed[key] = None
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def __ior__(self, other):
        self.update(other)
        return self

    def take_changed(self):
        changed = self._changed
        self._changed = {}
        return changed


class LiveView:
    def __init__(self, run_dir, flow_dir, loader=None, guid_paths=None, *, compact=True):
        self.run_dir = Path(run_dir); self.flow_dir = Path(flow_dir)
        self.lock = threading.RLock()  # 可重入：snapshot/poll/_derive 会嵌套加锁
        self._compact_enabled = compact
        self.loader = loader if loader is not None else IncrementalFlowLoader(
            self.flow_dir, auto_s2c=True, c2s_self=True
        )
        # 自建 loader 时也要开上行自机位姿（不抽稀：见 flowlive.enable_c2s_self 的说明）
        try:
            self.loader.enable_c2s_self(1)
        except AttributeError:
            pass
        self.guid_paths = _GuidPaths(guid_paths or {})
        CLS = nl.load_catalogs()
        self.ps_cat = CLS["BP_DFMPlayerState_C"]
        self.ps_cmds = sorted(self.ps_cat["cmds"], key=lambda c: c["index"])
        self.ps_parents = self.ps_cat["parents"]
        self.char_cat = CLS["BP_DFMCharacter_C"]
        self.char_cmds = sorted(self.char_cat["cmds"], key=lambda c: c["index"])
        self.char_parents = self.char_cat["parents"]
        # 0904: 角色时装组件（currentCharacterHeroID = hero 权威源，PS h137 兜底）
        _fc = CLS.get("DFMCharacterFashionComponent")
        self._fashion_cmds = sorted(_fc["cmds"], key=lambda c: c["index"]) if _fc else []
        self._fashion_names = ({i: p.get("name") for i, p in enumerate(_fc["parents"])}
                               if _fc else {})
        self._fashion_h = (nl._fashion_hero_handle(self._fashion_cmds, _fc["parents"])
                           if _fc else None)
        self._fashion_guids = set()
        self.ps_archs = {131}
        self.char_archs = {105}
        self.box_archs = set()     # BP_Inventory_DeadBody_C 类 GUID（路径解析）
        self.carry_archs = set()   # BP_Inventory_CarryBody_C 类 GUID（背尸盒，间接指回 Character）
        self.pk_archs = set()      # BP_InventoryPickup_C(+FromSingleItemContainer) 露天物资
        self.pickup_tracker = PickupTracker(self.pk_archs, LOOT_NAMES, LOOT_META)
        self.box_tracker = DeathBoxTracker(self.box_archs, self.carry_archs)
        self.box_contents = BoxContentsTracker()
        self.container_tracker = ContainerTracker(self.guid_paths)
        self.loot = self.pickup_tracker.active
        self.loot_history = self.pickup_tracker.removed
        self.loot_ch = self.pickup_tracker.channels
        self._pk_chk = {}          # slot -> 已做过拾取推断的轨迹样本数（增量游标）
        self.players = {}          # actor_guid -> {name, player_id, bot, owner_guid, channel, uin}
        self.player_state = {}     # ps_actor -> {"values": {field: value}, "seq": {field: seq}}
        self.ps_ch_actor = {}      # PlayerState channel -> current PlayerState actor
        self.links = {}            # char_actor -> {owner, ps_guid, spawn_seq, loc, num_exports}
        self.slots = {}            # slot -> [(seq, ts, x, y, z, yaw)]
        self.boxes = {}            # box_actor -> {"loc":[x,y,z], "seq":seq, "owner_actor":guid}（死亡盒）
        self.carry_owner = {}      # carry_actor -> char_actor（背尸盒 → 原死亡者角色）
        self.mra_ch = None
        self.mra_locked = False
        self._self_slot_pose = None    # 上行自机坐标钉住的自机槽（权威，见 _slot_near_pose）
        self._mra_idx = 0
        self._spawn_idx = {}       # channel -> 已扫描 spawn 的 LB 数
        self._seq_ts = {}          # seq -> ts
        self._seq_ts_idx = 0       # 已消费 loader.seq_ts 的下标
        self._last_data_ts = 0.0   # 最新帧到达时刻（对局销毁判定：超时无新帧=已退出）
        self.self_slot = None
        self.ents = []
        self.member_slots = set()
        self.claim = {}
        self.health_batches = HealthBatchTracker()
        self.life_tracker = DeathStateTracker()
        self.ps_profiles = {}
        self.ps_decode_stats = {}
        self.weapon_tracker = WeaponTracker()
        self.team_tracker = TeamTracker()
        self.mra_frame_stats = {"accepted": 0, "incomplete": 0, "error": 0}
        self.self_name = None
        self.self_src = None        # 自机名是谁定的：pose/ch/spawn/pid（排查用）
        self.n_lb = 0
        self.started = time.time()
        # Monotonic revision for read-side snapshot caching.  Keeping this
        # scalar avoids walking every retained slot sample on each API poll.
        self.state_version = 0
        # 角色状态（FSM/战斗状态）：增量解码，随 poll 新增子对象块实时更新。
        # actor_guid -> {"fsm_states": [...], "fight": {...}}
        # 替换旧的「一次性 _fsm_warm 全量解码」（状态只解一次、此后冻结）——
        # 倒地/死亡/战斗状态必须在局内实时刷新，故逐 poll 只解新增块。
        self.fsm_state = {}
        self._subcats = CLS                # 子对象类目录（GPFightStateComponent/FSM_Main 等）
        self._cls2cat = {}                 # cls/sub guid -> 目录名（随 guid_paths 增长刷新）
        self._cls2cat_miss = set()         # (guid,path) 负缓存：路径不变则 tail2cat 恒未命中
        self._archs_idx = 0                # _resolve_archs 已分类的 guid_paths 前缀长度
        self._last_fsm_seq = -1            # 已消费子对象块的最大 seq（防重复解码）
        self._fsm_ch_actor = {}            # channel -> 最近角色 actor（FSM 归属；close 不清，死亡后状态块仍归原主）
        # 实时当前武器（0830 装备链路定案，静态反汇编+14 流对拍）：

        # 总血量/护甲由 PropertyReplication 批量流合并；普通属性流补充
        # 上限、部位和濒死倒计时。Health 或单个部位为 0 本身不证明死亡。
        # actor -> {"parts": {部位名: 最新值}, "impending": 最新濒死血量或 None,
        #           "seq": 最后更新 seq}
        self.health = {}
        self.vital_dead = set()    # 致命部位归零的 actor（_derive 从 health 重算）
        self.vital_revived = set() # 归零后回升过 = 倒地被救（_derive 维护）
        self._vital_zero_seen = {} # actor -> set(曾归零的致命部位名)（跨 poll 记忆）
        # 护甲装备（SOLCharacterEquipComponent，增量解码）：
        # actor -> {"armor": {元素: {"item_id","dur","seq"}}, "items": {...}}
        # armor 元素 itemId(1101000xxxxx 头甲 / 11050003xxx 防弹衣，11 位物品
        # ID 第 4~5 位=等级段) + dur 耐久（掉甲过程 66→52→38→9→0 实测）。
        self.equip = {}
        self._equip_scan_cache = OrderedDict()
        # 武器 ID（MultiWeaponManagerComponent）：
        # actor -> {"first": 首装备武器ID, "ids": [(seq, weaponId)...]}
        # weaponId 为 11 位模板 ID（1801/1802/1810/2102 段实测）。
        self.weapons = {}
        # 快照期缓存（每次 /api/state ~2.5Hz 复用；poll 新增数据即失效）：
        self._seqs_cache = {}     # slot -> 递增 seq list（供 bisect）
        self._deadbox_cache = {}  # correlate_death_boxes 指纹缓存（slot/boxes 不变即命中）
        # 角色生命周期追踪：通道 -> 当前角色本体 actor；actor -> (close_seq, close_reason)
        # close_reason ∈ {0=Destroyed, 2=LevelUnloaded, 3=Relevancy, 4=TearOff} = 角色消失
        # close_reason=1(Dormancy) = 子对象休眠，忽略（蹲点角色不消失）
        self.ch_actor = {}       # channel -> 当前角色本体 actor guid
        self.gone = {}           # actor -> (close_seq, close_reason)
        self.box_dead = {}       # actor -> box_seq（死亡盒 owner 死亡信号，见 _derive）
        # AI 角色名字表（LOC_AICharacterNameTable，每局一张，wire 只同步部分条目）：
        # key(int, "<N>_Name" 的 N) -> 显示名。AI spawn 无 PlayerState 引用
        # （h14/h18/h19 全 None），名字走 LOCTABLE 本地化引用 -> 本表解析。
        self.ai_names = {}
        # field13 小队广播（我 + 2 队友，服务器直发）：确定性自机坐标，
        # 不依赖 MRA 槽位认领 —— 闭世界失效的对局（AI 抢槽）由它兜底。
        self.f13_self = None        # (seq, ts, [x,y,z] cm)
        self.f13_self_idx = 0       # 最近全量帧里"我"的下标
        # field13 全轨迹（按帧内下标，仅全量帧追加——增量帧无 vals64 无法定位下标）：
        # 语义定版（用户确认）：MRA 成员槽广播覆盖所有被追踪角色（含敌人/AI），
        # 槽位 ≠ 队友；唯一协议级可靠的队友信号 = field13 小队广播的非自机轨迹。
        self.f13_tracks = {}        # track_idx -> [(seq, ts, x, y, z)...]
        self.mate_slots = {}        # slot -> f13 轨迹 idx（队友槽位，见 _derive）
        self.anon_names = {}        # slot -> PlayerState（匿名槽 seq 对齐命名，见 _derive）
        self._slot_samples_prev = -1  # 上一轮的 slot 采样总数（derive 脏检查）

    # ---- 增量 guid 路径 + 类 GUID ----
    def _accumulate_paths(self, new_lbs):
        for x in new_lbs:
            # MRA 通道锁定后只承载 handle-21 移动复制（不携带类导出路径），
            # 跳过其前缀解析（确定性结构规则，非启发式）
            if self.mra_locked and x.ch == self.mra_ch:
                continue
            # 重组 bunch(partial 分片重组,frags 非空)payload 同样含导出
            # 段——0830 装备链路:开局全量的装备组件路径只在重组 bunch 的
            # 导出里,跳过它们=22 个 CharacterEquip 路径进不了 guid_paths,
            # 全员装备块路由失败(44741 局实证)
            if not x.hx:
                continue
            try:
                info, _ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=False, frags=x.frags)
            except Exception:
                # 重组的运动类 bunch 前缀结构与状态 bunch 不同，解析越位
                # （IndexError 等）——只影响导出收集，跳过即可
                continue
            for e in info.get("exports", []):
                p = e.get("path")
                if p:
                    g = e["guid"]
                    if g not in self.guid_paths or len(p) > len(self.guid_paths[g]):
                        self.guid_paths[g] = p

    def _resolve_archs(self):
        """增量版：guid_paths 只增不减，只分类上次之后新增的路径。

        原实现每 poll 全量重走全部路径做 rsplit/startswith（~5k 路径×3 次
        startswith，稳态 poll 第二热点）；归档集合只增语义不变——同一 guid
        的路径不会变化，成员判定（arch in self.ps_archs）结果一致。"""
        if len(self.guid_paths) == self._archs_idx:
            return
        ps, ch = self.ps_archs, self.char_archs
        box, carry = self.box_archs, self.carry_archs
        pk = self.pk_archs
        for g, p in islice(self.guid_paths.items(), self._archs_idx, None):
            tail = p.rsplit(".", 1)[-1]
            if tail.startswith("Default__"):
                tail = tail[9:]
            if tail == "CharacterFashionComponent":
                self._fashion_guids.add(g)
            if tail == "BP_DFMCharacter_C":
                ch.add(g)
            elif tail.startswith("BP_DFM") and "Character" in tail and tail.endswith("_C"):
                # AI 角色子类（BP_DFMCharacter_AI_DT_C / ..._RPG_C /
                # BP_DFMAICharacter_ShielderLight_C / ..._Firebat_C 等）也是角色 pawn，
                # 只是属性布局可能不同（单独处理）
                ch.add(g)
            elif tail == "BP_AZCharacter_C":
                # 核弹局（4928 实证）：真人 Character 换用 BP_AZCharacter_C，
                # 不带 BP_DFM 前缀、不匹配上面两条规则 → 整局真人不上屏。
                # 精确名单匹配，不能用宽泛含 "AZ"/"Character" 判断 —— 同图还有
                # BP_InteractorContainer_AZ_NukesBackpack_C /
                # BP_DynamicWaterSystem_AZ_C 等非角色类。
                ch.add(g)
            elif tail in ("BP_DFMPlayerState_C", "BP_SOLPlayerState_C"):
                ps.add(g)
            elif tail == "BP_Inventory_DeadBody_C":
                box.add(g)
            elif tail == "BP_Inventory_CarryBody_C":
                carry.add(g)
            elif tail.startswith("BP_InventoryPickup"):
                # 露天物资（BP_InventoryPickup_C + BP_InventoryPickup_
                # FromSingleItemContainer_C 容器搜出变体）。宽前缀匹配：
                # 两变体共用同一解析路径，未来新增变体自动覆盖。
                pk.add(g)
        self._archs_idx = len(self.guid_paths)
        self.ps_archs, self.char_archs = ps, ch
        self.box_archs, self.carry_archs = box, carry
        self.pk_archs = pk

    # ---- 单 LB 解码 ----
    def _merge_playerstate(self, actor, out, seq, channel, *, opened=False, verified=True):
        """Merge one PlayerState replication delta without inventing defaults.

        ``verified=False`` keeps a partial-identity merge from claiming the PS is
        schema-verified (lifecycle observers gate on that flag).
        """
        life = self.player_state.setdefault(
            actor, {"values": {}, "seq": {}})
        if verified:
            life["schema_verified"] = True  # this helper accepts already-decoded named/legacy fields
        values = life["values"]
        field_seq = life["seq"]
        prev = self.players.get(actor)
        if prev is None:
            prev = {
                "name": None, "player_id": None, "bot": None,
                "actor_guid": actor, "owner_guid": None, "channel": channel,
                "uin": None, "team": None, "camp": None, "inner_team": None,
                "hero_id": None, "expert_id": None,
                "spawn_seq": seq if opened else None,
                "life_state": values, "life_seq": field_seq,
            }
            self.players[actor] = prev
        else:
            prev["life_state"] = values
            prev["life_seq"] = field_seq
            if opened and prev.get("spawn_seq") is None:
                prev["spawn_seq"] = seq
        prev["channel"] = channel

        # Identity is also delta-replicated.  A missing handle means unchanged.
        for h, key in ((18, "player_id"), (21, "bot"), (14, "owner_guid"),
                       (28, "uin"), (62, "team"), (63, "camp"), (30, "inner_team"),
                       (137, "hero_id"), (138, "expert_id")):
            if h in out:
                prev[key] = out[h]
        if 27 in out and out[27]:
            prev["name"] = out[27]

        # Life fields keep their own wire sequence so an omitted field never
        # clears a previous value and a late/replayed delta cannot roll it back.
        for h, name in PS_LIFE_FIELDS.items():
            if h not in out or seq < field_seq.get(name, -1):
                continue
            values[name] = out[h]
            field_seq[name] = seq

    def _handle_player_lb(self, x):
        """Route exact named PlayerState properties using its exported class."""
        try:
            exports, start = exports_prefix(x)
            for entry in exports:
                if entry.get("path"):
                    self.guid_paths[entry["guid"]] = entry["path"]
            if x.hx and start == x.pbits:
                return
            if x.open:
                self.ps_ch_actor.pop(x.ch, None)
                if x.close:
                    return
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
                info, start = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=False, frags=x.frags)
                profile = select_profile(self.guid_paths.get(info.get("arch")))
                if profile is None:
                    return
                actor = info["actor"]
                self.ps_ch_actor[x.ch] = actor
                self.ps_profiles[actor] = profile
            else:
                actor = self.ps_ch_actor.get(x.ch)
                if actor is None:
                    return
                profile = self.ps_profiles.get(actor)
            for block in tile(x.payload, x.pbits, start, partial_ok=True) or []:
                if not block.get("is_actor") or not block.get("has_rep") or block.get("deleted"):
                    continue
                decoded = decode_playerstate(x.payload, block["payload_start"], block["payload_end"], profile)
                status = decoded["status"]
                self.ps_decode_stats[status] = self.ps_decode_stats.get(status, 0) + 1
                if status == "exact":
                    self._merge_named_playerstate(actor, decoded["fields"], x.seq, x.ch, opened=bool(x.open))
                    continue
                # 非 exact 块：身份字段（记录最前面的纯标量）仍可信，只并身份、
                # 不标 schema_verified、不写生命周期值（见 IDENTITY_FIELDS 注释）。
                # 这条正是自机名字的来源：本地玩家 PS 的记录布局会中途不匹配
                # （实测 170: 21870bit 于 11893 报 error），旧代码整块丢弃 ->
                # 自机 PS 无名 -> 真机候选把它排除 -> 自机名退化成同局别人。
                identity = decoded.get("identity") or {}
                if identity:
                    self._merge_named_playerstate(actor, identity, x.seq, x.ch,
                                                  opened=bool(x.open), verified=False)
        except (sna.Bad, BitError, IndexError):
            self.ps_decode_stats["framing_error"] = self.ps_decode_stats.get("framing_error", 0) + 1

    def _merge_named_playerstate(self, actor, fields, seq, channel, *, opened=False, verified=True):
        """把命名为键的 PS 字段并入 players / player_state。

        verified=False：只并身份（非 exact 块的 identity 白名单），**不**把该 PS 标为
        schema_verified —— 生命周期/生死仍只信 exact 块（见 IDENTITY_FIELDS 注释）。
        """
        self._merge_playerstate(actor, {}, seq, channel, opened=opened, verified=verified)
        life = self.player_state[actor]
        life["profile"] = self.ps_profiles.get(actor)
        player = self.players[actor]
        identity = {"Owner":"owner_guid", "PlayerId":"player_id", "bIsABot":"bot",
                    "PlayerNamePrivate":"name", "Uin":"uin", "InnerTeamIndex":"inner_team",
                    "TeamID":"team", "Camp":"camp", "CampID":"camp", "HeroId":"hero_id", "ExpertId":"expert_id"}
        for name, value in fields.items():
            if seq < life["seq"].get(name, -1):
                continue
            life["values"][name] = value
            life["seq"][name] = seq
            if name in identity:
                if name == "Owner" and isinstance(value, dict):
                    value = value.get("guid")
                player[identity[name]] = value

    def _handle_player_close(self, x):
        if x.close:
            self.ps_ch_actor.pop(x.ch, None)

    def _handle_char_lb(self, x):
        bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
        try:
            info, ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=True, frags=x.frags)
        except sna.Bad:
            return
        if info.get("arch") not in self.char_archs:
            return
        # 0904：本 bunch 导出里出现的时装组件实例 guid（hero 解码路由用）
        for _e in info.get("exports", []):
            _p = _e.get("path")
            if isinstance(_p, str) and "CharacterFashion" in _p.rsplit("/", 1)[-1]:
                self._fashion_guids.add(_e["guid"])
        a = info.get("actor"); loc = info.get("loc")
        if not a:
            return
        blk = tile(x.payload, x.pbits, ss + 8, partial_ok=True)
        out = {}
        if blk and blk[0].get("is_actor") == 1 and blk[0].get("has_rep") == 1:
            out = nl.decode_character_identity(x.payload, blk[0]["payload_start"],
                                  blk[0]["payload_end"], self.char_cmds, self.char_parents)
        # 0904: hero 从角色时装组件解（join 全量包就带 currentCharacterHeroID，
        # 新版 PS h137 不再发送）；先扫描，link 建好后再挂，避免抢先建条目
        # 丢掉 ai_key。
        _fashion_hid = None
        if blk and self._fashion_guids:
            for bk in blk:
                if bk.get("deleted") or bk.get("sub") not in self._fashion_guids:
                    continue
                if bk["payload_end"] <= bk["payload_start"]:
                    continue
                _fashion_hid = nl._walk_fashion_hero(x.payload, bk["payload_start"],
                                                     bk["payload_end"], self._fashion_cmds,
                                                     self._fashion_names, self._fashion_h)
                if _fashion_hid is not None:
                    break
        if a not in self.links:
            self.links[a] = {"owner": out.get(14), "ps_guid": out.get(18),
                             "spawn_seq": x.seq, "loc": loc, "num_exports": info.get("num_exports"),
                             "cls": self.guid_paths.get(info.get("arch"), "").rsplit(".", 1)[-1],
                             "ai_key": ant.extract_ref_key(x.payload)}
        else:
            if self.links[a]["owner"] is None and out.get(14) is not None:
                self.links[a]["owner"] = out.get(14)
            if self.links[a]["ps_guid"] is None and out.get(18) is not None:
                self.links[a]["ps_guid"] = out.get(18)
            # AI 名字引用可能晚于首个 spawn 到达（如 Firebat 首包只有 PoolName）
            if self.links[a].get("ai_key") is None:
                self.links[a]["ai_key"] = ant.extract_ref_key(x.payload)
        nl.merge_character_identity(self.links[a], out, x.seq)
        self.life_tracker.spawn(a, self.links[a].get("spawn_seq") or x.seq)
        if _fashion_hid is not None and self.links[a].get("hero") is None:
            self.links[a]["hero"] = _fashion_hid
        # 记录该通道当前角色本体（用于 close 事件归属）
        self.ch_actor[x.ch] = a
        # 独立 FSM 归属表：close 不清（死亡后的子对象状态块仍归原角色）
        self._fsm_ch_actor[x.ch] = a
        # 角色重新 spawn（复活）→ 清除消失状态。仅当 spawn 晚于已记录的 close
        # 才清除（防御任何残留乱序：早序 spawn 不得清除晚序 close 的死亡判定）。
        g = self.gone.get(a)
        if g is None or x.seq >= g[0]:
            self.gone.pop(a, None)

    def _handle_char_delta(self, x):
        actor = self.ch_actor.get(x.ch)
        if actor is None:
            return
        try:
            _, start = exports_prefix(x)
            for block in tile(x.payload, x.pbits, start) or []:
                if block["is_actor"] and block["has_rep"]:
                    fields = nl.decode_character_identity(x.payload,
                        block["payload_start"], block["payload_end"],
                        self.char_cmds, self.char_parents)
                    nl.merge_character_identity(self.links[actor], fields, x.seq)
        except (sna.Bad, IndexError):
            return

    def _decode_box_owner(self, x, ss):
        """死亡盒/背尸盒内容块的 handle 14(Owner) = 死亡者引用（协议级，非几何关联）。

        依据（flow 465 实证）：死亡盒内容块用 AActor 基础 RepLayout 解码，
        handle 14(Object)=Owner 指向**死亡者 Character**：
          - box 12668 handle14=10266（BP_DFMCharacter AI，TearOff@22018 死亡）
          - box 12644 handle14=10422（BP_DFMCharacter AI，TearOff@21851 死亡）
        两个 Owner 都是 TearOff（死亡）角色，且盒子位置接近死者最终位置。
        注意：handle 8 = AttachmentReplication.AttachParent（盒子挂靠的父对象，
        可能是 CarryBody/舔包者），**不是死者**——上一版误用了 h8（flow 495
        box 8574 h8=1564 是活 AI 走到盒子旁，非死者）。h14(Owner) 才是死者。

        实现：手动 walk AActor 基础（h1-h16），跳过 Vector100/Rotator 等复杂
        字段（repdec4.read_val 支持），读到 h14(Object) 即返回 guid。
        （不能用 nl._decode_flat：它的 read_val 不支持 type 13 Vector100，
        会在 h10 提前退出导致 h14 读不到。）
        """
        import repdec4
        from codec.engine_bunch import read_serialized_fname
        blk = tile(x.payload, x.pbits, ss)
        if not blk:
            return None
        k = blk[0]
        if not (k.get("is_actor") == 1 and k.get("has_rep") == 1):
            return None
        r = BitReader(x.payload, k["payload_end"]); r.position = k["payload_start"]
        try:
            r.bit()  # bDoChecksum
            h, _ = read_dfm_packed_int(r)
            while h != 0:
                if 1 <= h <= 5:
                    r.bit()
                elif h == 6:
                    nl.ser_int(r, 4)            # RemoteRole
                elif h == 7:
                    return None                 # ReplicatedMovement（复杂结构，死亡盒不复制）
                elif h == 8:
                    read_dfm_packed_int(r)      # AttachParent
                elif h in (9, 10):
                    repdec4.read_val(r, 13, None)   # Vector100
                elif h == 11:
                    repdec4.read_val(r, 11, None)   # Rotator
                elif h == 12:
                    read_serialized_fname(r)        # Name
                elif h == 13:
                    read_dfm_packed_int(r)      # AttachComponent
                elif h == 14:
                    g, _ = read_dfm_packed_int(r)   # Owner = 死者
                    return int(g)
                elif h == 15:
                    nl.ser_int(r, 4)            # Role
                elif h == 16:
                    read_dfm_packed_int(r)      # Instigator
                else:
                    return None                 # h17+，非 AActor 基础
                h, _ = read_dfm_packed_int(r)
            return None
        except BitError:
            return None

    def _handle_box_lb(self, x):
        self.box_tracker.consume(x)
        self._sync_boxes()

    def _sync_boxes(self):
        self.boxes = {actor: rec for actor, rec in self.box_tracker.active.items() if rec["kind"] == "box"}
        self.carry_owner = {actor: rec.get("deceased_actor") or rec.get("owner_actor")
                            for actor, rec in self.box_tracker.active.items() if rec["kind"] == "carry"}

    def _handle_carry_lb(self, x):
        self.box_tracker.consume(x)
        self._sync_boxes()

    def _handle_pickup_lb(self, x):
        self.pickup_tracker.consume(x)

    def _handle_pickup_close(self, x):
        self.pickup_tracker.close_channel(x.ch, x.seq, x.close_reason)

    def _handle_char_close(self, x):
        """角色通道 close 事件：按 close_reason 判定角色是否消失。

        EChannelCloseReason（UE4.26）:
            0 = Destroyed       （销毁）
            1 = Dormancy        （休眠 —— 子对象/组件，角色保留）
            2 = LevelUnloaded   （关卡卸载）
            3 = Relevancy       （离开更新距离 —— 点消失）
            4 = TearOff         （死亡 tearoff —— 点消失 + 盒子）

        只处理「角色本体消失」类 reason {0,2,3,4}；reason=1 是子对象休眠，
        角色仍在（蹲点），忽略。
        """
        if not x.close:
            return
        reason = x.close_reason
        # reason=1 (Dormancy) 是子对象组件休眠，不是角色本体消失；其余 {0,2,3,4} 消失
        if reason is None or reason not in st.GONE_CLOSE_REASONS:
            return
        # 关闭即清空通道归属：角色通道关闭后，同通道后续的 close 是子对象（组件）
        # 的 Dormancy/Destroyed，不是角色本体消失——不能归到已关闭角色头上（否则
        # 会把「离开相关性 Relevancy」误判成「销毁 Destroyed」，污染死盒/槽复用判定）。
        actor = self.ch_actor.pop(x.ch, None)
        if actor is None:
            return
        # 记录消失（最后 close 胜出；复活会由 spawn 清除）。防御：早序 close 不得
        # 覆盖晚序 close（排序后本应天然成立）。
        prev = self.gone.get(actor)
        if prev is not None and x.seq < prev[0]:
            return
        self.gone[actor] = (x.seq, reason)

    def _handle_mra_lb(self, x):
        ts = self._seq_ts.get(x.seq, 0.0)
        try:
            blk = tile(x.payload, x.pbits, 0)
        except Exception:
            return
        if not blk:
            return
        for k in blk:
            if not k["has_rep"] or k["deleted"] or not k["is_actor"]:
                continue
            r0 = BitReader(x.payload, k["payload_end"]); r0.position = k["payload_start"]
            try:
                if r0.bit():
                    continue
                h, _ = read_dfm_packed_int(r0)
                if h != 21:
                    continue
                fr, _r = rm4.read_frame4(x.payload, r0.position, k["payload_end"])
                # Do not publish a successfully decoded prefix of an unknown
                # movement layout. The RepLayout handle terminator must follow
                # the complete native frame and end exactly at this block.
                if fr.get("stopped") or read_dfm_packed_int(_r)[0] != 0 \
                        or _r.position != k["payload_end"]:
                    self.mra_frame_stats["incomplete"] += 1
                    continue
            except Exception:
                self.mra_frame_stats["error"] += 1
                continue
            self.mra_frame_stats["accepted"] += 1
            for i, it in fr["items"].items():
                p = it.get("pos")
                if not p or not WORLD(p):
                    continue
                rot = it.get("rot")
                yaw = rot[1] if rot else None
                self.slots.setdefault(i, []).append(
                    (x.seq, ts, float(p[0]), float(p[1]), float(p[2]), yaw))

        # ---- 主轮询 ----
    def poll(self, max_frames=None):
        added = self.loader.poll(max_frames=max_frames)
        b = self.loader.b
        with self.lock:  # poll 写 v.slots/v.boxes/v.players 与 snapshot 读互斥
            # 更新 seq->ts（增量，只消费新增）；并记最新帧到达时刻（对局销毁判定用）
            for seq, ts in self.loader.seq_ts[self._seq_ts_idx:]:
                self._seq_ts.setdefault(seq, ts)
                if ts > self._last_data_ts:
                    self._last_data_ts = ts
            self._seq_ts_idx = len(self.loader.seq_ts)
            # 1) 累积导出路径 + 解析类 GUID
            new_lbs = []
            for ci, lst in b.items():
                idx = self._spawn_idx.get(ci, 0)
                if idx < len(lst):
                    new_lbs.extend(lst[idx:])
                    self._spawn_idx[ci] = len(lst)
            # 关键：spawn/close 生命周期必须按全局 seq 顺序处理。b.items() 按通道
            # 分组，若直接按通道顺序处理，高通道号的「早 seq」close 会覆盖低通道
            # 号的「晚 seq」open/close，导致 gone 状态错乱——实测 465 局 actor=1168
            # 重进相关性后 spawn@69006 反被 ch 号更大的 ch 上的 close@36878 覆盖，
            # 表现为「重进范围死人重现 / 重进范围丢名字」。按 (seq, ch) 排序后与
            # wire 真实时间线一致。
            new_lbs.sort(key=lambda x: (x.seq, x.ch))
            self._accumulate_paths(new_lbs)
            self._resolve_archs()
            # 1.5) 当局 AI 名字表（LOC_AICharacterNameTable）：廉价字节包含检查，
            #      命中才解析。同 key 多实例取 seq 更大者（对局内状态以最新为准）。
            for x in new_lbs:
                if ant.TABLE_HEAD in x.payload:
                    for k, nm in ant.parse_table_instance(x.payload).items():
                        self.ai_names[k] = nm
            # 1.6) field13 小队广播（我 + 2 队友）-> 确定性自机坐标。
            #      注意：field13 走已打开通道的普通 bunch（open=0），不能按 open 过滤；
            #      解码自带 field_index==13 校验，非 field13 LB 在外层快速拒绝。
            for x in new_lbs:
                if x.pbits < 64:
                    continue
                if self.mra_locked and x.ch == self.mra_ch:
                    continue
                try:
                    dec = f13.decode(x.payload, x.pbits)
                except Exception:
                    continue
                if not dec or not dec["locs"]:
                    continue
                if dec["vals64"] and any(v in f13.SELF_WIRE for v in dec["vals64"]):
                    self.f13_self_idx = f13.self_index(dec["vals64"])
                si = min(self.f13_self_idx, len(dec["locs"]) - 1)
                if self.f13_self is None or x.seq >= self.f13_self[0]:
                    self.f13_self = (x.seq, self._seq_ts.get(x.seq, 0.0),
                                     [float(v) for v in dec["locs"][si]])
                # 全量帧（带 vals64）才追加全轨迹：下标 = vals64 下标，帧内对齐可靠；
                # 增量帧无 vals64、无法确定 locs 属于哪些轨迹，跳过（队友匹配用）。
                if dec["vals64"]:
                    ts = self._seq_ts.get(x.seq, 0.0)
                    for i, loc in enumerate(dec["locs"]):
                        self.f13_tracks.setdefault(i, []).append(
                            (x.seq, ts, float(loc[0]), float(loc[1]), float(loc[2])))
            # 2) spawn / close 检测（新 LB；MRA 锁定后跳过该通道——只承载移动复制）
            actor_by_lb = {}
            for x in new_lbs:
                self.weapon_tracker.consume(x)
                self.team_tracker.consume(x)
                self.pickup_tracker.consume(x)
                self.container_tracker.consume(x)
                closing_box = self.box_tracker.channels.get(x.ch)
                self.box_tracker.consume(x)
                self.box_contents.consume(x)
                if not x.open and x.close and x.close_reason in (0, 2, 4) and closing_box is not None:
                    self.box_contents.forget(closing_box, x.seq)
                if self.mra_locked and x.ch == self.mra_ch:
                    continue
                # PlayerState properties continue on ordinary (open=0)
                # content-block bunches.  Route those before the lifecycle
                # split; the handler itself resolves the channel generation.
                if not x.close:
                    self._handle_player_lb(x)
                if x.open:
                    self.ch_actor.pop(x.ch, None)
                    self._fsm_ch_actor.pop(x.ch, None)
                    self._handle_char_lb(x)
                elif x.close:
                    self._handle_player_close(x)
                    # 角色通道 close：按 close_reason 判定消失（死亡/离开相关性/销毁）
                    self._handle_char_close(x)
                else:
                    self._handle_char_delta(x)
                # Preserve ownership at this packet, before a later open in
                # the same batch reuses the channel for a different actor.
                actor_by_lb[id(x)] = self._fsm_ch_actor.get(x.ch)
            # 3) MRA 通道锁定 + 槽位解析
            if not self.mra_locked:
                mra_ch, mra_n = nl.find_mra_channel(b)
                if mra_ch is not None and mra_n >= 16:
                    self.mra_ch = mra_ch
                    self.mra_locked = True
                    self._mra_idx = 0
            if self.mra_locked:
                lst = b.get(self.mra_ch, [])
                for x in lst[self._mra_idx:]:
                    self._handle_mra_lb(x)
                self._mra_idx = len(lst)
            self.n_lb = self.loader.n_lb
            # 4) FSM/血量**增量**解码：必须在 _derive 之前——derive 的
            #    vital_dead（致命部位归零）从 self.health 读，先 derive 会用
            #    上一轮的旧血量（死亡信号滞后一轮）。只解本轮新增的子对象块，
            #    O(新增块)，不阻塞 poll/snapshot（单块 tile+repdec6 亚毫秒级）。
            if new_lbs:
                self._decode_fsm_incremental(new_lbs, actor_by_lb)
                self._decode_health_batches(new_lbs)
                self._sync_boxes()
            # 5) 重算 join/self/ents —— 仅当有输入变化时。join 依赖 links/players/
            #    slots，全部自新增数据推导；无新增（纯空闲轮或 MRA 增量未触发
            #    links 变化）时上一轮的 ents 仍有效，跳过重算（O(links×slots)）。
            #    注意：MRA 移动复制（self.slots 追加）即便无新增角色 spawn 也可能
            #    改变 slot 归属（新角色轨迹跨过 500cm 门限），故按 slot 总采样数粗判。
            cur_slot_samples = sum(len(tr) for tr in self.slots.values())
            dirty = bool(new_lbs) or (cur_slot_samples != self._slot_samples_prev)
            self._slot_samples_prev = cur_slot_samples
            if dirty:
                self._derive()
                self._loot_pick_scan()
                self._compact_histories()
            if self._compact_enabled:
                self._compact_consumed()
        changed = bool(new_lbs) or added > 0
        if changed:
            self.state_version += 1
        return changed

    def _slot_near_pose(self, loc, max_cm: float = 500.0,
                        pose_seq=None, margin_cm: float = POSE_MARGIN_CM):
        """离上行自机坐标（客户端自报，权威）最近的槽号；有歧义返回 None（绝不猜）。

        阈值 500cm：实测自机槽差 103cm，次近的 1167cm，区分度足够。上行没在发 /
        不在对局时返回 None，调用方自动回落到原来的启发式。

        **三道闸（缺一不可）**——纯最近邻在"自己和队友贴在一起"时会认错人：
        185435 局实测 pose 距队友槽 113cm、距真自机槽 146cm，最近邻选中队友，
        整帧把自机名字/位置换成队友（z张道友）：
          ① 陈旧位姿不作数：pose.seq 落后最新槽采样超过 POSE_STALE_SEQ 说明上行
             已停（倒地/阵亡），此时的最近邻比对没有意义；
          ② 无歧义才认：次近槽若离 pose 也很近（< 最近 + margin 且 < 2×最近），
             说明自机与别人贴身，返回 None 交给 PS 通道序启发式（身份权威）；
          ③ 粘性：已钉住的槽仍在半径内、且新候选没有明显更近时保持不动，避免
             两个相邻槽逐帧来回抢（名字闪烁）。
        """
        if not loc:
            return None
        try:
            tx, ty = float(loc[0]), float(loc[1])
        except (TypeError, ValueError, IndexError):
            return None
        cand = []                        # (d2, slot, samples)
        newest = -1
        for slot, tr in self.slots.items():
            if not tr:
                continue
            r = tr[-1]
            if r[0] > newest:
                newest = r[0]
            d2 = (float(r[2]) - tx) ** 2 + (float(r[3]) - ty) ** 2
            cand.append((d2, slot, len(tr)))
        if not cand:
            return None
        # ① 位姿陈旧（上行已停发）：不认槽，交给启发式
        if (pose_seq is not None and newest > 0
                and newest - int(pose_seq) > POSE_STALE_SEQ):
            return None
        cand.sort(key=lambda t: (t[0], -t[2]))
        bd2, bslot, _n = cand[0]
        if bd2 > max_cm * max_cm:
            return None
        bd = bd2 ** 0.5
        # ③ 粘性：已钉住的槽仍在半径内，且新候选没有明显更近 -> 保持不变
        prev = getattr(self, "_self_slot_pose", None)
        if prev is not None:
            for d2, slot, _cnt in cand:
                if slot == prev:
                    pd = d2 ** 0.5
                    if pd <= max_cm and bd + margin_cm > pd:
                        return prev
                    break
        # ② 歧义（自机与别人贴身）：不猜
        if len(cand) > 1:
            sd = cand[1][0] ** 0.5
            if sd < bd + margin_cm and sd < bd * 2.0:
                return None
        return bslot

    def decode_error_count(self) -> int:
        """各内容解码器累计的"分帧/前缀"错误数。

        这些错误几乎只在**增量消费**时出现：解码器按"每批新增 LB"跑，批边界如果
        切在一条跨多 LB 的记录中间，就会看到半条记录。整局一次性重解时批边界只有
        一个，所以基本为 0。数字用于触发污染自愈（见 er_radar 的 live tick）。
        """
        n = 0
        for st in (getattr(self, "ps_decode_stats", None),
                   getattr(getattr(self, "health_batches", None), "stats", None),
                   getattr(getattr(self, "life_tracker", None), "stats", None),
                   getattr(getattr(self, "weapon_tracker", None), "stats", None),
                   getattr(getattr(self, "pickup_tracker", None), "stats", None),
                   getattr(getattr(self, "box_tracker", None), "stats", None),
                   getattr(getattr(self, "box_contents", None), "stats", None)):
            if not isinstance(st, dict):
                continue
            for k in ("framing_error", "prefix_error", "error", "layout_error"):
                try:
                    n += int(st.get(k) or 0)
                except (TypeError, ValueError):
                    pass
        return n

    def _compact_consumed(self):
        """Discard consumed wire records, retaining derived identity/history."""
        self.loader.seq_ts.clear()
        self._seq_ts_idx = 0
        # Unlocked MRA detection searches its accumulated sample; keep that
        # sample until a channel is confirmed so compaction cannot break it.
        if not self.mra_locked:
            return
        for channel, records in list(self.loader.b.items()):
            consumed = self._spawn_idx.get(channel, 0)
            if channel == self.mra_ch:
                consumed = min(consumed, self._mra_idx)
                self._mra_idx -= consumed
            if consumed:
                del records[:consumed]
                self._spawn_idx[channel] -= consumed
            if not records:
                del self.loader.b[channel]
                self._spawn_idx.pop(channel, None)

    @staticmethod
    def _compact_track(track):
        """Bound a monotonically appended track while preserving its recent tail."""
        if len(track) <= TRACK_HISTORY_MAX:
            return False
        recent_n = TRACK_HISTORY_RECENT
        old = track[:-recent_n]
        recent = track[-recent_n:]
        old_budget = TRACK_HISTORY_MAX - recent_n
        if len(old) > old_budget:
            stride = (len(old) + old_budget - 1) // old_budget
            old = old[::stride]
        track[:] = old + recent
        return True

    def _compact_histories(self):
        """Bound long-run movement/time indexes without resetting live state."""
        compacted = False
        for slot, track in list(self.slots.items()):
            if self._compact_track(track):
                compacted = True
                # The list contents changed, not just its length.
                self._seqs_cache.pop(slot, None)
                self._pk_chk[slot] = len(track)
        for track in self.f13_tracks.values():
            compacted = self._compact_track(track) or compacted

        # Old sequence timestamps are only needed for recent FSM timing and
        # spawn-age display.  Track records already carry their own timestamps.
        if len(self._seq_ts) > SEQ_TS_MAX:
            latest = max(self._seq_ts)
            cutoff = latest - SEQ_TS_MAX
            for seq in list(self._seq_ts):
                if seq < cutoff:
                    self._seq_ts.pop(seq, None)
        return compacted

    def _loot_pick_scan(self):
        """Record pickup candidates from movement, without declaring removal.

        Only a close reason 0/2/4 is a protocol-level disappearance signal.
        Standing beside an item is useful evidence, but an observer cannot
        prove the inventory transaction from that posture alone.
        """
        if not LOOT_PICK_INFER or not self.loot:
            return
        R2 = LOOT_PICK_RADIUS_CM * LOOT_PICK_RADIUS_CM
        for s, tr in self.slots.items():
            i0 = self._pk_chk.get(s, 1)
            if i0 >= len(tr):
                self._pk_chk[s] = len(tr)
                continue
            for i in range(max(1, i0), len(tr)):
                q, p = tr[i], tr[i - 1]
                dt = q[1] - p[1]
                if dt <= 0:
                    continue
                v = (((q[2] - p[2]) ** 2 + (q[3] - p[3]) ** 2
                      + (q[4] - p[4]) ** 2) ** 0.5) / dt
                if v >= LOOT_PICK_VMAX:
                    continue
                for a, lr in list(self.loot.items()):
                    if lr.get("pickup_candidate_seq") is not None \
                            or lr.get("gone_seq") is not None:
                        continue
                    if q[0] < lr["seq"]:
                        continue
                    dx = q[2] - lr["loc"][0]
                    dy = q[3] - lr["loc"][1]
                    if dx * dx + dy * dy <= R2:
                        lr["pickup_candidate_seq"] = q[0]
                        lr["pickup_candidate_slot"] = s
                        lr["pickup_confidence"] = "candidate"
            self._pk_chk[s] = len(tr)

    def _tail2catname(self, g):
        """guid -> 目录名（tail 匹配 TAIL2CAT；无则 None）。"""
        p = self.guid_paths.get(g)
        if not p:
            return None
        t = p.rsplit(".", 1)[-1]
        if t.startswith("Default__"):
            t = t[9:]
        return st.TAIL2CAT.get(t)

    def _cls_to_cat(self):
        """guid -> 目录名（基于当前已解析的 guid_paths，增量刷新）。

        两条路线都要建（6870 局实证）：显式类块用 `cls` guid；**stably-named 块
        （99% 的子对象块）没有 cls，其 `sub` guid 即类实例的稳定名 GUID，
        spawn 时带路径导出、同样落在 guid_paths 里**——旧实现只查 cls 导致
        状态/血量块全部漏解。"""
        cat = self._cls2cat
        miss = self._cls2cat_miss
        paths = self.guid_paths
        changed = paths.take_changed() if isinstance(paths, _GuidPaths) else paths
        for g in changed:
            if g in cat:
                continue
            p = self.guid_paths.get(g)
            if (g, p) in miss:
                continue
            cm = self._tail2catname(g)
            if cm and cm in self._subcats:
                cat[g] = cm
            else:
                # 负缓存：guid_paths[g] 与 TAIL2CAT 都不变 → 未命中恒未命中。
                # 原实现每 poll 对 ~5k 未命中路径重查 rsplit/startswith（473 万
                # 次/_120tick），键含 path 防 guid 路径被重导出改尾
                miss.add((g, p))
        return cat

    def _decode_health_batches(self, bunches):
        for bunch in bunches:
            self.health_batches.consume(bunch)
            # Drain per bunch: a death followed by revival in the same poll
            # must not collapse into one late field value.
            for actor, name, value, seq in self.health_batches.drain():
                self._record_health(actor, name, value, seq, source="property_replication")

    def _record_health(self, actor, name, value, seq, source="attribute_set"):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return
        if not np.isfinite(value) or value < 0:
            return
        if source != "property_replication" and not health_val_ok(name, value):
            return
        if name in {wire for _, wire in HP_PARTS} and value > 500:
            return
        if name in {"Max" + wire for _, wire in HP_PARTS} and not 5 <= value <= 500:
            return
        rec = self.health.setdefault(actor, {"parts": {}, "impending": None, "seq": seq})
        seen = rec.setdefault("part_seq", {})
        if seq < seen.get(name, -1):
            return
        seen[name] = seq
        rec.setdefault("sources", {})[name] = source
        rec["seq"] = max(seq, rec["seq"])
        value = float(value)
        if name == "IsAlive":
            before = self.life_tracker.current(actor)
            if self.life_tracker.observe(actor, value, seq, source=source):
                if before["dead"] and value in (1, 3):
                    rec["revive_seq"] = seq
        if name == "ImpendingDeathHealth":
            if rec["impending"] is not None and value < rec["impending"]:
                rec["imp_chg_seq"] = seq
            rec["impending"] = value
        else:
            previous = rec["parts"].get(name)
            if name in ("HeadHealth", "ThoraxHealth", "AbdomenHealth") \
                    and previous == 0 and value > 0:
                rec["revive_seq"] = seq
            rec["parts"][name] = value

    def _decode_fsm_incremental(self, new_lbs, actor_by_lb=None):
        """增量解码新增子对象块 → 角色状态 + 血量。

        与 state_track.extract_fsm_state 同源：sub-object 块（is_actor=0）用
        repdec6 按类目录解码。差异是**只处理本轮新增的块**（O(新增) 非全量），
        并且用 `_fsm_ch_actor`（该通道最近角色）作 actor 归属。

        块的类 guid 两条路线（6870 局实证）：显式类块取 `cls`；stably-named 块
        （绝大多数）无 cls，取 `sub`——其稳定名 GUID 在 spawn 导出路径里。
        解出的内容：
          - DFMCharacterFSM_Main_C.R_ActiveStates → fsm_states（部分局 wire 无此块）
          - GPFightStateComponent.* → fight
          - GPAttributeSetHealth.* → self.health[actor]（部位血量 + 濒死倒计时，
            真人死亡的实时信号，见 __init__ 注释）
        """
        c2cat = self._cls_to_cat()
        if not c2cat:
            return
        try:
            for x in new_lbs:
                # 含导出的 bunch（hx/hm）：payload 开头是导出数据，块流从
                # parse_prefix 的 ss 起——旧逻辑 tile(0) 从导出开始切=错位，
                # 开局全量装备块（重组超 bunch）全部丢失（44741 局实证）。
                ss = 0
                if x.hx or x.hm:
                    try:
                        bits = np.unpackbits(
                            np.frombuffer(x.payload, dtype=np.uint8),
                            bitorder="little")[:x.pbits]
                        _info, ss = sna.parse_prefix(
                            bits, x.hx, x.hm, quantize_bit=False, frags=x.frags)
                        # 重组 bunch 的导出收集（_accumulate_paths 对这类
                        # bunch 的前缀结构会解析越位）：开局全量的装备组件
                        # 路径只在这里出现，收进 guid_paths 并即时路由，
                        # 否则同 bunch 的装备块 catname=None 被跳过
                        for _e in _info.get("exports", []):
                            _p = _e.get("path")
                            if not _p:
                                continue
                            _g = _e["guid"]
                            if _g not in self.guid_paths or                                     len(_p) > len(self.guid_paths[_g]):
                                self.guid_paths[_g] = _p
                            if _g not in c2cat:
                                _cm = self._tail2catname(_g)
                                if _cm:
                                    c2cat[_g] = _cm
                    except Exception:
                        ss = 0
                # partial_ok:reasm 重组的超 bunch 尾部有对齐残留，严格模式
                # 整批 return None 丢掉全员装备组件块；已切出的前缀块本身
                # 位流有效（每步过 handle 校验，不猜位宽）
                actor = (actor_by_lb.get(id(x)) if actor_by_lb is not None
                         else self._fsm_ch_actor.get(x.ch))
                if actor is None:
                    continue
                blk = tile(x.payload, x.pbits, ss, partial_ok=True)
                if not blk:
                    continue

                def _merge_equip_scan(kk):
                    # 0830 三轮：结构判定装备数组提取（tile 错归属/无路由块
                    # 的兜底；元素布局实证见 _scan_equip_arrays 注释）
                    if kk.get("payload_end", 0) - kk.get("payload_start", 0)                             < 250:
                        return
                    try:
                        _els = _scan_equip_arrays(
                            x.payload, kk["payload_start"],
                            kk["payload_end"], cache=self._equip_scan_cache)
                    except Exception:
                        return
                    if not _els:
                        return
                    _erec = self.equip.setdefault(
                        actor, {"armor": {}, "items": {}})
                    for _el in _els:
                        _f = _erec["items"].setdefault(
                            _el["uid"], {"seq": x.seq})
                        _f["seq"] = x.seq
                        _f["item_id"] = _el["item_id"]
                        if _el.get("dur") is not None:
                            _f["dur"] = float(_el["dur"])
                        if _el.get("dur_max") is not None:
                            _f["dur_max"] = float(_el["dur_max"])

                rec = None
                for k in blk:
                    if k.get("deleted"):
                        continue
                    if k.get("is_actor"):
                        # 0831 闭合：actor 块 payload 也可能被 tile 错位吞进
                        # 装备数组（ch46 seq5773 实证），严格验证提取无害
                        _merge_equip_scan(k)
                        continue
                    cg = k.get("sub") if k.get("stably") else k.get("cls")
                    catname = c2cat.get(cg)
                    if catname == "WeaponReplicateComponent":
                        # Handled by WeaponTracker with exact bunch framing,
                        # array lengths and the custom FastArray fields.
                        continue
                    if not catname:
                        import os as _os
                        if _os.environ.get("GEAR_DBG") and k.get("payload_end", 0) - k.get("payload_start", 0) > 400:
                            print("DBG 未知路由块 g=%s pbits区间=%d..%d" % (
                                cg, k.get("payload_start"), k.get("payload_end")))
                        _merge_equip_scan(k)
                        continue
                    cat = self._subcats.get(catname)
                    if not cat:
                        _merge_equip_scan(k)
                        continue
                    try:
                        ev, _pos, status = repdec6.decode_block(
                            x.payload, k, cat, {})
                    except Exception:
                        continue
                    if not ev:
                        _merge_equip_scan(k)
                    if status not in ("exact", "short"):
                        # 0830：装备组件放宽——error/leftover 块的已解 ev 是
                        # 位流合法的（顺序解码，抛错点之前每步过 handle 校验，
                        # 方法论"不猜位宽"的正交收益）。44741 局实证：开局
                        # 全量 itemId 大半落在 leftover 块里被整块丢弃，
                        # 14 人头甲盲。其它组件维持原门控。
                        if catname not in ("SOLCharacterEquipComponent",
                                           "WeaponReplicateComponent",
                                           "MultiWeaponManagerComponent"):
                            continue
                        if not ev:
                            continue
                    par = {p["index"]: p["name"]
                           for p in cat.get("parents", [])}
                    if rec is None:
                        rec = self.fsm_state.setdefault(
                            actor, {"fsm_states": [], "fight": {}})
                    for e in ev:
                        nm = par.get(e["parent_index"], "?")
                        if catname == "DFMCharacterFSM_Main_C" and nm == "R_ActiveStates":
                            # 保留最近最多 8 个活动状态（slice 末尾为当前）
                            # R_ActiveStates contains FGuid words, not enum ordinals.
                            lst = rec.setdefault("raw_state_guid_words", [])
                            lst.append(e["value"])
                            del lst[:-32]
                        elif catname == "GPFightStateComponent":
                            rec["fight"][nm] = e["value"]
                        elif catname == "DFMCharacterFashionComponent":
                            # 0904 起 currentCharacterHeroID 从 PS h137 移到
                            # 角色时装组件复制（11 条 0904 流实证）；旧流量
                            # 该组件同样在复制，这里统一入库，PS h137 兜底。
                            if nm == "currentCharacterHeroID":
                                _v = e["value"]
                                if isinstance(_v, int) \
                                        and 88000000001 <= _v <= 88000000099:
                                    _lk = self.links.get(actor)
                                    if _lk is not None and _lk.get("hero") is None:
                                        _lk["hero"] = _v
                        elif catname == "GPAttributeSetHealth":
                            # Health/MaxHealth-style attributes have two float
                            # members; IsAlive and ImpendingDeathNum are also
                            # single semantic fields in the same catalog.
                            if current_health_event(e, cat):
                                self._record_health(actor, nm, e["value"], x.seq)
                        # ---- 护甲装备（头甲/防弹衣等级与耐久）----
                        # SOLCharacterEquipComponent：repdec6 数组展开后
                        # e["handle"] 是 wire 绝对 handle（随块内前置 cmd
                        # 浮动），不能按固定值映射；改用「cmd_index 定位父数组
                        # + 元素内类型序」：EquipmentInfoArray 首个 u32=int
                        # itemId、float 对=耐久/上限；EquipedArmorInfoArray
                        # 首个 float=护甲耐久（6870 实测 66→52→38→9→0 掉甲）。
                        elif catname == "SOLCharacterEquipComponent":
                            import os as _os2
                            if _os2.environ.get("GEAR_DBG"):
                                print("DBG 装备块命中 ch=%s seq=%s" % (x.ch, x.seq))
                            erec = self.equip.setdefault(
                                actor, {"armor": {}, "items": {}})
                            pth = e["path"]
                            if not pth:
                                continue
                            ei = pth[-1][1]
                            cmd_idx = e.get("cmd_index")
                            slot = ("armor"
                                    if (cmd_idx is not None and 13 <= cmd_idx <= 30)
                                    else "items")
                            f = erec[slot].setdefault(ei, {"seq": x.seq})
                            f["seq"] = x.seq
                            v = e["value"]
                            tname = e.get("tname")
                            if slot == "items":
                                if tname == "UInt64" and isinstance(v, int) \
                                        and 10 ** 10 <= v < 10 ** 12:
                                    f["item_id"] = int(v)   # 11 位物品 ID
                                elif tname == "Float" and isinstance(v, float):
                                    f["dur"] = float(v)
                            else:
                                if tname == "UInt64" and isinstance(v, int) \
                                        and 10 ** 10 <= v < 10 ** 12:
                                    f["item_id"] = int(v)
                                elif tname == "Float" and isinstance(v, float) \
                                        and "dur" not in f:
                                    f["dur"] = float(v)
                        # ---- 武器 ID 列表 ----
                        # MultiWeaponManagerComponent.HadFirstEquipWeaponID /
                        # WeaponIDList：u64 完整武器模板 ID（18xx=枪械段）。
                        elif catname == "MultiWeaponManagerComponent":
                            wrec = self.weapons.setdefault(actor, {})
                            nm2 = par.get(e["parent_index"], "?")
                            if nm2 == "HadFirstEquipWeaponID":
                                wrec["first"] = int(e["value"])
                            elif nm2 in ("WeaponIDList", "WeaponIdentityList"):
                                lst2 = wrec.setdefault(
                                    "ids", [])
                                iv = e["value"]
                                if isinstance(iv, int) and iv > 0:
                                    lst2.append((x.seq, int(iv)))
                                del lst2[:-16]
                # 0831 闭合：块间隙扫描——tile 错位丢失的装备数组常落在
                # 相邻块之间或尾部残留区（ch46/琅琊缘型），严格验证提取。
                try:
                    _last = ss
                    _gaps = []
                    for _k in blk:
                        if _k.get("deleted"):
                            continue
                        _s0, _e0 = _k["payload_start"], _k["payload_end"]
                        if _s0 > _last + 250:
                            _gaps.append((_last, _s0))
                        _last = max(_last, _e0)
                    if x.pbits - _last > 250:
                        _gaps.append((_last, x.pbits))
                    for (_g0, _g1) in _gaps:
                        _merge_equip_scan({"payload_start": _g0,
                                           "payload_end": _g1})
                except Exception:
                    pass
        except Exception:
            pass


    # ---- join / self / ents（廉价重算）----
    @staticmethod
    def _is_human(lk, players):
        """链接是否真人玩家：PS 解析出 bot=None 且非 AI 类/无 ai_key。
        gone(3) 保留判定用——AI 的 Relevancy 无情报价值，真人的有。"""
        if not lk:
            return False
        if lk.get("ai_key") is not None or "AI" in (lk.get("cls") or ""):
            return False
        pg = lk.get("ps_guid")
        if pg is not None and pg in players:
            return players[pg].get("bot") is None
        ow = lk.get("owner")
        if ow is not None:
            for p in players.values():
                if p.get("owner_guid") == ow:
                    return p.get("bot") is None
        return False

    def _bind_local_character(self):
        # NetDriver's local controller / pawn channels are also
        # the route used by the existing c2s ch3 movement decoder. Require all
        # three identities; the owned PlayerState channel can vary per session.
        channels = self.team_tracker.channels
        owner = channels.get(2)
        actor = channels.get(3)
        if owner is None or actor is None:
            return
        candidates = [(g, p) for g, p in self.players.items()
                      if p.get("owner_guid") == owner
                      and self.ps_ch_actor.get(p.get("channel")) == g
                      and channels.get(p.get("channel")) == g]
        if len(candidates) != 1:
            return
        guid, player = candidates[0]
        existing = self.links.get(actor)
        if existing and existing.get("ps_guid") not in (None, guid):
            return
        spawn = getattr(self.team_tracker, "local_spawn", None)
        if existing is None and spawn is not None:
            self._handle_char_lb(spawn)
            existing = self.links.get(actor)
        if existing is None:
            sequences = self.team_tracker.actors.get(actor, {}).get("seq", {})
            existing = {"owner": owner, "ps_guid": guid,
                        "spawn_seq": min(sequences.values(), default=player.get("spawn_seq", 0)),
                        "loc": None, "num_exports": None, "ai_key": None}
            self.links[actor] = existing
        existing.update(owner=owner, ps_guid=guid, local_owner=True)
        self.ch_actor[3] = actor
        self._fsm_ch_actor[3] = actor

    def _derive(self):
        self._bind_local_character()
        slots = self.slots
        explicit = nl.explicit_slot_claims(self.links)
        reserved = nl.observed_move_slots(self.links)
        claim = {slot: (actor, 0.0) for slot, actor in explicit.items()}
        # owner_guid -> [ps_guid,...]（插入序）：named_actors/linked_ps/ents 命名
        # /hero_id 四处原本各自 O(links×players) 全量嵌套扫描，一次建好复用
        owner2guid = {}
        for _g, _p in self.players.items():
            _ow = _p.get("owner_guid")
            if _ow is not None:
                owner2guid.setdefault(_ow, []).append(_g)
        named_actors = set()
        for actor, lk in self.links.items():
            if (lk.get("ps_guid") is not None and lk["ps_guid"] in self.players) or \
               (lk.get("owner") is not None and lk["owner"] in owner2guid):
                named_actors.add(actor)
        for actor, lk in self.links.items():
            # 身份未解析的链接（PVE AI 子类：无 PlayerState 引用）不参与槽位
            # 认领——它们会抢占队友/self 槽位，破坏闭世界判定。
            # 注意：不能跳过 gone 角色来「释放」槽位——实测重生型角色（通道拆除
            # 重建，2490 局 v1@184->v2@197）出生点与前任重合，会抢回前任的死亡
            # 废槽；gone 角色的槽位压制改由快照层的「复用感知」处理（见 snapshot）。
            if "move_handle" in lk or actor not in named_actors:
                continue
            loc, S = lk.get("loc"), lk.get("spawn_seq")
            if not loc or S is None or not slots:
                continue
            g = self.gone.get(actor)
            best = None; bd = 1e18; bpts = 0
            lx, ly, lz = loc[0], loc[1], loc[2]
            for slot, tr in slots.items():
                if slot in reserved:
                    continue
                if not tr:
                    continue
                if g is not None and tr[0][0] > g[0]:
                    # 角色 close 早于该槽首点 = 角色从未在此槽上活动，
                    # 认领无效（2426 局实证：actor366 v1 Destroyed@163，slot18
                    # 首点@169——v1 的认领压制了同主活角色 v2(3874)，slot18
                    # 实体整个消失。与 2490 局「重生角色抢死亡废槽」不冲突：
                    # 那边槽首点早于 close，角色真在该槽上待过）。
                    continue
                j = bisect.bisect_left(tr, S, key=_SAMPLE_SEQ)
                for jj in (j - 1, j):
                    if 0 <= jj < len(tr):
                        t = tr[jj]
                        dx = lx - t[2]; dy = ly - t[3]; dz = lz - t[4]
                        d2 = dx * dx + dy * dy + dz * dz
                        # 平局打破（2426 局实证）：出生点 z 偏移使多个槽距离精确
                        # 相等（86.0），严格小于按迭代序让先到者占坑——两个重复
                        # PS 的角色全挤进只有 2 个采样点的废槽，真正的成员槽
                        # (7070 点) 无人认领 → 匿名命名丢队伍。平距时采样多者胜。
                        # （平方距离单调，比较/平局语义与开方一致）
                        if d2 < bd or (d2 == bd and len(tr) > bpts):
                            bd, best, bpts = d2, slot, len(tr)
            if best is not None and bd < 250000.0:   # 500cm²
                prev = claim.get(best)
                if prev is None or bd < prev[1]:
                    claim[best] = (actor, bd)
        members = {s for s, tr in slots.items() if len(tr) >= MEMBER_MIN_POINTS}
        unclaimed = [s for s in members if s not in claim]
        self_slot = None
        # Old short-lived slots are not members. Counting their claims makes
        # an otherwise complete member assignment fail forever (AZ self case).
        member_claims = {s: rec for s, rec in claim.items() if s in members}
        claimed_actors = {a for a, _d in member_claims.values()}
        if len(unclaimed) == 1 and claimed_actors and claimed_actors <= named_actors \
                and len(member_claims) == len(members) - 1:
            self_slot = unclaimed[0]
        # 确定性 self_slot（不依赖闭世界）：field13 小队广播的"我"轨迹对齐
        # 最近成员槽。实测 flow1368 全程亚米级（4cm）。仅在闭世界失效时启用，
        # 与既有金标准路径互不干扰。
        if self_slot is None and self.f13_self is not None:
            fseq, _ts, floc = self.f13_self
            fx, fy = floc[0], floc[1]
            best = None; bd = 1e18
            for slot, tr in slots.items():
                if len(tr) < MEMBER_MIN_POINTS:
                    continue
                # 已被别的角色认领的槽不能当自机槽 —— 那是别人（实测：队友）的位置。
                # 闭世界那条路只从"无人认领"的槽里选，这里必须同样过滤，否则数据变稀时
                # 会把自机钉到队友槽上（"自己贴在队友身上"）。
                if slot in claim:
                    continue
                j = bisect.bisect_left(tr, fseq, key=_SAMPLE_SEQ)
                for jj in (j - 1, j):
                    if 0 <= jj < len(tr):
                        t = tr[jj]
                        dx = fx - t[2]; dy = fy - t[3]
                        d2 = dx * dx + dy * dy
                        if d2 < bd:
                            bd, best = d2, slot
            if best is not None and bd < 90000.0:   # 300cm²
                self_slot = best
        # 队友槽位（协议级定版）：field13 非自机轨迹 <-> 成员槽最近点对齐。
        # 用户语义确认：MRA 槽位广播覆盖所有被追踪角色（敌人真人/敌方 AI 都在），
        # 槽位与 AI 标记均不代表队友；唯一可靠的队友信号 = field13 小队广播。
        # 阵亡队友轨迹冻结在其最后位置、对应槽同步静默，历史点仍能对上。
        self.mate_slots = {}
        if self.f13_tracks and slots:
            _mates = []
            for ti, tr in self.f13_tracks.items():
                if ti == self.f13_self_idx or not tr:
                    continue
                fseq, _ts, fx, fy, fz = tr[-1]
                best = None; bd = 1e18
                for slot, mtr in slots.items():
                    if len(mtr) < MEMBER_MIN_POINTS:
                        continue
                    j = bisect.bisect_left(mtr, fseq, key=_SAMPLE_SEQ)
                    for jj in (j - 1, j):
                        if 0 <= jj < len(mtr):
                            t = mtr[jj]
                            dx = fx - t[2]; dy = fy - t[3]
                            d2 = dx * dx + dy * dy
                            if d2 < bd:
                                bd, best = d2, slot
                if best is not None and bd < 90000.0:   # 300cm²
                    _mates.append((bd, best, ti))
            for bd, slot, ti in sorted(_mates):   # 距离最近者优先占槽
                self.mate_slots[slot] = ti
            # 槽仍活着但认领它的角色是「离开更新距离」(Relevancy)：槽已被新主人
            # 复用（服务器对 Relevancy 角色销毁槽后重新分配）。把旧映射从 mate
            # 关系里剔除，避免新主人顶着阵亡队友的名字显示（实测 495 slot4
            # Relevancy@34836 后槽又活了 4000+ seq 且仍在 field13 轨迹上，但已
            # 不是原队友）。
            _actor_of_slot = {}
            for _s, (_a, _d) in claim.items():
                _actor_of_slot[_s] = _a
            for s in list(self.mate_slots):
                a = _actor_of_slot.get(s)
                if a is None:
                    continue
                g = self.gone.get(a)
                tr = self.slots.get(s) or []
                if g is not None and g[1] == 3 and tr and tr[-1][0] > g[0] \
                        and s not in explicit:
                    del self.mate_slots[s]
        # ---- 权威 self 判定（前移到匿名命名块之前）----
        # 顺序缺陷（875 局实证）：闭世界「唯一未认领槽 = 我」会把中途加入、
        # 角色认领失败的真人槽（slot21 心安฿）误标为 self 候选并加进
        # claimed_slots，匿名命名块随后把它排除出候选 → 永远「未知玩家」。
        # 而 self_ps_guid（ch 最小真人）只依赖 self.players/links，可先于命名
        # 确定权威 self_slot，闭世界/f13 只是兜底。
        # 自己的显示名：UE4 握手时服务器先复制本连接自己的 PlayerState，
        # 通道按创建顺序分配 -> 自己的 PS 通道必为真人 PS 中最低（实测 495/733/988
        # 三局一致：ch=4/8/8，而 pid 最小规则在其中两局标错）。
        self_name = None
        self_ps_guid = None
        self_src = None
        # 真人候选（有名字、非 bot）。**不能**因为 channel 为空就把记录跳过——那正是
        # "自己"被踢出候选、退而选中下一个人的原因（实测：正确 guid452/ch4 被换成
        # guid4506/ch15）。
        _humans = [(g, p) for g, p in self.players.items()
                   if p.get("bot") is None and p.get("name")]
        if _humans and all(p.get("channel") is not None for _g, p in _humans):
            _g, p = min(_humans, key=lambda kv: kv[1]["channel"])
            self_name, self_ps_guid, self_src = p["name"], _g, "ch"
        elif _humans:
            # 有记录的 channel 缺失 → 改用"最早 spawn"：自己的 PS 必是服务器最先
            # 复制的那条（实测 guid452 spawn_seq=77，其余 142+）。
            def _early(kv):
                p = kv[1]
                return (p.get("spawn_seq") if p.get("spawn_seq") is not None else 1 << 30,
                        p.get("channel") if p.get("channel") is not None else 1 << 30)
            _g, p = min(_humans, key=_early)
            self_name, self_ps_guid, self_src = p["name"], _g, "spawn"
        if self_name is None:   # 回退：pid 最小真人（旧规则）
            for _g, p in sorted(self.players.items(), key=lambda kv: kv[1].get("player_id") or 1 << 30):
                if p.get("bot") is None and p.get("name"):
                    self_name, self_ps_guid, self_src = p["name"], _g, "pid"
                    break
        # ---- 权威自机身份：优先用**上行自机坐标**（客户端自己报的位置）----
        # 下行没有可靠的"我是谁"标记，所以自机名一直是启发式（真人 PS 里通道最小）；
        # live 增量一旦把你自己那条 PS 记录弄丢/弄成无名，启发式就退而选中同局另一个
        # 人（实测：正确的 guid4290/ch11 被换成 guid5362/ch15 的队友），而 self_slot
        # 又是从 self_ps_guid 反查的 —— 于是名字和位置一起错。
        # 上行 ch3 的 loc 是客户端自报世界坐标：实测与自机槽差 103cm，次近的 1167cm，
        # 区分度足够。用它钉住自机槽 -> 该槽认领的 actor -> ps_guid，名字与 self_slot
        # 都从这里出发，比启发式硬。**但位姿必须无歧义**（自己和队友贴身时最近邻会
        # 认错人，见 _slot_near_pose 的三道闸）——有歧义/位姿陈旧时返回 None，
        # self_name 保持上面的 PS 通道序启发式结果（身份权威口径不变）。
        pose = getattr(self.loader, "self_pose", None)
        if pose is not None:
            _ps = self._slot_near_pose(pose.get("loc"), pose_seq=pose.get("seq"))
            if _ps is not None:
                # 位置权威：无歧义的位姿槽直接钉住（即使该槽暂时没有 PS 名，
                # 位置也比闭世界/f13 几何兜底准）。
                self._self_slot_pose = _ps
                _pa = claim.get(_ps, (None, 0.0))[0]
                _pg = (self.links.get(_pa) or {}).get("ps_guid") if _pa is not None else None
                _pp = self.players.get(_pg) if _pg is not None else None
                if isinstance(_pp, dict) and _pp.get("name") and _pp.get("bot") is None:
                    self_name, self_ps_guid = _pp["name"], _pg
                    self_src = "pose"
        actor2slot = {a: s for s, (a, _d) in claim.items()}
        # self_slot 覆盖（确定性，优先于闭世界/field13 兜底）：
        # hx=1 解码修复后，本地玩家的 Character 也带 loc 并认领自己的槽位，
        # 闭世界判定「未被认领的槽 = 我」的前提已失效（flow 1643：稳健医疗亡命峰
        # 的 char 认领 slot17，闭世界+field13 却把匿名 slot18 误判为 self）。
        # 用 self_ps_guid 反查 Character -> 认领的槽，直接钉死 self_slot。
        # 上行坐标钉住的槽更硬（不依赖 PS 记录是否完好），优先用它。
        self_slot = getattr(self, "_self_slot_pose", None) or self_slot
        if getattr(self, "_self_slot_pose", None) is None and self_ps_guid is not None:
            for _a, _lk in self.links.items():
                if _lk.get("ps_guid") == self_ps_guid and _a in actor2slot:
                    self_slot = actor2slot[_a]
                    break
        # 匿名成员槽命名（确定性，协议级）：Character 关联不上槽位有两种实测
        # 失败模式 —— (a) 开局 hx=1 角色内容块 tile 解析失败，h14/h18 无法解码；
        # (b) 角色通道被服务器拆除重建（2490 局：v1 Destroyed@184/710 -> v2 立即
        # 重生），二代角色的 PS spawn_seq 早于其新槽首点（slot11 首点 189 vs
        # PS@197、slot16 首点 714 vs PS@717，实测差 3~8 seq）。两种都表现为
        # 「PS 有名字、槽位无认领」，用「槽首点 seq <-> PS spawn seq」对齐命名。
        # 池含未链接 PS **和有链接但全部角色未认领槽的 PS**；窗口 ±10（实测 d=8），
        # 一对一消耗。金标准 495 全关联，此路径不触发，零回归。
        self.anon_names = {}
        if self.players:
            # claim 值是 (actor, 距离) 元组（2426 平局打破形），必须解包取槽号。
            # 曾直接 set(claim.values()) → 元组集合，「槽 in claimed_slots」永不
            # 命中 → 全部成员槽都成了匿名候选：第二梯队唯一配对恒失效（875 局
            # 心安฿ 显示未知玩家），第一梯队 ±10 精对齐还会在全槽上冒误配风险。
            claimed_slots = {s for s, (_a, _d) in claim.items()}
            # gone 角色认领的槽：Relevancy/LevelUnloading 关闭 = 角色被相关性
            # 剔除但同一主人仍在槽上广播（1368 局 slot9），首点对齐命名仍指向
            # 原主 -> 参与命名；Destroyed/TearOff = 死亡，槽可能被新人复用，
            # 首点命名会张冠李戴 -> 不参与。
            # 「仍在槽上广播」须实证：槽在 close 之后**有新采样**才 discard（=
            # 槽真被复用/同主续播，需要重新命名）。close 后槽就静默的（6335 局
            # slot21：last_seq 31491 < close 31642，每1把出红离开后再无广播），
            # 槽不会再有新主人，也不该进 anon 候选池与真新槽（如 34）竞争——
            # 6335 实证：两个候选槽让「唯一空闲 PS × 唯一 anon 槽」配对失效。
            for _s, (_a, _d) in claim.items():
                _gr = self.gone.get(_a)
                if _gr is not None and _gr[1] not in (0, 4):
                    _tr = slots.get(_s)
                    if _tr and _tr[-1][0] > _gr[0]:
                        claimed_slots.discard(_s)
            if self_slot is not None:
                claimed_slots.add(self_slot)
            linked_ps = set()
            for _a, _lk in self.links.items():
                _pg = _lk.get("ps_guid")
                if _pg is not None and _pg in self.players:
                    linked_ps.add(_pg)
                _ow = _lk.get("owner")
                if _ow is not None:
                    linked_ps.update(owner2guid.get(_ow, ()))
            claimed_actors = {a for a, _d in claim.values()}

            def _ps_free(g):
                """PS 可参与对齐：无任何角色链接；或有链接但角色全都未认领槽位。"""
                if g not in linked_ps:
                    return True
                return all(a not in claimed_actors
                           for a, lk2 in self.links.items()
                           if lk2.get("ps_guid") == g
                           or (lk2.get("owner") is not None
                               and self.players.get(g, {}).get("owner_guid") == lk2.get("owner")))

            anon_slots = sorted(((slots[s][0][0], s) for s in members
                                 if s not in claimed_slots and slots.get(s)),
                                key=lambda t: t[0])
            ps_pool = sorted(((p.get("spawn_seq"), g) for g, p in self.players.items()
                              if _ps_free(g) and p.get("name")
                              and p.get("spawn_seq") is not None),
                             key=lambda t: t[0])
            # 全局最近对优先的一对一分配（非逐槽贪心）：±10 窗口下多个槽可能
            # 邻近同一 PS，逐槽贪心会让先处理的槽抢走别人 d 更小的 PS
            # （实测 1368：slot9 专名）。按 (d, slot, guid) 排序后依次占用。
            _cand = sorted((abs(sseq - pseq), s, g)
                           for sseq, s in anon_slots for pseq, g in ps_pool
                           if abs(sseq - pseq) < 11)
            _used_s, _used_g = set(), set()
            for d, s, g in _cand:
                if s in _used_s or g in _used_g:
                    continue
                self.anon_names[s] = self.players[g]
                _used_s.add(s); _used_g.add(g)
            # 第三梯队（**player_id ↔ 槽号同序**，确定性，强于 seq 对齐）：服务器给
            # PlayerState 的 PlayerId 与 MRA 槽号同出一套分配序。**但只是局部同序**：
            # 实测 115718 局 10/10 个认领槽 (pid - slot) 恒为 242（据此精确补出
            # slot23 -> 祺捣）；而 215035 局会漂移（257->9=248、259->11=248、
            # 261->15=246、262->17=245、263->18=245）——因为中间夹了无身份实体的槽。
            # 所以只在**高度一致**时才采信（见下面门槛），有漂移就整条放弃：宁可
            # 留「未知玩家」也不能补错名（多对多不猜，与既有定版一致）。
            _pidoff = {}
            _pidoff_tot = 0
            for _s2, (_a, _d3) in claim.items():
                _pg2 = (self.links.get(_a) or {}).get("ps_guid")
                _p2 = self.players.get(_pg2) if _pg2 is not None else None
                _pid2 = _p2.get("player_id") if _p2 is not None else None
                # bot 的 PlayerId 自成一个 8000+ 编号段，与真人槽序无关，不参与推偏移
                if _pid2 is None or _p2.get("bot") is not None or _pid2 >= 8000:
                    continue
                _pidoff[_pid2 - _s2] = _pidoff.get(_pid2 - _s2, 0) + 1
                _pidoff_tot += 1
            if _pidoff:
                _off_v, _off_n = max(_pidoff.items(), key=lambda kv: kv[1])
                _off_2nd = max((v for k, v in _pidoff.items() if k != _off_v), default=0)
                # 采信门槛：≥4 个已知槽一致、覆盖 ≥80% 的带 pid 槽、且明显压过次强
                # 偏移（有漂移/两套偏移 = 数据不可靠，不猜）。
                if (_off_n >= 4 and _off_n >= _pidoff_tot * 0.8
                        and _off_n > _off_2nd * 3):
                    _pid_used = set()
                    _claimed_uins3 = set()
                    for _s2, (_a, _d3) in claim.items():
                        _pg2 = (self.links.get(_a) or {}).get("ps_guid")
                        _p2 = self.players.get(_pg2) if _pg2 is not None else None
                        if _p2 is None:
                            continue
                        if _p2.get("player_id") is not None:
                            _pid_used.add(_p2["player_id"])
                        if _p2.get("uin") is not None:
                            _claimed_uins3.add(_p2["uin"])
                    _pid_free = {}
                    for _g3, _p3 in self.players.items():
                        _pid3 = _p3.get("player_id")
                        if (_pid3 is None or _pid3 in _pid_used or not _p3.get("name")
                                or _p3.get("bot") is not None):
                            continue
                        _u3 = _p3.get("uin")
                        if _u3 is not None and _u3 in _claimed_uins3:
                            continue    # 同账号旧 PS 副本
                        if not _ps_free(_g3):
                            continue    # 已挂到别的槽上的角色，不重复命名
                        _pid_free[_pid3] = _g3
                    for _fs, _s2 in anon_slots:
                        if _s2 in _used_s:
                            continue
                        _g3 = _pid_free.get(_s2 + _off_v)
                        if _g3 is not None:
                            self.anon_names[_s2] = self.players[_g3]
                            _used_s.add(_s2); _used_g.add(_g3)
            # 第二梯队（用户定版 0827「缓存所有PS避免名字挂不上」）：精对齐失败的
            # 剩余 anon 槽 × 真空闲 PS 唯一配对。实测 6335 局：泡小蛋 PS@122、
            # 中途落地槽 34 首点@29330，seq 差 29k 精对齐必败；但同一账号会因
            # 重进产生多份 PS（岸勾 2108/8172、国一 2116/7852），**uin 相同**，
            # uin 已被 claimed PS 占用的 = 旧副本，从空闲池排除后泡小蛋是唯一
            # 真空闲 PS × 唯一 anon 槽 → 唯一配对可命名。多对多不猜（赌错比
            # 不显示更糟，与 1368 局身份冲突定版一致）。
            _claimed_uins = set()
            # claim 形状 = slot -> (actor, 距离)：这里要的是 actor（元组里解包），
            # 曾写成 for _a, _s2 in claim.items() 把槽号当 actor 查 links，
            # _claimed_uins 永远近乎为空 → 同账号旧 PS 副本排除失效（875 局
            # LAOZHOUZZZ/欧美野猪君新旧两份 PS 都留在空闲池 → 唯一配对恒失效）。
            for _s2, (_a, _d3) in claim.items():
                _lk2 = self.links.get(_a) or {}
                _pg2 = _lk2.get("ps_guid")
                if _pg2 is not None:
                    _p2 = self.players.get(_pg2)
                    if _p2 is not None and _p2.get("uin") is not None:
                        _claimed_uins.add(_p2["uin"])
                _ow2 = _lk2.get("owner")
                if _ow2 is not None:
                    for _p2 in self.players.values():
                        if _p2.get("owner_guid") == _ow2 and _p2.get("uin") is not None:
                            _claimed_uins.add(_p2["uin"])
            _free2 = []
            for pseq, g in ps_pool:
                if g in _used_g:
                    continue
                _p = self.players.get(g) or {}
                _u = _p.get("uin")
                if _u is not None and _u in _claimed_uins:
                    continue    # 同账号旧 PS 副本（已被 claimed PS 占用）
                _free2.append(g)
            _rest_slots = [s for _fs, s in anon_slots if s not in _used_s]
            if len(_free2) == 1 and len(_rest_slots) == 1:
                self.anon_names[_rest_slots[0]] = self.players[_free2[0]]
        # （self_name/self_ps_guid/actor2slot/self_slot 权威判定已前移至
        #   匿名命名块之前——闭世界 self 候选会污染匿名候选池，875 局实证）
        # Legacy diagnostic set only; a zero vital part is not confirmed death.
        # 6870 局实证与死亡盒 seq 吻合（1560: Abdomen 0@9037 vs 盒@9336）。
        # ImpendingDeathHealth 是濒死（倒地流血）非死亡，不在此列——
        # 濒死会被救起，判死会误杀。四肢归零只是残废不致死（2286 实测活着）。
        # **倒地→被救诈尸修复（5130 局 460 波风黄宇健实证）**：致命部位归零
        # 只是「倒地流血」，队友拉起后部位血量会回升（0→30）——此时人还活着。
        # vital_dead 只统计「归零后**未回升**」的部位；回升过一次的部位再归零
        # 重新视为死亡信号（第二次倒地/真死）。金标准 4330/3302/3762 复核：
        # 真死者部位恒 0 无回升，不受影响。
        self.vital_dead = set()
        # vital_revived **持续集合**（不在每轮 derive 重置）：_seen 在回升时被
        # discard，下一轮 derive 不再重复 add——若这里清空就会丢掉复活记录
        # （5130 局 178.8s 有 460、182.4s 被清空的实证）。
        for _ha, _hrec in self.health.items():
            _p = _hrec["parts"]
            _seen = self._vital_zero_seen.setdefault(_ha, set())
            for _k in ("HeadHealth", "ThoraxHealth", "AbdomenHealth"):
                _v = _p.get(_k)
                if _v is None:
                    continue
                if _v <= 0.0:
                    _seen.add(_k)          # 归零：记录（倒地流血，可能被救）
                elif _k in _seen:
                    _seen.discard(_k)      # 回升：被救起
                    self.vital_revived.add(_ha)
            # 有未回升的归零部位才判死；曾归零但全部回升 = 活着（移出 vital_dead）
            if any(_p.get(k) is not None and _p.get(k) <= 0.0
                   for k in ("HeadHealth", "ThoraxHealth", "AbdomenHealth")):
                self.vital_dead.add(_ha)
            else:
                self.vital_dead.discard(_ha)
        # 死亡盒 owner 死亡信号（补齐 close_reason 丢失的真人死亡）：
        # 真人死亡不派发 TearOff/Destroyed close（实测 6870 局 6 名阵亡玩家全部
        # gone=None），但会 spawn 死亡盒（DeadBody），其 handle14 Owner = 死者
        # Character，盒子位置 ≈ 死者最终位置（实测 0~124cm）。用「owner 解析到
        # 角色 + 盒子贴近其槽位最终采样(<300cm)」作为死亡信号。几何门控是防误判
        # 的关键：owner 字段偶有杂音（指到活人/舔包者），只有盒子贴着角色最终
        # 落点才采信。self 也在此表内（自己的死亡盒），但 self 永久豁免移除。
        self.box_dead = {}
        for ba, bx in self.boxes.items():
            oc = self._confirmed_box_owner(bx, actor2slot)
            if oc is not None:
                bx["confirmed_owner"] = oc
                seq = bx.get("spawn_seq", bx["seq"])
                self.box_dead[oc] = max(seq, self.box_dead.get(oc, -1))
        # self_slot 覆盖（确定性，优先于闭世界/field13 兜底）：
        # hx=1 解码修复后，本地玩家的 Character 也带 loc 并认领自己的槽位，
        # 闭世界判定「未被认领的槽 = 我」的前提已失效（flow 1643：稳健医疗亡命峰
        # 的 char 认领 slot17，闭世界+field13 却把匿名 slot18 误判为 self）。
        # 用 self_ps_guid 反查 Character -> 认领的槽，直接钉死 self_slot。
        # 上行坐标钉住的槽更硬（不依赖 PS 记录是否完好），优先用它。
        self_slot = getattr(self, "_self_slot_pose", None) or self_slot
        if getattr(self, "_self_slot_pose", None) is None and self_ps_guid is not None:
            for _a, _lk in self.links.items():
                if _lk.get("ps_guid") == self_ps_guid and _a in actor2slot:
                    self_slot = actor2slot[_a]
                    break
        # Rebuild all assignments from actual squad IDs. f13 proximity and
        # shared PlayerState factions (100/101) cannot establish friendship.
        self.self_team, self.self_team_source, self.slot_teams, self.mate_slots = resolve_squad(
            self.players, self.links, actor2slot, self_ps_guid, self_slot,
            self.team_tracker, self._actor_removed)
        ents = []
        for actor, lk in self.links.items():
            if isinstance(lk.get("move_handle"), int) and lk["move_handle"] >= 0 \
                    and actor not in actor2slot:
                continue  # an explicit later assignment displaced this actor
            # self 判定：
            # 1) 认领了 self_slot 的角色（hx=1 解码修复后，本地玩家的 Character 也带
            #    loc 并认领自己的槽位，旧「loc=None」标记不再出现）
            # 2) 旧判定：loc=None + num_exports=28（自机 spawn 无 loc 标记，兜底）
            claimed_slot = actor2slot.get(actor)
            is_self = (claimed_slot is not None and self_slot is not None
                       and claimed_slot == self_slot) or \
                      (lk.get("loc") is None and lk.get("num_exports") == 28) or bool(lk.get("local_owner"))
            # 角色已消失（close_reason ∈ {0,2,3,4}）→ 不进入实体列表。
            # 但 self 永久在场；**队友槽的 gone 角色保留**（阵亡队友 ☠ + 最后已知
            # 位置，1368 局 slot9）；**血量/死亡盒确认死亡的也保留**——真人死亡不走
            # close，此处若剔除会跌进匿名兜底丢名字（实测「痛的时候喊继续」变未知
            # 玩家）。他们经 dead_map 标 dead=True 上屏为尸体点。
            # **Relevancy(3) 真人也保留**：Relevancy = 离开更新距离、角色仍活着
            # （结算阶段全员出距离是常态，2426 局 slot18 汤泡腿饭@32470）。剔除后
            # 快照层只能走匿名兜底——名字/队伍全丢。AI 的 Relevancy 照旧剔除
            # （地图 AI 卸载无情报价值）。
            _grec = self.gone.get(actor)
            if actor in self.gone and not is_self \
                    and actor2slot.get(actor) not in self.mate_slots:
                # Relevancy is an update-range boundary for every character,
                # including unbound AI. It does not establish destruction.
                if not (_grec is not None and _grec[1] == 3):
                    continue
            name = pid = bot = None
            team = camp = None
            name_src = None
            player_state = None
            # O(1) 查找替代原 O(players) 全扫：ps_guid 至多命中一个玩家；
            # owner 分支取 owner2guid 插入序首个 = 原字典迭代序首个，等价
            _ps = self.players.get(lk["ps_guid"]) \
                if lk.get("ps_guid") is not None else None
            if _ps is not None:
                name, pid, bot = _ps["name"], _ps.get("player_id"), _ps.get("bot")
                team, camp = _ps.get("team"), _ps.get("camp")
                name_src = "ps"
                player_state = _ps
            if name is None and lk.get("owner") is not None:
                for _g in owner2guid.get(lk["owner"], ()):
                    _p = self.players[_g]
                    name, pid, bot = _p["name"], _p.get("player_id"), _p.get("bot")
                    team, camp = _p.get("team"), _p.get("camp")
                    name_src = "owner"
                    player_state = _p
                    break
            # AI 子类（无 PlayerState 引用）：名字 = 当局 LOC_AICharacterNameTable
            # 解析值；表里没有的 key（静态类型名，客户端走本地资源）用 key 兜底。
            ai_key = lk.get("ai_key")
            is_ai_cls = "AI" in (lk.get("cls") or "")
            if name is None and ai_key is not None:
                # 表里没有的 key 优先按类名给兵种名（"AI·167_Name" 这种占位串
                # 对用户没有意义），仍无解才留 key 兜底
                name = (self.ai_names.get(ai_key) or ai_cls_name(lk.get("cls"))
                        or ("AI·%d" % ai_key))
                name_src = "ai"
            # 0901：ai_key 缺席的 AI 子类（495 局 55 个 AI 里 39 个）过去 name=None
            # -> 快照层「无名标记不上屏」把 PVE AI 整体吞掉。改用类名兵种兜底，
            # 让它们至少能以「AI·士兵/盾兵/火箭兵」上屏（用户报 PVE AI 不显示）。
            if name is None and is_ai_cls:
                name = ai_cls_name(lk.get("cls"))
                if name is not None:
                    name_src = "ai"
            # AI 表名兜底的槽若命中 anon_names 的 PS 名 -> 改用 PS 名（小队
            # 花名册名。1368 局 slot9：角色挂着悬空 ps_guid、PS「蓝Amos」未捕获，
            # 表名「星星藏在云堆」顶掉了真名）
            _aslot = actor2slot.get(actor)
            if name_src == "ai" and _aslot is not None:
                _p2 = self.anon_names.get(_aslot)
                if _p2 is not None:
                    name, pid, bot = _p2["name"], _p2.get("player_id"), _p2.get("bot")
                    team, camp = _p2.get("team"), _p2.get("camp")
                    player_state = _p2
            # 队友槽身份冲突（PS 名 vs anon 对齐名）不做自动覆盖：两候选都是
            # 小队 AI 填充时无法从协议判定真主（1368 局 slot9 双候选），赌错
            # 比显示 PS 解析名更糟。人类队友走 ps 解析不受影响。
            if is_self:
                kind = "self"
            elif ai_key is not None or is_ai_cls:
                # 真 AI：AI 子类（无 PS 引用）或类名含 AI（BP_DFMAICharacter_* 等）
                kind = "ai"
            elif bot is True:
                # 伪真人 bot（有人机 PS、走真人 wire 协议、名字从真人池取）：
                # 用户定版 0828「和真人差不多的人机就当真人对待」→ 敌方玩家待遇
                kind = "player"
            else:
                kind = "player"
            disp = (("%s(self)" % self_name) if self_name else "自己") if is_self else name
            # 服务器会给本机玩家复制一份匿名 PlayerState（team 100/101 共享阵营，
            # 实测 cyyyh12346 局 anon-29 与自机同名同点），它不是敌人，跳过。
            if not is_self and self_name and name == self_name:
                continue
            if disp is None and actor2slot.get(actor) is None and lk.get("loc") is None:
                continue
            _hid = lk.get("hero")   # 0904: 角色 fashion 组件为权威，PS h137 兜底
            if _hid is None and lk.get("ps_guid") is not None and lk["ps_guid"] in self.players:
                _hid = self.players[lk["ps_guid"]].get("hero_id")
            if _hid is None and lk.get("owner") is not None:
                for _g in owner2guid.get(lk["owner"], ()):
                    _hid = self.players[_g].get("hero_id"); break
            component_team = self.team_tracker.current(actor)
            team = squad_id(component_team.get('TeamID')) or squad_id(team)
            camp = component_team.get('CampId', camp)
            ents.append({
                "actor": actor, "name": disp, "kind": kind,
                "binding_source": "move_handle" if actor in explicit.values()
                                  else "spawn_proximity" if actor in actor2slot else None,
                "slot": None if is_self else actor2slot.get(actor),
                "spawn_loc": lk.get("loc"), "spawn_seq": lk.get("spawn_seq"),
                "player_id": pid,
                "team": team, "camp": camp,
                # 伪真人 bot 标记（PS bot=True）：前端「（人机干员）」后缀判定用。
                # 真 AI（ai_key/cls）恒 False——它们不用后缀。
                "is_bot": confirmed_player_bot(player_state, HERO_NAMES.get(_hid) is not None),
                "hero": HERO_NAMES.get(_hid),
            })
        # ---- Relevancy-槽复用的实体名处理：----
        # 槽主人被 close(=3) 踢出后槽仍活：此时槽上的轨迹已是新主人，
        # 但 ents 里仍是旧角色的 PS 名（ps_guid 没变）。这类实体：
        #   - 名字不该再用旧名（实测 495 slot4 actor420 顶着"我没点外脉"继续走）
        #   - 不算 mate（已上面剔除）→ 退化为"player 复用者"显示无名点
        # 标记 is_reused=True，snapshot 据此把 name 覆盖为 None → 上屏未知玩家。
        _cur2 = self.latest_seq() or 0
        for ent in ents:
            s = ent.get("slot")
            if s is None or ent["kind"] in ("self",):
                continue
            a = ent["actor"]
            if a is None:
                continue
            g = self.gone.get(a)
            if g is None or g[1] != 3:
                continue
            if explicit.get(s) == a:
                continue  # Relevancy close does not change MoveHandle ownership
            tr = self.slots.get(s) or []
            if tr and tr[-1][0] > g[0]:
                ent["slot_reused"] = True
                ent["name"] = None          # 不再用旧名
                if ent["kind"] == "mate":
                    ent["kind"] = "player"
        with self.lock:
            self.claim = dict(actor2slot)
            self.self_slot = self_slot
            self.self_name = self_name
            self.self_src = self_src
            self.ents = ents
            self.member_slots = members

    # ---- 快照（tail-follow：游标 = 最新 seq）----
    def _dying_fallback(self, actor, cur_ts):
        state = self._life_state_for_actor(actor)
        return ("倒地", "down") if state["downed"] else (None, None)

    def _fsm(self):
        """读取增量解码的角色状态（dict；绝不阻塞 snapshot）。

        poll() 每轮已把新增子对象块增量解码进 self.fsm_state，snapshot 只读取
        当前值。就绪前（无状态块）为 {}，坐标不受影响。
        """
        return self.fsm_state or {}

    def _equip(self):
        """护甲装备快照：actor -> {"armor": {...}, "items": {...}}（见 __init__）。"""
        return self.equip or {}

    def _weapons(self):
        """武器 ID 快照：actor -> {"first": id, "ids": [...]}（见 __init__）。"""
        return self.weapons or {}

    @staticmethod
    def armor_level(item_id):
        """11 位防具物品 ID -> 等级（1~6）。0/None = 未知。

        ID 段式（item_names_cn v8 实测）：11010003004 = 1101|00|03|004 →
        第 7~8 位 = 等级段（01 新兵 02 标准 03 突击 04 战术 05 重装 06 六级）。
        """
        try:
            s = str(int(item_id))
        except (TypeError, ValueError):
            return 0
        if len(s) == 11 and s.startswith("110"):
            try:
                lv = int(s[6:8])
            except ValueError:
                return 0
            return lv if 1 <= lv <= 6 else 0
        return 0

    def _seqs_of(self, slot, tr):
        """slot 的递增 seq 列表（numpy int32），按 poll 增量追加。

        snapshot 每次 tick 都要对每个 slot 做 bisect；原版每次重建
        [t[0] for t in tr]（O(轨迹长)，轨迹上千条时占满 snapshot 一半时间）。
        slot 轨迹只增不改（仅 _handle_mra_lb append，见 self.slots.setdefault），
        故缓存 numpy 数组、仅在长度变化时增量追加。结果与原 list 完全等价。
        """
        cached = self._seqs_cache.get(slot)
        n = len(tr)
        if cached is not None and cached.shape[0] == n:
            return cached
        if cached is None:
            arr = np.fromiter((t[0] for t in tr), dtype=np.int64, count=n)
        else:
            extra = np.fromiter((tr[i][0] for i in range(cached.shape[0], n)),
                                dtype=np.int64, count=n - cached.shape[0])
            arr = np.concatenate((cached, extra))
        self._seqs_cache[slot] = arr
        return arr

    def latest_seq(self):
        hi = None
        with self.lock:
            for tr in self.slots.values():
                if tr:
                    hi = tr[-1][0] if hi is None else max(hi, tr[-1][0])
        return hi

    def _char_name_kind(self, actor):
        """角色 actor → (name, kind)。kind: player(人类)/ai(AI)。无名返回 (None, kind)。

        与 _derive 的实体命名同源：人类走 PlayerState（ps_guid→players 或
        owner→players），AI 走 ai_key→ai_names。盒子命名复用此判定：
        kind=="player" 才是玩家盒子（有名字的就是玩家）。
        """
        lk = self.links.get(actor)
        if lk is None:
            return None, "unknown"
        name = pid = bot = None
        for g, p in self.players.items():
            if lk.get("ps_guid") is not None and g == lk["ps_guid"]:
                name, pid, bot = p["name"], p.get("player_id"), p.get("bot"); break
        if name is None and lk.get("owner") is not None:
            for g, p in self.players.items():
                if p.get("owner_guid") == lk["owner"]:
                    name, pid, bot = p["name"], p.get("player_id"), p.get("bot"); break
        ai_key = lk.get("ai_key")
        is_ai_cls = "AI" in (lk.get("cls") or "")
        if name is None and ai_key is not None:
            name = self.ai_names.get(ai_key) or ("AI·%d_Name" % ai_key)
        if ai_key is not None or is_ai_cls:
            kind = "ai"
        elif bot is True:
            # 伪真人 bot → 玩家待遇（0828 用户定版，同 _derive）
            kind = "player"
        else:
            kind = "player"
        return name, kind

    def _char_is_bot(self, actor):
        """角色 actor 是否伪真人 bot（PS bot=True）。无 links/无 PS → False。

        盒子命名后缀用：伪真人死者盒子名带「（人机干员）」，真人/真 AI 不带。
        """
        if actor is None:
            return False
        lk = self.links.get(actor)
        if lk is None:
            return False
        pg = lk.get("ps_guid")
        if pg is not None and pg in self.players:
            player = self.players[pg]
            hid = lk.get("hero") or player.get("hero_id")
            return confirmed_player_bot(player, HERO_NAMES.get(hid) is not None)
        ow = lk.get("owner")
        if ow is not None:
            for p in self.players.values():
                if p.get("owner_guid") == ow:
                    hid = lk.get("hero") or p.get("hero_id")
                    return confirmed_player_bot(p, HERO_NAMES.get(hid) is not None)
        return False

    def _box_owner_char(self, box_owner):
        """死亡盒 handle 14(Owner) 死亡者引用 → 最终 Character actor。

        box.handle14(Owner) → 死者 Character 直达（flow 465 实证：Owner=TearOff AI）。
        若 Owner 偶指 CarryBody（背尸盒），穿透 carry_owner 链回到原 Character。
        返回最终 Character actor（在 self.links 里），解析失败返回 None。
        """
        if box_owner is None:
            return None
        resolved = box_owner
        seen = set()
        while resolved in self.carry_owner and resolved not in seen:
            seen.add(resolved)
            resolved = self.carry_owner[resolved]
        if resolved in self.links:
            return resolved
        return None

    def _confirmed_box_owner(self, box, claims=None):
        """Only replicated references identify the dead character, never distance."""
        actor = self._box_owner_char(box.get("deceased_actor") or box.get("owner_actor"))
        if actor is None:
            return None
        if self.links[actor].get("spawn_seq", 0) > box.get("spawn_seq", box["seq"]):
            return None
        return actor

    def _player_life_for_actor(self, actor):
        """Return the PlayerState life record linked to a Character actor."""
        link = self.links.get(actor) or {}
        ps_guid = link.get("ps_guid")
        if ps_guid is not None:
            rec = self.player_state.get(ps_guid)
            if rec is not None:
                return rec
        owner = link.get("owner")
        if owner is None:
            return None
        for ps_guid, player in self.players.items():
            if player.get("owner_guid") != owner:
                continue
            rec = self.player_state.get(ps_guid)
            if rec is not None:
                return rec
        return None

    def _life_state_for_actor(self, actor):
        link = self.links.get(actor) or {}
        if link.get("spawn_seq") is not None:
            self.life_tracker.spawn(actor, link["spawn_seq"])
        life = self._player_life_for_actor(actor)
        if life and life.get("schema_verified"):
            ps = link.get("ps_guid")
            # A PlayerState can survive a replacement Character. Later PS
            # revival belongs to the latest Character linked at that sequence.
            ends = [v.get("spawn_seq", 0) for a, v in self.links.items()
                    if a != actor and ps is not None and v.get("ps_guid") == ps
                    and v.get("spawn_seq", 0) > link.get("spawn_seq", 0)]
            end = min(ends) if ends else float("inf")
            seqs = {k: q for k, q in life.get("seq", {}).items() if q < end}
            self.life_tracker.observe_playerstate(actor, life.get("values", {}), seqs, verified=True)
        return self.life_tracker.current(actor)

    def _actor_removed(self, actor):
        # Departure is a replicated match state, independent of actor close
        # and health. Waiting/ongoing extraction never removes a player.
        life = self._player_life_for_actor(actor)
        if life and life.get("schema_verified"):
            spawn = (self.links.get(actor) or {}).get("spawn_seq", 0)
            values, seqs = life.get("values", {}), life.get("seq", {})
            for name, terminal in (("ExitState", 3), ("bFinishGame", 1),
                                   ("PlayerMatchInfo.bMatchOver", 1)):
                if values.get(name) == terminal and seqs.get(name, -1) >= spawn:
                    return True
        closed = self.gone.get(actor)
        return bool(closed and closed[1] in (0, 2, 4)
                    and closed[0] >= (self.links.get(actor) or {}).get("spawn_seq", 0))

    def _confirmed_death_seq(self, actor):
        """Semantic death is an explicit replicated status, never a close reason."""
        state = self._life_state_for_actor(actor)
        return state["seq"] if state["dead"] else None

    def _slot_outlived_death(self, actor, slot):
        if "move_handle" in (self.links.get(actor) or {}):
            return False  # explicit ownership changes, not distance, replace this actor
        seq = self._confirmed_death_seq(actor)
        track = self.slots.get(slot) or []
        if seq is None or not track or track[-1][0] <= seq:
            return False
        index = bisect.bisect_right(track, seq, key=_SAMPLE_SEQ) - 1
        if index < 0:
            return True
        return sum((a - b) ** 2 for a, b in
                   zip(track[index][2:5], track[-1][2:5])) >= 500.0 ** 2

    def snapshot(self, light=False, *, include_trails=True):
        v = self
        cur = self.latest_seq()
        if cur is None:
            cur = 0
        out_ents = []
        # 已确认死亡的槽：named 路径过滤后记录，匿名兜底路径同样压制。
        # 死亡后人物点立即从快照消失，只保留死亡盒；若槽已被后来者复用，
        # 不压制该槽，但旧死者身份仍必须移除，新轨迹只能作为新人重建。
        dead_slots = set()
        self_xyz = None; self_yaw = 0.0; self_name = None
        names = []

        # ---- 静默时长（信息字段）+ 死亡盒关联（标注）----
        # 移除/保留由「角色通道 close 事件」（self.gone，在 _derive 里过滤）驱动：
        #   蹲点（无 close / close_reason=Dormancy）→ 保留
        #   死亡（TearOff）/ 离开更新距离（Relevancy）/ 销毁（Destroyed）→ 点消失
        # slot 静默只是信息字段（silent_seq），不再驱动移除（蹲点也静默，会误删）。
        with v.lock:  # 与 poll 写 v.slots/v.boxes 互斥（否则 dict resize 时迭代抛 RuntimeError）
            silent_map = st.slot_staleness(v.slots, cur)
            # One death decision is shared by marker removal and box naming.
            # A downed/idle player must not enter this map.
            dead_map = {}
            for actor, slot in v.claim.items():
                death_seq = v._confirmed_death_seq(actor)
                if death_seq is not None and not v._slot_outlived_death(actor, slot):
                    dead_map[slot] = (death_seq, 0.0, None)
        # FSM 解码极重（全量 tile + repdec6，首次 5s+）；light 模式（admin 总览
        # 只需 ents 数/self 名）跳过，避免拖慢落地页/管理台。
        fsm = {} if light else v._fsm()

        # hp.total 是独立复制的真实 Health；普通属性流若提供 MaxHealth，
        # 则一并输出。部位属性另行保留，不能相加或归一化为总血量。
        # 出口再做一次防御性校验（入库门之外的第二道）：上限必须落在合理量级
        # （部位上限实测 30~50，放宽到 [5,500]），当前值必须在 [0, 上限] 内；
        # 越界一律按「该部位无数据」处理——宁可让前端画未知，也不画错。
        def _hp_of(actor):
            if not actor:
                return None
            hrec = v.health.get(actor)
            parts = (hrec or {}).get("parts") or {}
            out = {}
            total = parts.get("Health")
            maximum = parts.get("MaxHealth")
            if isinstance(total, (int, float)) and np.isfinite(total) and total >= 0:
                mx = float(maximum) if isinstance(maximum, (int, float)) \
                    and np.isfinite(maximum) and maximum > 0 and maximum >= total else None
                out["total"] = [round(float(total), 2), mx]
                out["source"] = hrec.get("sources", {}).get("Health", "attribute_set")
                out["seq"] = hrec.get("part_seq", {}).get("Health")
            for pk, wn in HP_PARTS:
                cv = parts.get(wn)
                mv = parts.get("Max" + wn)
                if cv is None and mv is None:
                    continue
                mx = float(mv) if isinstance(mv, (int, float)) \
                    and 5.0 <= float(mv) <= 500.0 else HP_PART_MAX[pk]
                if cv is not None:
                    cv = float(cv)
                    if not np.isfinite(cv) or cv < 0.0 or cv > mx \
                            or 0.0 < cv < HP_VAL_MIN:
                        cv = None       # 脏值兜底：视作未上报
                if cv is None:
                    # 只有上限、没有可信当前值 = 该部位无信息（前端按未知渲染，
                    # 与整条缺席等价），不进 hp，让前端的「已知部位」计数准确
                    continue
                out[pk] = [round(cv, 1) if cv is not None else None,
                           round(mx, 1)]
            return out or None

        # ---- 装备快照（头甲/防弹衣等级 + 首装备武器名）----
        # 等级从 equip 全量（armor+items 两个 slot 合并）扫 11 位物品 ID：
        # 1101 段=头甲、1105 段=防弹衣，等级=ID 第 7~8 位（armor_level 同式）。
        # cmd_idx 13~30 的 armor slot 判定在回归流上仅 2/191 行命中——itemId
        # 主要随 items slot 同步，按 slot 分开提取会漏掉绝大多数装备。
        _gear_cache = {}

        def _gear_of(actor):
            """actor -> (helmet_lv, vest_lv, first_wname, helmet_dur, vest_dur,
            bp_name, cr_name)。

            等级从 equip 全量（armor+items 合并）扫 11 位物品 ID：1101 段=头
            甲、1105 段=防弹衣，等级=ID 第 7~8 位（同角色换甲取 seq 最新）。
            耐久优先 GPAttributeSetHealth（持续同步的属性集，HelmetArmor
            Health/ArmorHealth+Max），兜底 EquipedArmorInfoArray 的 dur。
            返回 [cur, max]，无数据为 None。
            背包/胸挂（user 定版 0831 只上干员台）：1108=背包、1107=胸挂，
            同段多条取 seq 最新一条的 item_id 查 GEAR_NAMES，无名（未收录）
            返回 None。"""
            if not actor:
                return 0, 0, None, None, None, None, None
            hit = _gear_cache.get(actor)
            if hit is not None:
                return hit
            hl = vl = 0
            best_h = best_v = -1
            bp_id = cr_id = None
            best_bp = best_cr = -1
            hdur_c = []                # 装备 dur 候选（含脏值，合成时按 max 过滤）
            vdur_c = []
            hdur_max_c = []            # dur_max 候选（提取器/装备分支采集，
            vdur_max_c = []            #  health Max 缺席时的上限兜底）
            erec = v.equip.get(actor)
            if erec:
                for _slot in ("armor", "items"):
                    for f in (erec.get(_slot) or {}).values():
                        iid = f.get("item_id")
                        s = str(iid) if iid else ""
                        dur = f.get("dur")
                        dmx = f.get("dur_max")
                        if isinstance(dmx, (int, float)) and 0 <= dmx <= 500:
                            if s[:4] == "1101":
                                hdur_max_c.append(float(dmx))
                            elif s[:4] == "1105":
                                vdur_max_c.append(float(dmx))
                        # EquipmentInfoArray（cond=0 全员）元素 item_id 与
                        # dur 同条：dur=装备当前耐久（实测与 Max 对账，满甲
                        # 105=MaxArmorHealth 105）
                        if isinstance(dur, (int, float)) and dur >= 0:
                            # 元素「float 对」里第一个 float 不一定是耐久
                            # （实测混入 1.4e9 位型脏值与>max 的上限值），
                            # 收全候选、合成时按 MaxArmorHealth 过滤
                            if s[:4] == "1101":
                                hdur_c.append(float(dur))
                            elif s[:4] == "1105":
                                vdur_c.append(float(dur))
                        if len(s) != 11 or s[:4] not in ("1101", "1105", "1107", "1108"):
                            continue
                        sq = f.get("seq") or 0
                        if s[:4] in ("1107", "1108"):
                            # 胸挂/背包只取名字，不校验 tier；换装取 seq 最新
                            if s[:4] == "1108":
                                if sq >= best_bp:
                                    bp_id, best_bp = s, sq
                            elif sq >= best_cr:
                                cr_id, best_cr = s, sq
                            continue
                        try:
                            lv = int(s[6:8])
                        except ValueError:
                            continue
                        if not 1 <= lv <= 6:
                            continue
                        # 同角色换甲会留多条记录，取最近同步的一条
                        if s[:4] == "1101":
                            if sq >= best_h:
                                hl, best_h = lv, sq
                        elif sq >= best_v:
                            vl, best_v = lv, sq
            # 耐久合成：cur 优先 EquipedArmorInfoArray（cond=15 实时掉甲，
            # 全员可达；GPAttributeSetHealth 的 ArmorHealth 族 cur=cond=31
            # 只发本人连接），max 用 GPAttributeSetHealth（cond=0 全员）
            parts = (v.health.get(actor) or {}).get("parts") or {}
            hh = parts.get("HelmetArmorHealth")
            hm = parts.get("MaxHelmetArmorHealth")
            ah = parts.get("ArmorHealth")
            am = parts.get("MaxArmorHealth")
            # 上限兜底：health Max（cond=0 全员）缺席时用装备记录的
            # dur_max（提取器/装备分支采集的物品耐久上限）
            _hm = hm if hm is not None else (max(hdur_max_c) if hdur_max_c else None)
            _am = am if am is not None else (max(vdur_max_c) if vdur_max_c else None)
            _hc = hh if hh is not None else                 max([d for d in hdur_c if _hm is None or d <= max(_hm, 1) * 1.05],
                    default=None) if hdur_c else None
            _vc = ah if ah is not None else                 max([d for d in vdur_c if _am is None or d <= max(_am, 1) * 1.05],
                    default=None) if vdur_c else None
            hdur = [round(float(_hc), 1) if _hc is not None else None,
                    round(float(_hm), 1) if _hm is not None else None]                 if (_hc is not None or _hm is not None) else None
            vdur = [round(float(_vc), 1) if _vc is not None else None,
                    round(float(_am), 1) if _am is not None else None]                 if (_vc is not None or _am is not None) else None
            wid = (v.weapons.get(actor) or {}).get("first")
            wname = WEAPON_NAMES.get(wid) if wid else None
            bp = GEAR_NAMES.get(bp_id) if bp_id else None
            cr = GEAR_NAMES.get(cr_id) if cr_id else None
            _gear_cache[actor] = (hl, vl, wname, hdur, vdur, bp, cr)
            return hl, vl, wname, hdur, vdur, bp, cr

        def _curr_weap_of(actor):
            return weapon_fields(v.weapon_tracker, actor, WEAPON_NAMES)

        def _slot_pos(slot, tr):
            if slot is None or not tr:
                return None, None, None
            n = len(tr)
            # 快速路径：live tail-follow 下游标恒等于最新 seq，bisect 命中末尾。
            # 结果与 bisect_right(...)-1 完全一致，跳过二分+trail 边界计算。
            if tr[-1][0] <= cur:
                rec = tr[-1]
                j0 = n - 160 if n > 160 else 0
                trail = [[p[2], p[3], p[4]] for p in tr[j0:n:4]] if include_trails else None
                return [rec[2], rec[3], rec[4]], rec[5], trail
            j = bisect.bisect_right(tr, cur, key=_SAMPLE_SEQ) - 1
            if j < 0:
                return None, None, None
            rec = tr[j]
            jj = min(j + 2, n)
            trail = [[p[2], p[3], p[4]] for p in tr[max(0, jj - 160):jj:4]] if include_trails else None
            return [rec[2], rec[3], rec[4]], rec[5], trail

        with v.lock:
            _self_ent = next((e for e in v.ents if e["kind"] == "self"), None)
            # 自机血条：self 实体缺 actor 时用 claim 反查（slot -> actor）
            _self_actor = (_self_ent or {}).get("actor")
            if _self_actor is None and v.self_slot is not None:
                for _sa, _ss in v.claim.items():
                    if _ss == v.self_slot:
                        _self_actor = _sa
                        break
            if _self_ent is not None:
                self_xyz, self_yaw0, _t = _slot_pos(v.self_slot, v.slots.get(v.self_slot))
                self_yaw = self_yaw0 or 0.0
                self_name = _self_ent["name"]
            else:
                # self 实体不存在（hx=1 型开局，本地玩家 Character 未进 links，
                # 无 loc=None + num_exports=28 的自机标记）：self_slot 已由
                # field13 兜底确定，用槽位轨迹取实时坐标/朝向；self_name 用
                # ch 最小真人（协议级：本连接 PS 通道最先创建，见 _derive）。
                if v.self_slot is not None:
                    self_xyz, self_yaw0, _t = _slot_pos(v.self_slot, v.slots.get(v.self_slot))
                    self_yaw = self_yaw0 or 0.0
                if v.self_name:
                    self_name = v.self_name + "(self)"
            # 自机坐标兜底：MRA 槽位闭世界判定失效的对局（AI 抢槽 -> self_slot
            # =None）用 field13 小队广播的"我"轨迹（服务器直发，确定性）。
            if self_xyz is None and v.f13_self is not None:
                self_xyz = list(v.f13_self[2])
            # 注意：**不要**用 field13 的"我"去覆盖自机坐标。f13 只是按"最近槽"对齐，
            # 数据变稀时会指到队友槽上；一旦用它覆盖，自机就从"偶尔飘过去"变成"一直
            # 贴在队友身上"（实测）。自机身份以 self（PS Character / 槽位归属）为准。
            # 自机坐标无效（占位原点 / NaN / 超引擎世界上限）时**回退到轨迹里最近一个
            # 有效采样**，而不是当作未知：站着不动时位置就该停在原地，不能从雷达上
            # 消失。WORLD() 是引擎级判据，与其它坐标同一套；它同时挡住 (0,0,0) 占位
            # —— 那种值若当有效，rel 会退化成绝对世界坐标，距离能显示成上万米。
            if self_xyz is not None and not WORLD(self_xyz):
                self_xyz = None
                for _rec in reversed(v.slots.get(v.self_slot) or []):
                    _cand = [_rec[2], _rec[3], _rec[4]]
                    if WORLD(_cand):
                        self_xyz = _cand
                        break
            # 自机位置：**不做任何"按住"判决**，直接采用槽位/回退采样。
            #
            # 曾经这里有一段防飘：静置 >2s 后突变 >5m、或单帧跳 >20m 时，若 field13
            # 的"我"没有佐证，就把输出换回旧位置。它有个致命的自锁 —— `_d_moved` 用
            # 原始值减 `_lock` 算，而 `_lock` 又被写回**按住后的值**，于是只要原始位置
            # 一直在旧坐标 20m 外，就每帧都判"可疑"、永久按住。
            # 实测（2026-09-15 团队模式第二局，run_20260915_110104/flows/29）：
            # 该局 f13_self 全程为 None（佐证口子一次都没开），自机在出生点换局那次
            # 位移后**连续 312 秒冻在一个坐标**，同一窗口内原始槽位有 423 个不同坐标、
            # 其它玩家位置变化率 69% 而自机只有 15%；日志 185 条 [self-hold]。
            # 唯一解冻条件是"人走回旧坐标 20m 内"——用户看到的就是"走回去又好了"。
            #
            # 现在的行为：人不动时槽位本来就不出新采样，位置自然恒定在原地（这就是
            # "长时间不动就在那恒定"）；人真移动（含复活/换局落位）就直接跟随。
            # 代价：脏样本（实测有 y 符号翻转、跳 13.4km 的帧）也会如实画出来。要拦
            # 它只能拦在**样本**一级（判为无效则不采纳、退回上一个有效采样），
            # 绝不能再拦在输出一级 —— 输出一级的"按住"必然自锁。
            # 下面保留 2s 一条的跳变诊断：下次出脏样本时看 worker 日志即可定位。
            try:
                _prev_self = getattr(v, "_dbg_self_xyz", None)
                if _prev_self and self_xyz and WORLD(self_xyz) and WORLD(_prev_self):
                    _jump = ((self_xyz[0] - _prev_self[0]) ** 2
                             + (self_xyz[1] - _prev_self[1]) ** 2) ** 0.5
                    _now_dbg = time.time()
                    if _DEBUG and _jump > 20000.0 and _now_dbg - getattr(v, "_dbg_self_t", 0.0) > 2.0:
                        v._dbg_self_t = _now_dbg
                        print("[self-jump] %.0fm  槽=%s f13=%s  旧=(%.0f,%.0f) 新=(%.0f,%.0f)"
                              % (_jump / 100.0, v.self_slot is not None, v.f13_self is not None,
                                 _prev_self[0], _prev_self[1], self_xyz[0], self_xyz[1]),
                              flush=True)
                if self_xyz and WORLD(self_xyz):
                    v._dbg_self_xyz = list(self_xyz)
            except Exception:
                pass
            _cur_ts0 = v._seq_ts.get(cur, 0.0)
            for e in v.ents:
                slot = v.self_slot if e["kind"] == "self" else e["slot"]
                if e["kind"] == "self":
                    continue
                slot_reused = e.get("slot_reused") or v._slot_outlived_death(e["actor"], slot)
                _gone = v.gone.get(e["actor"])
                # Relevancy(3) is the protocol-level signal that the character
                # left the observer's update range. Keep the last point, but
                # expose it as stale so the radar cannot present it as live.
                out_of_range = (_gone is not None and _gone[1] == 3
                                and not slot_reused)
                if v._confirmed_death_seq(e["actor"]) is not None or v._actor_removed(e["actor"]):
                    if slot is not None and not slot_reused:
                        dead_slots.add(slot)  # prevent anonymous fallback resurrection
                    continue
                pos, yaw, trail = _slot_pos(slot, v.slots.get(slot))
                # ---- 静态 spawn 标记（无槽位实体的唯一坐标来源）----
                # 出生点作为最后观测保留，spawn_mark/age_sec 区分实时坐标。
                _sp_age = None
                if e.get("spawn_seq") is not None:
                    _sp_ts = v._seq_ts.get(e["spawn_seq"])
                    if _sp_ts and _cur_ts0:
                        _sp_age = max(0.0, _cur_ts0 - _sp_ts)
                if pos is None and slot is None and e.get("spawn_loc") \
                        and e.get("name") and e.get("spawn_seq") is not None:
                    # Retain the last observed spawn point across silence;
                    # spawn_mark and age_sec distinguish it from live movement.
                    sp = e["spawn_loc"]
                    pos = [float(sp[0]), float(sp[1]), float(sp[2])]
                    _gh, _gv, _gw, _hd, _vd, _bp, _cr = _gear_of(e["actor"])
                    _cw = _curr_weap_of(e["actor"])
                    out_ents.append({
                        "key": "spawn-%s" % e["actor"], "name": e["name"], "kind": e["kind"],
                        "xyz": pos, "world": pos,
                        "rel": [pos[0] - self_xyz[0], pos[1] - self_xyz[1], pos[2] - self_xyz[2]] if self_xyz else None,
                        "yaw": None, "slot": None, "ch": e["actor"], "eid": e["actor"], "trail": None,
                        "team": e.get("team"), "camp": e.get("camp"),
                        "is_bot": e.get("is_bot", False),
                        "helmet": _gh, "vest": _gv, "weapon": _gw,
                        "helmet_dur": _hd, "vest_dur": _vd,
                        "bp": _bp, "cr": _cr,
                        **_cw,
                        # 出生点标记：位置=出生点，不是实时位置（前端画空心 + 龄）
                        "spawn_mark": True,
                        "age_sec": round(_sp_age, 1) if _sp_age is not None else None,
                        "out_of_range": out_of_range,
                        "position_age_sec": round(_sp_age, 1) if _sp_age is not None else None,
                        "position_stale": out_of_range or (_sp_age is not None and _sp_age > STALE_HIDE_SEC),
                        "hp": _hp_of(e["actor"])})
                    if e["name"]:
                        names.append({"ch": None, "name": e["name"], "kind": e["kind"]})
                    continue
                if pos is None:
                    continue
                rel = [pos[0] - self_xyz[0], pos[1] - self_xyz[1], pos[2] - self_xyz[2]] if self_xyz else None
                silent = silent_map.get(slot)
                is_mate = slot in v.mate_slots
                # 静止、休眠和离开更新范围均可能停更；时间只标记数据年龄，
                # 不作为移除依据。显式死亡、销毁和槽位复用仍走生命周期判断。
                _tr_stale = v.slots.get(slot) or []
                _stale_ts = _tr_stale[-1][1] if _tr_stale else 0.0
                _cur_ts = v._seq_ts.get(cur, 0.0)
                _silent_sec = (_cur_ts - _stale_ts) if (_stale_ts and _cur_ts) else 0.0
                # 槽被复用：e 已被 _derive 标记 slot_reused + name 置 None；
                # 此处直接退化为匿名未知玩家槽，不再视作原角色
                if slot_reused:
                    anon_name2, anon_kind2 = "未知玩家", "unknown"
                    if is_mate:
                        anon_kind2 = "mate"
                    out_ents.append({
                        "key": "slot-%s" % slot, "name": anon_name2, "kind": anon_kind2,
                        "xyz": pos, "world": pos, "rel": rel, "yaw": yaw,
                        "slot": slot, "ch": slot, "eid": 0, "trail": trail,
                        "dead": False,
                        "out_of_range": False,
                        "hp": None,
                        "silent_seq": silent if silent is not None else 0,
                        "death_box": None,
                    })
                    continue
                # 队友阵亡判定：只采信权威死亡信号（死亡盒 dead_map / 致命部位 vital
                # 归零，二者都已在 _derive 与 dead_map 构建里落过）。**不再用槽位长
                # 静默判死**——蹲点活人也会静默（4330 局稳健医疗亡命峰槽位稀疏广播
                # 间隙达 6510 seq、全程血量正常，被判 dead=True 后又复活，是"死而复
                # 生"的根因；6870 金标准里恶犬伊凡/战地妙手静默 1508/2042 也是蹲点
                # 活人保持 alive）。静默只是信息字段（前端显示），不驱动移除/判死
                # （漏人最不可接受，user 定版；这类若真死亡另有死亡盒/vital 兜底）。
                is_dead = slot in dead_map
                # 统一产品语义：真人、队友、人机只要已确认死亡，人物点立即移除。
                # 死亡落点由后面的死亡盒表达，不再把 dead=True 尸体点留在地图/名单。
                if is_dead:
                    if slot is not None:
                        dead_slots.add(slot)
                    continue
                # 非队友长静默不再按「静默时长」移除：蹲点活人也会静默（实测
                # gap 最长 7 分钟），按静默删 = 漏人（最不可接受）。移除只由
                # close_reason + 死亡盒 owner 两个权威信号驱动（已在 _derive 过滤）。
                # 故这里保留静默的实体（可能蹲点、也可能 close/盒都丢失的极少数
                # 阵亡者，宁多勿漏）。
                kind = "mate" if is_mate else e["kind"]
                fsm_rec = fsm.get(e["actor"]) or {}
                _s_lbl, _s_key = fight_status(
                    fsm_rec.get("fight", {}), fsm_rec.get("fsm_states", []))
                # FSM 无状态时血量兜底（5130 局：R_ActiveStates 整局缺失）；
                # 已判死者不再标倒地（尸体走 ☠ 语义）
                if _s_key is None and not is_dead:
                    _s_lbl, _s_key = v._dying_fallback(e["actor"], _cur_ts)
                _gh, _gv, _gw, _hd, _vd, _bp, _cr = _gear_of(e["actor"])
                _cw = _curr_weap_of(e["actor"])
                out_ents.append({
                    "key": "slot-%s" % slot, "name": e["name"], "kind": kind,
                    "xyz": pos, "world": pos, "rel": rel, "yaw": yaw,
                    "slot": slot, "ch": slot, "eid": e["actor"], "trail": trail,
                    "dead": is_dead,
                    "out_of_range": out_of_range,
                    "position_age_sec": round(max(0.0, _silent_sec), 1),
                    "position_stale": out_of_range or _silent_sec > STALE_HIDE_SEC,
                    "hp": _hp_of(e["actor"]),
                    "helmet": _gh, "vest": _gv, "weapon": _gw,
                    "helmet_dur": _hd, "vest_dur": _vd,
                    "bp": _bp, "cr": _cr,
                    **_cw,
                    "status": _s_lbl, "status_key": _s_key,
                    "silent_seq": silent if silent is not None else 0,
                    "death_box": dead_map.get(slot),
                    "fsm_states": fsm_rec.get("fsm_states", []),
                    "fight": fsm_rec.get("fight", {}),
                    "team": e.get("team"), "camp": e.get("camp"),
                    "is_bot": e.get("is_bot", False),
                    "hero": e.get("hero"),
                })
                if e["name"]:
                    names.append({"ch": slot, "name": e["name"], "kind": kind})
            bound = {x["slot"] for x in out_ents if x.get("slot") is not None}
            # 已消失角色（gone）认领的 slot：若槽在角色关闭后**仍有新采样**，
            # 说明槽位已被新成员复用（2490 局 slot13/14/15 原主阵亡后继续广播
            # 新成员轨迹）——解除压制照常上屏（匿名）；随主死亡静默的槽不上屏。
            # 「随主静默不上屏」只适用于**死亡类** close（Destroyed/TearOff，
            # DEAD_CLOSE_REASONS）：尸体不该以活人点显示。Relevancy(3) = 离开
            # 更新距离、角色仍活着（2426 局 slot18 汤泡腿饭：结算阶段出更新
            # 距离@32470，槽同步停更）——按旧规则整条槽被吞，实体直接从雷达
            # 消失（漏人）。Relevancy 静默槽保留上屏（dead 由死盒关联判定）。
            claimant_of = {}
            for _ca, _cs in v.claim.items():
                claimant_of[_cs] = _ca
            for s, tr in v.slots.items():
                if s in bound or s == v.self_slot or s not in v.member_slots \
                        or s in dead_slots:
                    continue
                _ca = claimant_of.get(s)
                reused = _ca is not None and v._slot_outlived_death(_ca, s)
                if (_ca is not None and (v._confirmed_death_seq(_ca) is not None or v._actor_removed(_ca))
                        and not reused):
                    continue
                if reused:
                    _ca = None
                n2 = len(tr)
                if tr[-1][0] <= cur:
                    rec = tr[-1]
                else:
                    j = bisect.bisect_right(tr, cur, key=_SAMPLE_SEQ) - 1
                    if j < 0:
                        continue
                    rec = tr[j]
                silent = silent_map.get(s)
                is_mate = s in v.mate_slots
                # 同 named 路径：队友死判定只采信权威信号（dead_map），不用静默
                # 时长判死（蹲点活人也静默；见上段注释）。静默保留为信息字段。
                is_dead = s in dead_map
                _stale_ts = tr[-1][1]
                _cur_ts = v._seq_ts.get(cur, 0.0)
                _silent_sec = (_cur_ts - _stale_ts) if (_stale_ts and _cur_ts) else 0.0
                # 未知槽同样不按静默时长移除（漏人最不可接受）；死亡由死盒关联
                # (dead_map) 标注，随主死亡静默的槽已在上面 continue。
                # 匿名槽命名：seq 对齐（协议级，见 _derive 的 anon_names）
                anon_p = None if reused else v.anon_names.get(s)
                # 认领 actor 无条件取：死 AI 隐藏与倒地兜底都要用
                _ca = None if reused else claimant_of.get(s)
                # 匿名兜底也遵守同一规则，防止 named 路径移除后又以
                # 「未知玩家」/队友的形式复活一帧。
                if is_dead:
                    dead_slots.add(s)
                    continue
                if anon_p is not None:
                    anon_name = anon_p["name"]
                    if anon_name and anon_name == v.self_name:
                        continue
                    # 伪真人 bot（PS bot=True）按用户定版 0828 当真人对待 → player
                    anon_kind = "player"
                else:
                    # 槽位 ≠ 队友（用户语义定版）：无名槽只标「未知玩家」，
                    # 不得暗示友方
                    anon_name, anon_kind = "未知玩家", "unknown"
                if is_mate:
                    anon_kind = "mate"
                # 匿名槽同享血量倒地兜底（认领 actor 的健康记录）
                _ds = (v._dying_fallback(_ca, _cur_ts)
                       if not is_dead and _ca is not None else (None, None))
                _gone = v.gone.get(_ca) if _ca is not None else None
                out_of_range = (_gone is not None and _gone[1] == 3
                                and not reused and not is_dead)
                out_ents.append({
                    "key": "anon-%s" % s, "name": anon_name, "kind": anon_kind,
                    "xyz": [rec[2], rec[3], rec[4]], "world": [rec[2], rec[3], rec[4]],
                    "rel": None, "yaw": rec[5], "slot": s, "ch": s, "eid": 0, "trail": None,
                    "dead": is_dead,
                    "out_of_range": out_of_range,
                    "position_age_sec": round(max(0.0, _silent_sec), 1),
                    "position_stale": out_of_range or _silent_sec > STALE_HIDE_SEC,
                    "status": _ds[0], "status_key": _ds[1],
                    "hp": _hp_of(_ca),
                    # 匿名命名命中的 PlayerState 带 team/camp/hero —— 必须透传，
                    # 否则该路径上屏的实体丢队伍气泡（2426 局 slot18 汤泡腿饭实证）
                    "team": getattr(v, "slot_teams", {}).get(s) or squad_id((anon_p or {}).get("team")),
                    "camp": (anon_p or {}).get("camp"),
                    "is_bot": confirmed_player_bot(
                        anon_p,
                        HERO_NAMES.get((anon_p or {}).get("hero_id")) is not None),
                    "hero": HERO_NAMES.get((anon_p or {}).get("hero_id")),
                    "silent_seq": silent if silent is not None else 0,
                    "death_box": dead_map.get(s),
                })
            filtered = []
            for entity in out_ents:
                actor = entity.get("eid")
                if isinstance(actor, int) and actor > 0:
                    life = v._life_state_for_actor(actor)
                    entity.update(life_state=life["state"], life_status=life["status"],
                                  life_seq=life["seq"], life_source=life["source"])
                    if life["downed"]:
                        entity.update(status="倒地", status_key="down")
                    if entity["kind"] != "self" and (life["dead"] or v._actor_removed(actor)):
                        continue
                if str(entity.get("key", "")).startswith("anon-") and entity.get("slot") in dead_map:
                    continue
                filtered.append(entity)
            out_ents = filtered
            positions = {actor: list(v.slots[slot][-1][2:5]) for actor, slot in v.claim.items()
                         if slot in v.slots and v.slots[slot]}
            dead_candidates = []
            for _a, _ev in (getattr(v.life_tracker, "_events", {}) or {}).items():
                if _ev.get("status") != 2:
                    continue
                _lk = v.links.get(_a) or {}
                _ps = v.players.get(_lk.get("ps_guid")) if _lk.get("ps_guid") is not None else None
                _nm = (_ps or {}).get("name")
                if _nm:
                    dead_candidates.append((int(_ev.get("seq") or 0), _a, _nm))
            used_dead = set()
            for actor, record in v.boxes.items():
                if record.get("hidden") or record.get("gone_seq") is not None:
                    continue
                pos = v.box_tracker.world_position(actor, positions) or record.get("loc")
                if not pos or not WORLD(pos):
                    continue
                owner = v._confirmed_box_owner(record)
                name = v._char_name_kind(owner)[0] if owner is not None else None
                if owner is None and not name and record.get("spawn_seq") is not None and dead_candidates:
                    _best = min((c for c in dead_candidates if c[1] not in used_dead),
                                key=lambda c: abs(c[0] - int(record["spawn_seq"])), default=None)
                    if _best is not None and abs(_best[0] - int(record["spawn_seq"])) <= 12:
                        owner, name = _best[1], _best[2]
                        used_dead.add(_best[1])
                if not name:
                    name = record.get("player_name")
                    if name and ("LOCTABLE" in name or "/Game/" in name):
                        key = ant.extract_ref_key(name.encode())
                        name = v.ai_names.get(key) if key is not None else None
                entity = box_entity(actor, record, pos, owner, name, self_xyz, v.ai_names)
                entity.update(v.box_contents.describe(actor, LOOT_NAMES))
                raw_owner = record.get('deceased_actor') or record.get('owner_actor')
                owner_link = v.links.get(owner) or v.links.get(raw_owner) or {}
                owner_cls = str(owner_link.get('cls') or '')
                owner_ai = ('AICharacter' in owner_cls or 'Character_AI' in owner_cls
                            or owner_link.get('ai_key') is not None)
                if entity['is_ai'] is None and (owner_ai or (raw_owner is not None and not owner_link)
                                                or (raw_owner is None and entity.get('name') == '死亡盒')):
                    entity['is_ai'] = True
                if owner is not None:
                    if entity['is_ai'] is None and v._char_name_kind(owner)[1] == 'ai':
                        entity['is_ai'] = True
                    owner_life = v._life_state_for_actor(owner)
                    entity['owner_dead'] = owner_life['dead'] if owner_life['confirmed'] else None
                out_ents.append(entity)
            # 露天物资（BP_InventoryPickup*）：spawn 前缀坐标 + handle52 ID FName。
            # 有 ID 显名表名；无 ID 变体（205/357 位族）显「物资」通用点。
            # 停驻只标记候选；属性失效或服务器销毁通知从活动表移除。
            # 保留休眠/离开相关性的物品，消失原因存入 loot_history。
            # 对局销毁（live 实时流超过 GAME_END_STALE_SEC 无新帧 = 游戏已退出）：
            # 物资不再上报——清单/地图随对局销毁清空（下一局新 LiveView 全新开始）。
            # 回放路径（Playback/BattleView，ts 为历史时刻）不走此判定，由游标时序管。
            loot_visible = (v._last_data_ts <= 0.0
                            or time.time() - v._last_data_ts <= GAME_END_STALE_SEC)
            if loot_visible:
                for container in v.container_tracker.active.values():
                    c = dict(container)
                    c['rel'] = [a-b for a,b in zip(c['world'], self_xyz)] if self_xyz else None
                    out_ents.append(c)
                for la, lr in v.loot.items():
                    nm = lr.get("name")
                    if (not lr.get("item_id") or not nm or nm in ("物资", "未知物资")
                            or str(nm).startswith("???")):
                        continue
                    lp = lr["loc"]
                    out_ents.append({
                        "key": "loot-%s" % la, "name": lr.get("name") or "物资",
                        "kind": "loot", "xyz": list(lp), "world": list(lp),
                        "rel": [lp[0] - self_xyz[0], lp[1] - self_xyz[1],
                                lp[2] - self_xyz[2]] if self_xyz else None,
                        "yaw": None, "slot": None, "ch": la, "eid": la, "trail": None,
                        **loot_display_fields(lr),
                    })
            flow = "%s / %s" % (v.run_dir.name, v.flow_dir.name)
            meta = {"mra_ch": v.mra_ch, "n_lb": v.n_lb,
                    "bindings_move_handle": sum("move_handle" in v.links.get(a, {}) for a in v.claim),
                    "health_batches": dict(v.health_batches.stats),
                    "life_decode": dict(v.life_tracker.stats),
                    "playerstate_decode": dict(v.ps_decode_stats),
                    "weapon_decode": dict(v.weapon_tracker.stats),
                    "team_decode": dict(v.team_tracker.stats),
                    "pickup_decode": dict(v.pickup_tracker.stats),
                    "box_decode": dict(v.box_tracker.stats),
                    "box_contents_decode": dict(v.box_contents.stats),
                    "boxes_removed": len(v.box_tracker.removed),
                    "loot_gone": not loot_visible,
                    "loot_confirmed_gone": len(v.loot_history),
                    "loot_confirmed_pickups": sum(
                        r.get("pickup_confidence") == "confirmed" for r in v.loot_history.values()),
                    "loot_pickup_candidates": sum(
                        1 for lr in v.loot.values()
                        if lr.get("pickup_candidate_seq") is not None)}
        for entity in out_ents:
            if entity.get('hp'):
                entity['hp'] = operator_health(entity['hp'], entity['kind'] in ('player', 'mate', 'self') or bool(entity.get('hero')))
        return {
            "self": self_xyz, "self_yaw": self_yaw, "self_name": self_name,
            "self_src": getattr(v, "self_src", None),
            "self_team": getattr(v, "self_team", None),
            "self_team_source": getattr(v, "self_team_source", "unknown"),
            "self_hp": operator_health(_hp_of(_self_actor)),
            "self_life": v._life_state_for_actor(_self_actor) if _self_actor is not None else None,
            "live_active": True, "origin": [0, 0],
            "entities": out_ents, "names": names,
            "flow": flow, "meta": meta,
            "gone": len(v.gone),
            "cursor": {"seq": cur, "lo": None, "hi": cur, "speed": 0, "live": True},
        }
