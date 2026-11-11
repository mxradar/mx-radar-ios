# -*- coding: utf-8 -*-
"""s2c_dec.py - 战斗 S2C UDP 去混淆（512 字节循环表 + 密文反馈 XOR）.

模型（ DOWNLINK_ENCRYPTION_0904.md §3.19/3.20 ）:
    P[i] = C[i] ^ C[i-1] ^ K[i % 512],  C[-1] = 0, 每条 UDP 记录独立重置.
还原后即标准引擎包，直接进 flowload/src/codec/repdec4 原生管线.

表按连接实例化。两种候选估计方式，均需独立验证:
    - recover_table_scored(frames, ref): 对裸流参照分布评分，头部序号可造成偏差;
    - recover_table(frames): D 位置众数，非零明文众数会造成残差。
完整恢复和留出验证见 tools/recover_s2c.py；已知表的下行导出见 tools/decode_udp.py。
"""
import json, math
from pathlib import Path


def load_table(path):
    path = Path(path)
    if path.suffix.lower() == ".bin":
        table = path.read_bytes()
    elif path.suffix.lower() == ".hex":
        table = bytes.fromhex(path.read_text(encoding="ascii"))
    else:
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, list) or any(
            type(value) is not int or not 0 <= value <= 255 for value in values
        ):
            raise ValueError("table JSON must be a list of byte integers")
        table = bytes(values)
    if len(table) != 512:
        raise ValueError("table must be 512 bytes")
    return table


def decode_s2c_inplace(buffer, table) -> None:
    """Decode a writable byte buffer, using the previous ORIGINAL byte."""
    if len(table) != 512:
        raise ValueError("table must be 512 entries")
    view = memoryview(buffer)
    if view.readonly or view.ndim != 1 or view.itemsize != 1:
        raise ValueError("buffer must be a writable one-dimensional byte buffer")
    view = view.cast("B")
    prev = 0
    for i, c in enumerate(view):
        view[i] = c ^ prev ^ table[i & 511]
        prev = c


def encode_s2c(plaintext: bytes, table) -> bytes:
    """Inverse transform; round trips alone do not prove plaintext correctness."""
    if len(table) != 512:
        raise ValueError("table must be 512 entries")
    out = bytearray(len(plaintext))
    prev = 0
    for i, p in enumerate(plaintext):
        prev = p ^ table[i & 511] ^ prev
        out[i] = prev
    return bytes(out)


def decode_s2c(ciphertext: bytes, table) -> bytes:
    if len(table) != 512:
        raise ValueError("table must be 512 entries")
    out = bytearray(len(ciphertext))
    prev = 0
    for i, c in enumerate(ciphertext):
        out[i] = c ^ prev ^ table[i & 511]
        prev = c
    return bytes(out)


def recover_table(datagrams, min_count=8, *, max_len=1024):
    """D 位置众数候选；非零明文众数会引入偏差，不能保证头部或正文正确。

    未覆盖的表位会报错，不再以零填充。需要独立明文和完整 bunch 校验。
    """
    if max_len < 1 or min_count < 1:
        raise ValueError("max_len and min_count must be positive")
    cnt = [None] * 512
    for c in datagrams:
        if not c or len(c) > max_len:
            continue
        prev = 0
        for i, b in enumerate(c):
            j = i & 511
            if cnt[j] is None:
                cnt[j] = {}
            cnt[j][b ^ prev] = cnt[j].get(b ^ prev, 0) + 1
            prev = b
    table = []
    unknown = []
    for j in range(512):
        c = cnt[j]
        if not c or max(c.values()) < min_count:
            unknown.append(j)
        else:
            table.append(max(c, key=c.get))
    if unknown:
        raise ValueError(f"insufficient observations for {len(unknown)} table positions")
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
