# -*- coding: utf-8 -*-
"""blocks.py — 修正版内容块铺砌解析（含 upstream 的"子对象删除"分支）。

UActorChannel::ReadContentBlockPayload:
    RepObj = ReadContentBlockHeader(Bunch, bObjectDeleted, bHasRepLayout)
        1b bHasRepLayout
        1b bIsActor ; 若 bIsActor 立即返回
        packed GUID SubObject
        1b bStablyNamed ; 若为真立即返回
        packed GUID SubObjectClass
        若 ClassNetGUID 无效(0) -> bObjectDeleted = true, 直接返回
    if (bObjectDeleted) { OutPayload.SetData(Bunch, 0); return 0; }   <<< 不读 NumPayloadBits
    packed NumPayloadBits            (可为 0)
    payload = NumPayloadBits 位
"""
import sys
from pathlib import Path
import os
ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "src"))
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int


def _tile_python(payload, pbits, start=0, min_blocks=1, max_blocks=4096, partial_ok=False):
    """切块。partial_ok=True 时,块头校验失败/位流错/尾部对齐残留不再丢弃
    整批,而是返回**已成功切出的前缀块**——0830 装备链路修复:reasm 重组的
    超 bunch(18k~37k bits)尾部有对齐残留,旧逻辑整批 return None 导致
    全员装备组件块丢失(44741 局 32 个重组 bunch 全灭)。调用方需容忍
    "块列表不完整"(已切出的块本身有效)。"""
    r = BitReader(payload, pbits)
    r.position = start
    out = []

    def _fail():
        return out if partial_ok and out else None

    try:
        while pbits - r.position >= 8:
            s = r.position
            has_rep = r.bit()
            is_actor = r.bit()
            sub = cls = None
            stably = None
            deleted = False
            if not is_actor:
                sub, _ = read_dfm_packed_int(r)
                stably = r.bit()
                if not stably:
                    cls, _ = read_dfm_packed_int(r)
                    if cls == 0:
                        deleted = True
            if deleted:
                out.append({"start": s, "has_rep": has_rep, "is_actor": is_actor,
                            "sub": sub, "stably": stably, "cls": cls, "deleted": True,
                            "num_bits": 0, "payload_start": r.position,
                            "payload_end": r.position})
                if len(out) > max_blocks: return None
                continue
            n, _ = read_dfm_packed_int(r)
            if n < 0 or r.position + n > pbits:
                if partial_ok:
                    # 重同步：当前块头非法（错位/残留），回退到本块起点后
                    # 移 1 bit 重新找块头。伪块由解码端 handle 校验天然挡住
                    #（status 非 exact 的 ev 不采），位游标单调前进不死循环
                    r.position = s + 1
                    continue
                return _fail()
            out.append({"start": s, "has_rep": has_rep, "is_actor": is_actor,
                        "sub": sub, "stably": stably, "cls": cls, "deleted": False,
                        "num_bits": n, "payload_start": r.position,
                        "payload_end": r.position + n})
            r.position += n
            if len(out) > max_blocks: return None
    except BitError:
        return _fail() if not partial_ok else (out if out else None)
    if len(out) < min_blocks: return None
    if pbits - r.position >= 8:
        return _fail()
    return out


try:
    from radar_server.native_codec import tile as _native_tile
except ImportError:
    _native_tile = None


def tile(payload, pbits, start=0, min_blocks=1, max_blocks=4096, partial_ok=False):
    if _native_tile is not None:
        result = _native_tile(payload, pbits, start, min_blocks, max_blocks, partial_ok)
        if result is not NotImplemented:
            return result
    return _tile_python(payload, pbits, start, min_blocks, max_blocks, partial_ok)
