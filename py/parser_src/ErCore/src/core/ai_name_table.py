# -*- coding: utf-8 -*-
"""ai_name_table.py — LOC_AICharacterNameTable 每局名字表解析 + AI 角色 key 关联。

逆向结论（round75 坐实）：
  1. AI 角色（BP_DFMCharacter_AI_*_C）spawn 无 PlayerState 引用（h14/h18/h19 全 None），
     名字字段承载本地化引用：
         LOCTABLE(".../LOC_AICharacterNameTable.LOC_AICharacterNameTable", "<N>_Name")
  2. 名字表在 wire 明文传输，每局一张（同 key 跨局不同名），结构：
         表名(49B) + \\0 + 4B padding + int32 count
         + count × [ "<N>_Name\\0" + (int32 A + 变宽常量 + FString(len+UTF-16)) × k ]
  3. key 与 AI 子类绑定实测（flow 1368）：Firebat -> 167_Name, ShielderLight -> 162_Name。
"""
import re

FULL_NAME = b"LOC_AICharacterNameTable.LOC_AICharacterNameTable"
TABLE_HEAD = FULL_NAME + b"\x00"
REF_RE = re.compile(rb'LOCTABLE\("[^"]*LOC_AICharacterNameTable[^"]*",\s*"(\d+)_Name"\)')


def plausible(s):
    if not (1 <= len(s) <= 64):
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in s):
        return False
    if "\ufffd" in s:
        return False
    good = sum(1 for c in s if c.isalnum() or c in " _-·.`~!@#")
    return good >= min(2, len(s))


def _read_entry_name(tail, p):
    """key 后位置 p 读一个条目：int32 A + 变宽常量 + FString(len<=-1, UTF-16)。
    长度字段间距不固定，在 p+4..p+12 窗口扫描定位（界限 [-128,-1]）。"""
    for delta in range(4, 13):
        q = p + delta
        if q + 4 > len(tail):
            break
        n = int.from_bytes(tail[q:q + 4], "little", signed=True)
        if not (-128 <= n <= -1):
            continue
        units = []
        r = q + 4
        ok = True
        for _ in range(-n):
            if r + 2 > len(tail):
                ok = False; break
            u = tail[r] | (tail[r + 1] << 8)
            r += 2
            if u == 0:
                break
            if not (0x20 <= u <= 0xD7FF or 0xE000 <= u <= 0xFFFF):
                ok = False; break
            units.append(u)
        if not ok or not units:
            continue
        s = "".join(chr(u) for u in units)
        if plausible(s):
            return s, r
    return None, p


def parse_table_instance(payload):
    """单个报文 payload -> {key:int -> name}（无表头返回 {}）。
    同实例内后写覆盖先写；调用方负责跨实例合并策略。"""
    out = {}
    m = payload.find(TABLE_HEAD)
    if m < 0:
        return out
    tail = payload[m:]
    p = len(FULL_NAME) + 1 + 4          # 表名 + \0 + 4B padding
    if p + 4 > len(tail):
        return out
    count = int.from_bytes(tail[p:p + 4], "little")
    p += 4
    if not (1 <= count <= 64):
        return out
    key_re = re.compile(rb"(\d+)_Name\x00")
    for _ in range(count):
        km = key_re.match(tail, p)
        if not km:
            break
        key = int(km.group(1))
        p = km.end()
        while p + 16 <= len(tail):
            a = int.from_bytes(tail[p:p + 4], "little")
            if a > 0x10000:
                break
            name, p2 = _read_entry_name(tail, p)
            if name is None:
                break
            out[key] = name             # 后条目覆盖（同 key 多条目取最新）
            p = p2
    return out


def extract_ref_key(payload):
    """AI 角色 spawn payload -> LOCTABLE key（int）或 None。"""
    m = REF_RE.search(payload)
    return int(m.group(1)) if m else None


def collect_table(b):
    """扫全 flow 的逻辑 bunch（load_cached 产物）-> 当局 {key -> name}。
    同 key 多实例取 seq 更大者（对局内状态更新以最新为准）。"""
    best_seq = {}
    for ch in sorted(b):
        for x in b[ch]:
            if TABLE_HEAD not in x.payload:
                continue
            for k, name in parse_table_instance(x.payload).items():
                if k not in best_seq or x.seq >= best_seq[k][0]:
                    best_seq[k] = (x.seq, name)
    return {k: v[1] for k, v in best_seq.items()}
