"""Bounded, connection-local S2C discovery for live battle-flow detection."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import io
import json
import os
from pathlib import Path
import threading
import time

import numpy as np

import asset_crypto
from s2c_dec import decode_s2c
from codec.engine_packet import decode_engine_packet

ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[1])
REFERENCE = ROOT / "src/core" / "s2c_reference.npz"
STATE_DIR = ROOT / "src/core" / "flowcache"
_STATE_PURPOSE = asset_crypto.PURPOSE_FLOWCACHE   # 与随包资产不同的 keystream 用途
MAX_RECORDS = 1024
MAX_INDEX_BYTES = 512 * 1024
MIN_TRAINING = 96
MAX_DISK_STATES = 2048
DISK_STATE_TTL = 7 * 86400

# 置信度门槛（margin = 每个密钥字节「最优 vs 次优」差值的全局最小值，见 fit_candidate）。
#
# 为什么必须设：96 条（MIN_TRAINING）时拟合严重欠定，实测 margin 只有 0.47~1.9，
# 而 strict_probe 仍会放行（它只验 bunch 平铺 + Welcome + 序列，96 条自洽就能过）。
# 这种「勉强及格」的表一旦被判 ready 就落进 flowcache 并**永久粘住**（缓存键只有
# fingerprint = response.bin 前 64 字节 + meta.json，不含文件大小；flowlive 拿到
# ready 表后 _auto_s2c=False 也不再换），于是整局 S2C 解错 → 角色体 bunch 全被丢
# → 槽位无认领 → 全员「未知玩家」。实测 221559/1：错表 margin=1.37，全程只建出
# 3 个角色链接；用完整数据重判得正确表 margin=76.6，能建 16 个。
#
# 实测分布：错表 1.37；正确表 96 条 0.47~1.9、**128 条 ≥10**、400 条 33~62、
# 1024 条 76~185。故取 8.0 作接受门槛（把 96 条的欠定拟合挡在门外，代价是锁流
# 晚约 32 条记录），40.0 作「高置信」；未达 40 的决策在流量继续增长时会重判。
MIN_SCORE_MARGIN = 8.0
UPGRADE_MARGIN = 40.0
# 未达 UPGRADE_MARGIN 的表在流量增长后重判的最小间隔（秒）。重判要跑一次
# Walsh 拟合（1024 条约 0.1s），必须限频，不能每个 tick 都算。
UPGRADE_INTERVAL = 3.0


def _load_disk_cache_policy():
    """磁盘缓存开关（读+写，一起关）。

    优先级：
      1. 环境变量 ER_S2C_CACHE —— 运行期覆盖，`0/off/false/no` 关闭；
      2. 构建期注入的 src/core/s2c_cache.json（{"disk_cache": false}）——
         pack_src.py / build_exe.py 的 --no-s2c-cache 会写这个文件；
      3. 默认开。

    关掉 = 纯内存缓存：每次进程内只判一次（_cache 仍在），但不读也不写
    src/core/flowcache/。表是每连接独立的随机表，所以「不落盘」不等于「不能用」，
    只是每次都从本流的密文现拟合（约 0.1s），并且同一份归档在多个读入口之间
    不再共享结论。
    """
    env = os.environ.get("ER_S2C_CACHE")
    if env is not None:
        return env.strip().lower() not in ("0", "off", "false", "no", "")
    try:
        cfg = json.loads((ROOT / "src" / "core" / "s2c_cache.json").read_text(encoding="utf-8"))
        return bool(cfg.get("disk_cache", True))
    except Exception:
        return True


DISK_CACHE = _load_disk_cache_policy()
_cache = OrderedDict()
_lock = threading.RLock()
_flow_locks = tuple(threading.Lock() for _ in range(64))
_recovery_slots = threading.BoundedSemaphore(2)
_disk_lock = threading.Lock()
_last_prune = 0.0


@dataclass(frozen=True)
class Decision:
    mode: str
    table: bytes | None = None
    records: int = 0
    detail: str = ""
    elapsed_ms: float = 0.0
    retry_after: float = 0.15   # 非 ready 决策的最小重试间隔（秒）；拟合失败 30s
    margin: float = float("inf")   # 拟合置信度：bare 视为无限大（明文解析是确定性判据）
    size: int = 0               # 判定时的 response.bin 字节数（用于流量增长后重判）

    @property
    def ready(self):
        return self.mode in ("bare", "s2c")


def walsh(values):
    """Unnormalized Walsh-Hadamard transform along the last (256-value) axis."""
    result = np.asarray(values, dtype=np.float64).copy()
    width = result.shape[-1]
    if width == 0 or width & (width - 1):
        raise ValueError("Walsh transform width must be a positive power of two")
    stride = 1
    while stride < width:
        groups = result.reshape(*result.shape[:-1], -1, stride * 2)
        left = groups[..., :stride].copy()
        right = groups[..., stride:].copy()
        groups[..., :stride] = left + right
        groups[..., stride:] = left - right
        stride *= 2
    return result


def histogram(payloads, cipher=False):
    result = np.zeros((512, 256), dtype=np.float64)
    for payload in payloads:
        values = np.frombuffer(payload, dtype=np.uint8)
        if cipher and len(values):
            values = values ^ np.r_[np.uint8(0), values[:-1]]
        positions = np.arange(32, len(values))
        np.add.at(result, (positions & 511, values[positions]), 1)
    return result


@lru_cache(maxsize=1)
def reference_spectrum():
    with np.load(io.BytesIO(asset_crypto.read_bytes(REFERENCE)), allow_pickle=False) as archive:
        counts = archive["counts"].astype(np.float64)
    if counts.shape != (512, 256) or not np.isfinite(counts).all() or np.any(counts < 0):
        raise ValueError("invalid S2C reference histogram")
    if np.any(counts.sum(axis=1) < 8):
        raise ValueError("incomplete S2C reference coverage")
    spectrum = walsh(np.log((counts + 0.1) / (counts.sum(axis=1, keepdims=True) + 25.6)))
    spectrum.setflags(write=False)
    return spectrum


def fit_candidate(payloads, spectrum=None):
    counts = histogram(payloads, cipher=True)
    if np.any(counts.sum(axis=1) < 8):
        raise ValueError("insufficient long-packet coverage")
    if spectrum is None:
        spectrum = reference_spectrum()
    # XOR convolution computes all 512x256 likelihood scores with a few MB,
    # without building a 512x256x256 lookup tensor or starting BLAS workers.
    scores = walsh(walsh(counts) * spectrum) / 256
    sorted_scores = np.partition(scores, -2, axis=1)
    margin = float((sorted_scores[:, -1] - sorted_scores[:, -2]).min())
    if margin < 1e-6:
        raise ValueError("ambiguous table candidates")
    return scores.argmax(axis=1).astype(np.uint8).tobytes(), margin


def read_prefix(flow_dir):
    """Read at most 1024 S2C datagrams, never the full growing response file."""
    flow_dir = Path(flow_dir)
    with (flow_dir / "response.bin").open("rb") as blob, (
        flow_dir / "frames.jsonl"
    ).open("rb") as index:
        raw_index = index.read(MAX_INDEX_BYTES)
        size = os.fstat(blob.fileno()).st_size
        records = []
        for line in raw_index.split(b"\n")[:-1]:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if record.get("dir") != "response":
                    continue
                offset, length = int(record["offset"]), int(record["length"])
            except (ValueError, KeyError, TypeError):
                continue
            if offset < 0 or not 1 <= length <= 65535:
                continue
            if offset + length > size:
                break
            blob.seek(offset)
            payload = blob.read(length)
            if len(payload) != length:
                break
            records.append(payload)
            if len(records) >= MAX_RECORDS:
                break
    return records


def strict_probe(payloads, table=None):
    """Require complete bunch tiling and an intact downstream Welcome."""
    from core.handshake import _scan_bunch, NMT_WELCOME

    valid = controls = large = 0
    welcome = False
    sequences = []
    for sequence, raw in enumerate(payloads):
        plain = decode_s2c(raw, table) if table is not None else raw
        trace = decode_engine_packet(sequence, "response", plain)
        if trace.packet.packet_kind == "control":
            controls += 1
            continue
        parsed = trace.bunch_parse
        if trace.error or parsed is None or not parsed.tiled:
            return False, {"reason": "bunch validation", "validated": valid}
        valid += 1
        large += len(raw) > 512
        sequences.append(trace.packet.packet_sequence)
        for bunch in parsed.bunches:
            if bunch.channel_index == 0 and not bunch.partial:
                nmt, detail = _scan_bunch(bunch.payload, bunch.payload_bit_count)
                welcome |= nmt == NMT_WELCOME and bool(detail)
    bad_jumps = sum(
        ((b - a) & 16383) not in (0, 1, 2, 3, 16383)
        for a, b in zip(sequences, sequences[1:])
    )
    ok = (
        valid >= 4 and controls <= 16 and welcome and bad_jumps <= 10
        and (table is None or large >= 8)
    )
    return ok, {
        "validated": valid, "controls": controls, "large": large,
        "welcome": welcome, "bad_sequence_jumps": bad_jumps,
    }


def classify(payloads, spectrum=None):
    start = time.perf_counter()
    if not payloads:
        return Decision("pending", detail="waiting for response records")
    bare, detail = strict_probe(payloads[:32])
    if bare:
        return Decision("bare", records=len(payloads), detail="strict Welcome",
                        margin=float("inf"))
    ordinary = [p for p in payloads if 10 <= len(p) <= 1024]
    if len(ordinary) < MIN_TRAINING:
        return Decision("pending", records=len(payloads), detail="collecting S2C sample")
    # 0907 协议更新后明文首字节低位改为多值标志域（旧格式恒 0），原「p[0]&31
    # 单值 ≥90%」门不再成立（新流 5 个值）；且该域的奇偶性随连接密钥 K[0] 变化
    # （0905 金标准位偶数、0906 流位奇数），任何按 bit0 的廉价门都会误拒。
    # 故不再设廉价统计门：候选流（UDP≥2KB）数量有限，直接进拟合，最终以
    # fit+strict_probe（bunch 全平铺 + Welcome + 序列合法性）仲裁。
    if len(ordinary) < MIN_TRAINING:
        return Decision("pending", records=len(payloads), detail="collecting ordinary sample")
    try:
        table, margin = fit_candidate(ordinary[:-16], spectrum)
    except (ValueError, OSError) as exc:
        return Decision("pending", records=len(payloads), detail=str(exc),
                        retry_after=30.0)
    ok, proof = strict_probe(payloads, table)
    elapsed = round((time.perf_counter() - start) * 1000, 3)
    if not ok:
        return Decision("pending", records=len(payloads), detail=json.dumps(proof),
                        elapsed_ms=elapsed, retry_after=30.0)
    # 置信度门槛（见 MIN_SCORE_MARGIN 注释）：欠定拟合会被 strict_probe 放行，
    # 但绝不能就此落盘/冻结。返回 pending 让调用方等流量长大后再判。
    if margin < MIN_SCORE_MARGIN:
        return Decision(
            "pending", records=len(payloads),
            detail=json.dumps({**proof, "score_margin": margin, "holdout": 16,
                               "low_confidence": True}),
            elapsed_ms=elapsed, retry_after=0.5, margin=margin)
    return Decision(
        "s2c", table, len(payloads),
        json.dumps({**proof, "score_margin": margin, "holdout": 16}),
        elapsed, margin=margin,
    )


def state_path(flow_dir):
    key = hashlib.sha256(str(Path(flow_dir).resolve()).encode("utf-8")).hexdigest()[:24]
    return STATE_DIR / ("s2c_verified_v1_" + key + ".json")


def _stored_confidence(saved):
    """落盘状态里的 (margin, size)。

    旧格式没有顶层 margin/size，但 detail JSON 里一直带着 score_margin —— 从中取；
    取不到就返回 0.0（低于任何门槛 → 触发重判），绝不把不明置信度的表当可信。
    """
    margin = saved.get("margin")
    if margin is None:
        margin = 0.0
        try:
            detail = json.loads(saved.get("detail") or "{}")
            if "score_margin" in detail:
                margin = float(detail["score_margin"])
        except (ValueError, TypeError):
            pass
    try:
        margin = float(margin)
    except (TypeError, ValueError):
        margin = 0.0
    if saved.get("mode") == "bare":
        return float("inf"), int(saved.get("size") or 0)
    try:
        size = int(saved.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return margin, size


def _fingerprint(flow_dir):
    path = Path(flow_dir)
    with (path / "response.bin").open("rb") as source:
        head = source.read(64)
    meta = b""
    if (path / "meta.json").exists():
        with (path / "meta.json").open("rb") as source:
            meta = source.read(65536)
    return hashlib.sha256(head + meta).hexdigest()


def prune_disk_states(*, force=False):
    """Bound disposable verification caches without touching capture files."""
    global _last_prune
    now = time.time()
    with _disk_lock:
        if not force and now - _last_prune < 300:
            return
        _last_prune = now
        try:
            entries = []
            for path in STATE_DIR.glob("s2c_verified_v1_*.json"):
                try:
                    if not path.is_symlink():
                        entries.append((path.stat().st_mtime, path))
                except OSError:
                    continue
            entries.sort(reverse=True)
            for i, (mtime, path) in enumerate(entries):
                if i >= MAX_DISK_STATES or now - mtime > DISK_STATE_TTL:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
        except OSError:
            pass


def resolve_flow(flow_dir):
    """Serialize same-flow recovery; distinct connections never share tables."""
    flow_dir = Path(flow_dir).resolve()
    stripe = int(hashlib.sha256(str(flow_dir).encode()).hexdigest()[:8], 16) & 63
    with _flow_locks[stripe]:
        return _resolve_flow(flow_dir)


def _resolve_flow(flow_dir):
    """Cache only validated positive states, retry negatives as a flow grows."""
    flow_dir = Path(flow_dir).resolve()
    key = str(flow_dir)
    start = time.monotonic()
    try:
        fingerprint = _fingerprint(flow_dir)
        stat = (flow_dir / "response.bin").stat()
        index_stat = (flow_dir / "frames.jsonl").stat()
        stamp = (stat.st_size, index_stat.st_size)
    except OSError as exc:
        return Decision("pending", detail=str(exc))
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] == fingerprint:
            _, old_stamp, checked, decision = cached
            _cache.move_to_end(key)
            # 低置信判定不粘住：流量继续长大就重判（早期欠定拟合出的错表
            # margin 只有 1.37，冻结它会把整局角色流解废）。限频在
            # UPGRADE_INTERVAL 以上，避免每个 tick 都跑一次拟合。
            grown = (decision.mode == "s2c" and decision.margin < UPGRADE_MARGIN
                     and decision.size and stat.st_size > decision.size
                     and start - checked >= UPGRADE_INTERVAL)
            if not grown:
                if decision.ready or old_stamp == stamp or \
                        start - checked < decision.retry_after:
                    return decision
                if decision.records >= MAX_RECORDS:
                    return decision
    path = state_path(flow_dir)
    prior = None
    restored = False
    if DISK_CACHE:                      # 关闭磁盘缓存 = 不读不写，纯内存
        try:
            with path.open("rb") as source:
                saved = json.loads(asset_crypto.decrypt_blob(source.read(16384), _STATE_PURPOSE))
            if saved["fingerprint"] != fingerprint or saved["mode"] not in ("bare", "s2c"):
                raise ValueError("different connection")
            table = bytes.fromhex(saved["table"]) if saved["mode"] == "s2c" else None
            if table is not None and len(table) != 512:
                raise ValueError("invalid stored table")
            margin, size = _stored_confidence(saved)
            prior = Decision(saved["mode"], table, saved["records"], saved.get("detail", ""),
                             margin=margin, size=size)
            # 低置信落盘状态不可复用（旧版本/早期欠定拟合写进来的错表就藏在这里）；
            # 未达高置信且流量已长大也重判，让更充分的样本升级这张表。
            if saved["mode"] == "s2c" and margin < MIN_SCORE_MARGIN:
                raise ValueError("stale low-confidence table")
            if (saved["mode"] == "s2c" and margin < UPGRADE_MARGIN
                    and stat.st_size > max(size, 0) * 2 + 65536):
                raise ValueError("flow grown; re-verify low-confidence table")
            decision = prior
            restored = True
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if not restored:
        try:
            with _recovery_slots:
                decision = classify(read_prefix(flow_dir))
        except (OSError, ValueError, IndexError) as exc:
            decision = Decision("pending", detail=str(exc))
        # 重判不 ready 时优先保留旧表（可用性优先），但绝不复用低于门槛的表。
        if not decision.ready and prior is not None and prior.ready \
                and prior.margin >= MIN_SCORE_MARGIN:
            with _lock:            # 刷新 checked，让 UPGRADE_INTERVAL 生效
                _cache[key] = (fingerprint, stamp, start, prior)
                _cache.move_to_end(key)
            return prior
        if decision.ready:
            if decision.mode == "s2c" and decision.size == 0:
                decision = Decision(decision.mode, decision.table, decision.records,
                                    decision.detail, decision.elapsed_ms,
                                    decision.retry_after, decision.margin,
                                    stat.st_size)
            if not DISK_CACHE:
                with _lock:
                    _cache[key] = (fingerprint, stamp, start, decision)
                    _cache.move_to_end(key)
                    while len(_cache) > 128:
                        _cache.popitem(last=False)
                return decision
            temporary = None
            try:
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
                body = json.dumps({
                    "fingerprint": fingerprint, "mode": decision.mode,
                    "table": decision.table.hex() if decision.table else None,
                    "records": decision.records, "detail": decision.detail,
                    "elapsed_ms": decision.elapsed_ms,
                    "margin": (None if decision.margin == float("inf")
                               else float(decision.margin)),
                    "size": int(decision.size or 0),
                }).encode("utf-8")
                # 这张表就是该连接的解密钥，落盘必须加密 —— 不能留明文给顺手拷走的人
                temporary.write_bytes(asset_crypto.encrypt(body, _STATE_PURPOSE))
                os.replace(temporary, path)
            except OSError:
                pass
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
            prune_disk_states()
    with _lock:
        _cache[key] = (fingerprint, stamp, start, decision)
        _cache.move_to_end(key)
        while len(_cache) > 128:
            _cache.popitem(last=False)
    return decision
