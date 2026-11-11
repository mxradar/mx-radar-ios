# -*- coding: utf-8 -*-
"""s2c_dec.py - 战斗 S2C UDP 去混淆（512 字节循环表 + 密文反馈 XOR）.

模型（ DOWNLINK_ENCRYPTION_0904.md §3.19/3.20 ）:
    P[i] = C[i] ^ C[i-1] ^ K[i % 512],  C[-1] = 0, 每条 UDP 记录独立重置.
还原后即标准引擎包，直接进 flowload/src/codec/repdec4 原生管线.

表按连接实例化。两种恢复方式:
    - recover_table_scored(frames, ref): 对裸流参照分布评分（文档 §7），~2-4s，推荐;
    - recover_table(frames): D 位置众数（历史字零先验，头部可用，bunch 区有残差）.
"""
import json, math


def load_table(path):
    return bytes(json.load(open(path)))


def decode_s2c(ciphertext: bytes, table) -> bytes:
    if len(table) != 512:
        raise ValueError("table must be 512 entries")
    out = bytearray(len(ciphertext))
    prev = 0
    for i, c in enumerate(ciphertext):
        out[i] = c ^ prev ^ table[i & 511]
        prev = c
    return bytes(out)


def recover_table(datagrams, min_count=8):
    """D 位置众数快速恢复（无参照时用；头部正确，bunch 区带 mode(P) 残差）."""
    cnt = [None] * 512
    for c in datagrams:
        prev = 0
        for i, b in enumerate(c):
            j = i & 511
            if cnt[j] is None:
                cnt[j] = {}
            cnt[j][b ^ prev] = cnt[j].get(b ^ prev, 0) + 1
            prev = b
    table = []
    for j in range(512):
        c = cnt[j]
        table.append(max(c, key=c.get) if c and max(c.values()) >= min_count else 0)
    return table


def load_ref_dist(path):
    """裸流参照: 位置明文计数 -> 对数概率表 [512][256]."""
    d = json.loads(open(path, "r", encoding="utf-8").read())
    ref = []
    for pairs in d["dist"]:
        tot = sum(c for _, c in pairs) + 76.8
        tbl = [-20.0] * 256
        for v, c in pairs:
            tbl[v] = math.log((c + 0.3) / tot)
        ref.append(tbl)
    return ref


def recover_table_scored(datagrams, ref):
    """文档 §7 评分恢复: K[j] = argmax_k Σ_c Dcnt_j(c) · log(P_ref_j(c ^ k))."""
    Dcnt = [dict() for _ in range(512)]
    for c in datagrams:
        prev = 0
        for i, b in enumerate(c):
            j = i & 511
            d = Dcnt[j]
            v = b ^ prev
            d[v] = d.get(v, 0) + 1
            prev = b
    K = []
    for j in range(512):
        pl = ref[j]
        d = Dcnt[j]
        best_k, best_s = 0, -1e18
        for k in range(256):
            s = 0.0
            for c, cnt in d.items():
                s += cnt * pl[c ^ k]
            if s > best_s:
                best_s, best_k = s, k
        K.append(best_k)
    return K
