
# -*- coding: utf-8 -*-
"""flowload.py - reusable, correct per-logical-bunch loader for a er flow dir.

Returns, per channel index, a list of LogicalBunch records where the bunch-header
flags (bHasPackageMapExports / bHasMustBeMappedGUIDs / bOpen / bClose) are taken
from the FIRST fragment of the reassembled chain, which is where the engine puts
the export / must-be-mapped sections.
"""
import json, sys
import hashlib
from dataclasses import dataclass
from pathlib import Path
import os
ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "src"))
import asset_crypto
from codec.engine_packet import decode_engine_packet
from codec.engine_stream import PartialBunchReassembler, ReliableSequenceUnwrapper


@dataclass
class LB:
    seq: int
    ch: int
    payload: bytes
    pbits: int
    open: int
    close: int
    hx: int
    hm: int
    partial: int
    rel: int
    close_reason: int | None = None
    dormant: int = 0
    frags: list | None = None   # [(start_bit, hx, hm), ...] 每 fragment 的起点 + flags


_AUTO_S2C = object()


def _explicit_s2c_table(direction, table):
    if direction == "response" and table is not _AUTO_S2C and table is not None:
        if len(table) != 512:
            raise ValueError("s2c_table must be 512 bytes")
        return bytes(table)
    return table


def load(flow_dir, direction="response", *, s2c_table=_AUTO_S2C):
    s2c_table = _explicit_s2c_table(direction, s2c_table)
    flow_dir = Path(flow_dir)
    blob = (flow_dir / ("response.bin" if direction == "response" else "request.bin")).read_bytes()
    frames = []
    for line in (flow_dir / "frames.jsonl").open(encoding="utf-8"):
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("dir") == direction:
            frames.append(rec)
    reasm = PartialBunchReassembler()
    unw = ReliableSequenceUnwrapper()
    head = {}          # channel -> [hx, hm, seq] OR-ed across the chain's fragments
    out = {}
    s2c_mode = "bare"
    if direction == "response":
        if s2c_table is _AUTO_S2C:
            try:
                s2c_mode, s2c_table = _s2c_classify(flow_dir, blob, frames)
            except Exception:
                s2c_mode, s2c_table = ("bare", None)
        elif s2c_table is not None:
            s2c_mode = "s2c"
    for rec in frames:
        data = blob[rec["offset"]:rec["offset"] + rec["length"]]
        if s2c_mode == "s2c":
            try:
                import s2c_dec
                data = s2c_dec.decode_s2c(data, s2c_table)
            except Exception:
                pass
        seq = int(rec.get("seq", 0))
        try:
            tr = decode_engine_packet(seq, direction, data)
        except Exception:
            continue
        bp = tr.bunch_parse
        if tr.error or bp is None or bp.error:
            continue
        for bn in bp.bunches:
            if bn.error:
                continue
            unw.apply(bn)
            ci = bn.channel_index
            starts = (not bn.partial) or bool(bn.partial_initial)
            if starts or ci not in head:
                head[ci] = [0, 0, seq]
            # UE writes the NetGUID export section in the FIRST fragment and the
            # must-be-mapped GUID list in a later one; the reassembled payload is
            # their concatenation, so the logical bunch carries both sections.
            head[ci][0] |= int(bn.has_package_map_exports)
            head[ci][1] |= int(bn.has_must_be_mapped_guids)
            for log in reasm.feed(bn, source={}):
                hx, hm, s0 = head.pop(ci, [0, 0, seq])
                # 每 fragment 的起点 + flags（partial bunch 可含多个 exports 段）
                frags = [(int(f["combined_payload_interval"][0]),
                          int(f.get("has_package_map_exports", 0)),
                          int(f.get("has_must_be_mapped_guids", 0)))
                         for f in log.fragments]
                out.setdefault(ci, []).append(LB(
                    seq=s0, ch=ci, payload=log.payload, pbits=log.payload_bit_count,
                    open=int(log.open), close=int(log.close), hx=hx, hm=hm,
                    partial=int(log.was_partial), rel=int(log.reliable),
                    close_reason=log.close_reason, dormant=log.dormant,
                    frags=frags))
    return out


CACHE_DIR = ROOT / "src/core" / "flowcache"
CACHE_VER = "v4s2c"  # v4s2c: S2C 加密对局自动去混淆（512B 循环表+密文反馈, 见 s2c_dec.py）


# ---------------------------------------------------------------- S2C 去混淆 hook
S2C_REF_PATH = ROOT / "src/core" / "s2c_ref_dist.json"
_S2C_REF = None


def _s2c_ref():
    """裸流位置明文计数 (512+512 位置 × 256 值) -> 对数概率表 (惰性加载)."""
    global _S2C_REF
    if _S2C_REF is None:
        import math
        d = json.loads(asset_crypto.read_text(S2C_REF_PATH))
        ref = []
        for i, pairs in enumerate(d["dist"]):
            tot = sum(c for _, c in pairs) + 76.8
            tbl = [-20.0] * 256
            for v, c in pairs:
                tbl[v] = math.log((c + 0.3) / tot)
            ref.append(tbl)
        _S2C_REF = ref
    return _S2C_REF


def _s2c_state_path(flow_dir):
    import hashlib
    key = hashlib.md5(str(flow_dir).encode("utf-8", "replace")).hexdigest()[:20]
    return CACHE_DIR / ("s2c_state_" + key + ".json")


def _write_enc(path, obj, purpose):
    """运行期缓存一律加密落盘 — 里面有恢复出的表和去混淆后的载荷，明文等于送密钥。"""
    path.write_bytes(asset_crypto.encrypt(
        json.dumps(obj, ensure_ascii=False).encode("utf-8"), purpose))


def _s2c_probe(frames, table=None):
    """对若干响应帧做 engine 正常包率探测. table=None 时按原始字节, 否则先去混淆."""
    import s2c_dec
    ok = 0; tot = 0
    for d in frames:
        tot += 1
        if table is not None:
            d = s2c_dec.decode_s2c(d, table)
        try:
            tr = decode_engine_packet(0, "response", d, request_prefix=False)
            if tr.error or tr.packet is None:
                continue
            bp = tr.bunch_parse
            if bp is not None and not getattr(bp, "error", None) and bp.bunches:
                ok += 1
        except Exception:
            pass
    return ok, tot


def _s2c_classify(flow_dir, blob, frames):
    """返回 (mode, table): mode in {"bare","s2c"}; 仅 UDP 响应流参与判定, 结果按流缓存."""
    meta_p = Path(flow_dir) / "meta.json"
    proto = "tcp"
    try:
        proto = json.loads(meta_p.read_text(encoding="utf-8")).get("proto", "tcp")
    except Exception:
        pass
    if proto != "udp":
        return "bare", None
    sp = _s2c_state_path(flow_dir)
    if sp.exists():
        try:
            st = json.loads(asset_crypto.decrypt_blob(sp.read_bytes(), asset_crypto.PURPOSE_FLOWCACHE))
            if st.get("mode") == "s2c":
                return "s2c", st.get("table")
            return "bare", None
        except Exception:
            pass
    probe_frames = []
    rec_frames = []
    for rec in frames:
        if rec.get("dir") != "response":
            continue
        d = blob[rec["offset"]:rec["offset"] + rec["length"]]
        if len(d) >= 40 and len(probe_frames) < 12:
            probe_frames.append(d)
        if len(d) >= 10 and len(rec_frames) < 300:
            rec_frames.append(d)
    ok, tot = _s2c_probe(probe_frames)
    if tot and ok >= tot * 0.6:
        _write_enc(sp, {"mode": "bare"}, asset_crypto.PURPOSE_FLOWCACHE)
        return "bare", None
    table = None
    try:
        import s2c_dec
        table = s2c_dec.recover_table_scored(rec_frames, _s2c_ref())
        ok2, tot2 = _s2c_probe(probe_frames, table)
    except Exception:
        table = None; ok2 = 0; tot2 = 1
    if tot2 and ok2 >= tot2 * 0.6:
        _write_enc(sp, {"mode": "s2c", "table": list(table)}, asset_crypto.PURPOSE_FLOWCACHE)
        return "s2c", table
    _write_enc(sp, {"mode": "bare"}, asset_crypto.PURPOSE_FLOWCACHE)
    return "bare", None


def load_cached(flow_dir, direction="response", *, s2c_table=_AUTO_S2C):
    s2c_table = _explicit_s2c_table(direction, s2c_table)
    flow_dir = Path(flow_dir)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # key 只留可读尾段 + 路径哈希：早先整条绝对路径拼进文件名，放在桌面上这种
    # 深目录里路径就超了 Windows 的 260 字符上限，写缓存直接 FileNotFoundError
    # （整个 BattleView 构建线程跟着挂掉，看起来像"这条流解不开"）。
    stem = "%s_%s_%s_%s" % (flow_dir.parent.parent.name[:24], flow_dir.name, direction, CACHE_VER)
    key = hashlib.md5(str(flow_dir).encode("utf-8", "replace")).hexdigest()[:16] + "_" + stem
    if direction == "response" and s2c_table is not _AUTO_S2C:
        key += "_explicit_bare" if s2c_table is None else "_explicit_s2c_" + hashlib.sha256(s2c_table).hexdigest()[:16]
    p = CACHE_DIR / (key + ".json")
    if p.exists():
        d = json.loads(asset_crypto.decrypt_blob(p.read_bytes(), asset_crypto.PURPOSE_FLOWPARSE))
        return {int(k): [LB(v[0], int(k), bytes.fromhex(v[1]), v[2], v[3], v[4], v[5], v[6], v[7], v[8], v[9], v[10],
                            v[11] if len(v) > 11 else None)
                         for v in vs] for k, vs in d.items()}
    b = load(flow_dir, direction, s2c_table=s2c_table)
    _write_enc(p, {str(ch): [[x.seq, x.payload.hex(), x.pbits, x.open, x.close, x.hx, x.hm, x.partial, x.rel, x.close_reason, x.dormant, x.frags]
                            for x in vs] for ch, vs in b.items()}, asset_crypto.PURPOSE_FLOWPARSE)
    return b
