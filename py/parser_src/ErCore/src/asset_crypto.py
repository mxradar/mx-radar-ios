# -*- coding: utf-8 -*-
"""asset_crypto.py - 数据文件静态保护：把"拷走文件即得明文"变成"必须先逆出密钥"。

设计取舍
    * 只保护算法/协议数据，不保护需要给浏览器直读的资源：`web/` 下的 JS 必须
      明文（前端要执行），`item_names_cn.json` 保持明文（纯名字表，量也大）。
    * 加密用 stdlib 的 SHAKE-256 生成 keystream 再做异或（流加密），零第三方依赖、
      底层是 C，3MB 约 12ms。**目标不是强密码学** —— 密钥终究在二进制里、能被
      逆出来；目标是抬高"直接拷文件"的成本，把它从零成本变成需要逆向。
    * 仓库里的源文件保持**明文**（便于开发、检索、diff）；打包时由
      build_exe.py 加密后再装进产物。read_bytes() 靠魔数自动识别两种形态，
      所以源码跑和 exe 跑走同一份代码。

格式：MAGIC(4 字节) + (原文件字节 XOR keystream)
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

MAGIC = b"NRA1"

# 打包密钥（32 字节）。只随二进制分发；泄漏它等于产品被拿着跑，所以别提交到公开仓库。
_KEY = bytes.fromhex("1f714614c0f21a3f7d58d069581198da9a56f07a04a6bd6ad4307fd174ee0b2b")

# 运行期落盘数据的用途标识：同一主密钥下按用途派生不同 keystream，
# 一类数据泄漏不会连带解开另一类。
PURPOSE_FLOWCACHE = b"flowcache"    # 恢复出的 S2C 表缓存（= 该连接的解密钥）
PURPOSE_FLOWPARSE = b"flowparse"    # flow 解析缓存（含解密后的原始载荷）
PURPOSE_RUNTIME = b"runtime"        # 其它运行期状态（战斗流检测缓存等）

# 需要保护的资产（相对仓库根）。build_exe.py 打包时把这些加密进产物。
PROTECTED = (
    "src/core/prop_meta_R36.json",            # 属性索引/偏移表
    "src/core/replayouts_R31.json",           # RepLayout 目录
    "src/core/subobj_replayouts_R32.json",    # 子对象 RepLayout 目录
    "src/core/s2c_reference.npz",             # S2C 512B 表恢复的参考分布
    "src/core/s2c_ref_dist.json",             # S2C 裸流位置明文分布
    "src/core/box_contents_schema_0904.json",
    "src/core/death_box_schema_0904.json",
    "src/core/death_box_schema_legacy.json",
    "src/core/pickup_schema_0904.json",
    "src/core/playerstate_schema_0904.json",
)


def _xor(data: bytes, purpose: bytes = b"") -> bytes:
    if not data:
        return data
    # 按用途派生 keystream：同一把主密钥下不同用途的密文互不可解 —— 泄漏一份
    # flowcache 不会连带解开随包资产。
    ks = hashlib.shake_256(_KEY + purpose).digest(len(data))
    # 大整数异或比逐字节循环快两个数量级
    return (int.from_bytes(data, "big") ^ int.from_bytes(ks, "big")).to_bytes(len(data), "big")


def encrypt(data: bytes, purpose: bytes = b"") -> bytes:
    """明文 -> 带魔数的密文。purpose 区分用途（随包资产用空值）。"""
    return MAGIC + _xor(data, purpose)


def decrypt(blob: bytes, purpose: bytes = b"") -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("不是加密资产（缺魔数）")
    return _xor(blob[len(MAGIC):], purpose)


def decrypt_blob(blob: bytes, purpose: bytes = b"") -> bytes:
    """有魔数就解密，没有就原样返回（兼容历史明文）。

    与 read_bytes() 不同：**不缓存** —— 用于会被反复改写的文件（如 flowcache），
    否则按路径缓存会读到过期内容。
    """
    return decrypt(blob, purpose) if blob.startswith(MAGIC) else blob


@lru_cache(maxsize=16)
def _read_cached(path_str: str) -> bytes:
    blob = Path(path_str).read_bytes()
    return decrypt(blob) if blob.startswith(MAGIC) else blob


def read_bytes(path) -> bytes:
    """读资产：有魔数就解密，没有就原样返回（源码形态）。结果按路径缓存。"""
    return _read_cached(str(Path(path)))


def read_text(path, encoding: str = "utf-8") -> str:
    return read_bytes(path).decode(encoding)


def is_encrypted(path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False
