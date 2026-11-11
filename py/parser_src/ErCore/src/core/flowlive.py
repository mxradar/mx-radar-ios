# -*- coding: utf-8 -*-
"""flowlive.py - 增量 flow 加载器（对接 light_relay 实时抓包）。

与 flowload.load() 完全同构（同一 reassembler/unwrapper/head 逻辑），但：
  - 保持 PartialBunchReassembler / ReliableSequenceUnwrapper / head 状态跨调用；
  - 只读取 frames.jsonl 与 response.bin 的新增部分，且响应字节**按需增长**
    （只覆盖当前待处理帧的偏移——探测一个 14MB 的流不再把整文件拉进内存）；
  - 空闲轮自动压实响应缓冲前缀（长对局内存 O(窗口) 而非 O(会话)）;
  - 产出与 load() 相同的 b: {channel -> [LB, ...]}，并维护 seq->ts 映射。

light_relay 每 1s flush 一次（先 response.bin 后 frames.jsonl），故读到新 frame 行时
其 response 字节必已落盘；仍做长度防御，未就绪则延后该帧。
"""
from __future__ import annotations
import json
import time
from collections import deque
from pathlib import Path

from flowload import LB
from codec.engine_packet import decode_engine_packet
from codec.engine_stream import PartialBunchReassembler, ReliableSequenceUnwrapper
import handshake as hs_mod
import c2s_self

RESP_KEEP_TAIL = 262144      # 压实时保留的尾部安全边距（字节）
RESP_COMPACT_MIN = 8 << 20   # 缓冲超过该值才触发压实
RESP_GROW_CHUNK = 4 << 20    # 按需增长的批量粒度（减少追赶期 syscall）
INDEX_READ_CHUNK = 256 * 1024
MAX_INDEX_LINE = 4096


class IncrementalFlowLoader:
    def __init__(self, flow_dir, *, s2c_table=None, auto_s2c=False, c2s_self=False, request_prefix=True):
        self.flow_dir = Path(flow_dir)
        self.request_prefix = request_prefix
        self.packet_errors = 0
        self.pose_samples = 0
        if s2c_table is not None and len(s2c_table) != 512:
            raise ValueError("s2c_table must be 512 bytes")
        self.s2c_table = bytes(s2c_table) if s2c_table is not None else None
        self._decode_s2c = None
        self._auto_s2c = bool(auto_s2c and s2c_table is None)
        self.s2c_mode = "pending" if self._auto_s2c else (
            "s2c" if self.s2c_table is not None else "bare"
        )
        if self.s2c_table is not None:
            from s2c_dec import decode_s2c
            self._decode_s2c = decode_s2c
        self.reasm = PartialBunchReassembler()
        self.unw = ReliableSequenceUnwrapper()
        self.head = {}            # channel -> [hx, hm, seq]
        self.b = {}               # channel -> [LB, ...]（与 load() 同构）
        # C2S 自机位姿（可选）：上行 ch3 的 parse_fgpmove_params 带自机
        # 位置/朝向/俯仰。用**独立的**重组器与 head，绝不与下行共用——
        # 两个方向的 partial 链和可靠序列是各自独立的，混用会互相污染。
        self.c2s_self = bool(c2s_self)
        self._c2s_every = 4       # 上行帧抽稀：每 N 帧解一次自机位姿
        self._req_seen = 0
        self.reasm_req = PartialBunchReassembler()
        self.head_req = {}
        self.self_pose = None     # 最新自机位姿（见 c2s_self.parse_fgp）
        self.self_pose_t = 0.0    # 该位姿的墙钟时刻（判陈旧用）
        self._c2s_pose_start = None
        self.seq_ts = []          # [(seq, ts)] response 帧时间戳
        self.frames_pos = 0
        self.resp_len = 0         # 已读入内存的文件尾偏移（绝对）
        self.resp_base = 0        # _resp[0] 对应的文件偏移（压实时推进）
        self.resp_consumed = 0    # 已处理 response 帧的最高文件尾偏移
        self._stat_size = None    # response.bin 大小缓存（只追加文件，安全下界）
        self._resp = bytearray()
        self._req = bytearray()   # request.bin 常驻内存（仅握手期使用）
        self.req_len = 0
        self._tail = b""
        self._discard_line = False
        self._pending = deque()   # 已解析未处理的帧记录 (dir,off,length,seq,ts)
        self.n_frames = 0
        self.n_lb = 0
        # ---- 控制通道握手跟踪（引擎级战斗流判据，与游戏客户端一致）----
        self.hs = hs_mod.Handshake()
        self.n_req_decoded = 0    # 已解码的 request 帧数（握手完成后停止解码）
        self.ctrl_done = False    # 握手已收敛（welcome+join 齐或帧数超限）

    # ---- response 字节：按需增长 + 空闲压实 ----
    def _ensure_resp(self, end):
        """保证内存缓冲覆盖文件偏移 [.., end)。返回 False=文件还没这么大。

        快路径 end≤已缓冲 → 零 syscall（原实现 stat 在快路径判断之前，
        追赶期每包一次 stat，3 万帧=3 万次）；余下走 _stat_size 缓存——
        response.bin 只追加不收缩，缓存的 size 下界恒安全，end 超过缓存
        才真正 stat（文件增长实时可见）。"""
        if end <= self.resp_len:
            return True
        size = self._stat_size
        if size is None or end > size:
            try:
                size = (self.flow_dir / "response.bin").stat().st_size
            except OSError:
                return False
            self._stat_size = size
        if size < end:
            return False
        target = min(size, max(end, self.resp_len + RESP_GROW_CHUNK))
        with (self.flow_dir / "response.bin").open("rb") as fh:
            fh.seek(self.resp_len)
            self._resp += fh.read(target - self.resp_len)
        self.resp_len = target
        return True

    def _resp_bytes(self, off, length):
        lo = off - self.resp_base
        hi = lo + length
        if lo < 0 or hi > len(self._resp):
            return None
        return bytes(self._resp[lo:hi])

    def _compact_resp(self):
        """空闲且缓冲过大时丢弃已消费前缀（保留尾部安全边距）。"""
        compact_end = min(self.resp_consumed, self.resp_len - RESP_KEEP_TAIL)
        excess = compact_end - self.resp_base
        if excess < RESP_COMPACT_MIN:
            return
        del self._resp[:excess]
        self.resp_base = compact_end

    def _req_bytes(self, off, length):
        try:
            with (self.flow_dir / "request.bin").open("rb") as fh:
                fh.seek(off)
                raw = fh.read(length)
        except OSError:
            return None
        return raw if len(raw) == length else None

    def _read_new_lines(self):
        """把 frames.jsonl 新增行解析为帧记录入队（不读 response 字节）。"""
        if self._pending:
            return
        try:
            fsize = (self.flow_dir / "frames.jsonl").stat().st_size
        except OSError:
            return
        if fsize <= self.frames_pos:
            return
        with (self.flow_dir / "frames.jsonl").open("rb") as fh:
            fh.seek(self.frames_pos)
            data = fh.read(min(INDEX_READ_CHUNK, fsize - self.frames_pos))
            self.frames_pos += len(data)
        if self._discard_line:
            boundary = data.find(b"\n")
            if boundary < 0:
                return
            data = data[boundary + 1:]
            self._discard_line = False
        data = self._tail + data
        lines = data.split(b"\n")
        self._tail = lines.pop()
        if len(self._tail) > MAX_INDEX_LINE:
            self._tail = b""
            self._discard_line = True
        for line in lines:
            line = line.strip()
            if not line or len(line) > MAX_INDEX_LINE:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            try:
                d = rec.get("dir")
                off, length = int(rec["offset"]), int(rec["length"])
                seq, ts = int(rec.get("seq", 0)), float(rec.get("ts", 0.0))
                if d not in ("request", "response") or off < 0 or not 1 <= length <= 65535:
                    continue
                self._pending.append((d, off, length, seq, ts))
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue

    def _track_ctrl(self, direction, bp):
        """增量跟踪 ch0 控制 bunch（握手状态机）。request 帧只在握手未收敛时
        解码——握手必在连接最初几帧内完成，之后 request 解码全部跳过。"""
        for bn in bp.bunches:
            if bn.error or bn.channel_index != 0 or bn.partial \
                    or bn.payload_bit_count < 8 or not bn.payload:
                continue
            nmt, det = hs_mod._scan_bunch(bn.payload, bn.payload_bit_count)
            if nmt is None:
                continue
            if nmt == hs_mod.NMT_HELLO:
                self.hs.hello = True
            elif nmt == hs_mod.NMT_CHALLENGE and det:
                self.hs.challenge = det
            elif nmt == hs_mod.NMT_LOGIN:
                self.hs.login = True
            elif nmt == hs_mod.NMT_WELCOME and det:
                if self.hs.welcome is None:
                    self.hs.welcome = det
                    self.hs.welcome_frame = self.n_frames
                self.hs.rounds += 1
            elif nmt == hs_mod.NMT_JOIN:
                self.hs.join = True
            elif nmt == hs_mod.NMT_FAILURE and self.hs.failure is None:
                self.hs.failure = det

    def _ctrl_converged(self):
        """握手是否已收敛：引擎门(welcome)已过且 Join 已发，或帧数超上限。"""
        return (self.hs.welcome is not None and self.hs.join) \
            or self.n_frames >= hs_mod.HS_MAX_FRAMES \
            or self.n_req_decoded >= hs_mod.HS_MAX_FRAMES

    def _process_one(self, rec):
        d, off, length, seq, ts = rec
        if d == "request":
            raw = self._req_bytes(off, length)
        else:
            if not self._ensure_resp(off + length):
                return -1
            raw = self._resp_bytes(off, length)
            self.resp_consumed = max(self.resp_consumed, off + length)
        if raw is None:
            return -1
        return self.consume_packet(seq, d, raw, ts)

    def consume_packet(self, seq, d, raw, ts):
        """Consume one packet from either a file or the V5 plaintext adapter."""
        if d == "request":
            # 握手跟踪需要 C->S 方向（Hello/Login/Join）；收敛后就不再解码了。
            # 但开了 c2s_self 要一直解：自机位姿（位置/朝向/**俯仰**）只有上行有。
            if self.ctrl_done and not self.c2s_self:
                return 0
            # 抽稀必须放在**解码之前**：成本几乎全在 decode_engine_packet + 组包，
            # 不在位姿解析本身（实测全解 11.9k LB/s、关掉 30.7k，只抽稀位姿解析
            # 几乎没救回来）。握手未收敛时不能抽（那时要靠每个上行帧判引擎门）。
            if self.ctrl_done and self.c2s_self:
                self._req_seen += 1
                if self._req_seen % self._c2s_every:
                    return 0
            self.n_req_decoded += 1
            try:
                tr = decode_engine_packet(seq, "request", raw, request_prefix=self.request_prefix, max_packet_bytes=1024)
            except Exception:
                self.packet_errors += 1
                return 0
            bp = tr.bunch_parse
            if tr.error or (bp is not None and bp.error):
                self.packet_errors += 1
                return 0
            if bp is None:
                return 0
            if not self.ctrl_done:
                if not tr.error and not bp.error:
                    self._track_ctrl("request", bp)
                self.ctrl_done = self._ctrl_converged()
            if self.c2s_self:
                self._c2s_self_from_bunches(seq, bp, timestamp=ts)
            return 0
        # response
        self.n_frames += 1
        self.seq_ts.append((seq, ts))
        try:
            if self.s2c_table is not None:
                raw = self._decode_s2c(raw, self.s2c_table)
            tr = decode_engine_packet(seq, "response", raw, response_frame_time_capable=False, max_packet_bytes=1024)
        except Exception:
            self.packet_errors += 1
            return 0
        bp = tr.bunch_parse
        if tr.error or bp is None:
            self.packet_errors += 1
            return 0
        if bp.error:
            self.packet_errors += 1
        if not self.ctrl_done:
            if not bp.error:
                self._track_ctrl("response", bp)
            self.ctrl_done = self._ctrl_converged()
        added = 0
        for bn in bp.bunches:
            if bn.error:
                continue
            self.unw.apply(bn)
            ci = bn.channel_index
            starts = (not bn.partial) or bool(bn.partial_initial)
            if starts or ci not in self.head:
                self.head[ci] = [0, 0, seq]
            self.head[ci][0] |= int(bn.has_package_map_exports)
            self.head[ci][1] |= int(bn.has_must_be_mapped_guids)
            for log in self.reasm.feed(bn, source={}):
                hx, hm, s0 = self.head.pop(ci, [0, 0, seq])
                frags = [(int(f["combined_payload_interval"][0]),
                          int(f.get("has_package_map_exports", 0)),
                          int(f.get("has_must_be_mapped_guids", 0)))
                         for f in log.fragments]
                self.b.setdefault(ci, []).append(LB(
                    seq=s0, ch=ci, payload=log.payload,
                    pbits=log.payload_bit_count, open=int(log.open),
                    close=int(log.close), hx=hx, hm=hm,
                    partial=int(log.was_partial), rel=int(log.reliable),
                    close_reason=log.close_reason, dormant=log.dormant,
                    frags=frags))
                self.n_lb += 1
                added += 1
        return added

    def enable_c2s_self(self, every: int = 1):
        """开始解上行自机位姿（探测阶段不用开：探测只看握手门，开了等于每个候选流
        都白烧一份 CPU，tick 会被拖慢）。视图接管时调用。

        默认 **不抽稀**（every=1）：实测 ch3 里只有约 20% 的帧是自机位姿消息，
        抽稀 4 帧会把位姿率从 6.6Hz 打到 1.6Hz（621ms 一个样本）—— 那是第一视角
        俯仰"顿 + 延迟"的根源。整局代价实测 5.2s→9.3s（763s 素材），摊到实时
        约 0.5% CPU，只在锁定的视图上跑，可以忽略。"""
        self.c2s_self = True
        self._c2s_every = max(1, int(every))

    def _c2s_self_from_bunches(self, seq, bp, timestamp=None):
        """上行 bunch -> 独立重组 -> ch3 自机位姿。

        只关心 ch3（parse_fgpmove_params 所在通道）；其余通道直接跳过，别把
        重组器状态喂大。单条产不出位姿是常态（同通道还有别的消息），
        靠 parse_fgp 自己的结构校验过滤。
        """
        for bn in bp.bunches:
            if bn.channel_index != 3:
                continue
            if bn.error and not bn.payload:
                continue
            starts = (not bn.partial) or bool(bn.partial_initial)
            if starts or 3 not in self.head_req:
                self.head_req[3] = [0, 0, seq]
            self.head_req[3][0] |= int(bn.has_package_map_exports)
            self.head_req[3][1] |= int(bn.has_must_be_mapped_guids)
            try:
                logs = self.reasm_req.feed(bn, source={})
            except Exception:
                self.head_req.pop(3, None)
                continue
            for log in logs:
                self.head_req.pop(3, None)
                # Both recorded layouts pass the same strict structure, bounds,
                # and exact-end checks. Cache the successful variant per flow.
                preferred = self._c2s_pose_start
                starts = (preferred, 53 if preferred == 45 else 45) if preferred in (45, 53) else (45, 53)
                pose = None
                for start in starts:
                    pose = c2s_self.parse_fgp(log.payload, log.payload_bit_count, start=start)
                    if pose:
                        self._c2s_pose_start = start
                        pose['wire_start_bit'] = start
                        break
                if pose:
                    self.pose_samples += 1
                    pose["seq"] = seq
                    self.self_pose = pose
                    self.self_pose_t = timestamp if timestamp is not None else time.time()

    def poll(self, max_frames=None):
        """处理新增帧，返回新增 LB 数量。max_frames 限制单次处理的帧数（分块）。"""
        if self._auto_s2c:
            from s2c_auto import resolve_flow
            decision = resolve_flow(self.flow_dir)
            self.s2c_mode = decision.mode
            if not decision.ready:
                return 0
            self.s2c_table = decision.table
            if self.s2c_table is not None:
                from s2c_dec import decode_s2c
                self._decode_s2c = decode_s2c
            self._auto_s2c = False
        self._read_new_lines()
        added = 0
        budget = max_frames if max_frames is not None else (1 << 30)
        retried = False
        while self._pending and budget > 0:
            rec = self._pending[0]
            r = self._process_one(rec)
            if r < 0:
                retried = True
                break  # 字节未落盘：留在队首，下次再试
            self._pending.popleft()
            added += r
            budget -= 1
        if not self._pending and not retried:
            self._compact_resp()   # 队列清空才压实（无重试竞争）
        return added

    def exhausted(self):
        """frames.jsonl 已全部读入且无待处理行。"""
        if self._auto_s2c:
            return False
        try:
            fsize = (self.flow_dir / "frames.jsonl").stat().st_size
        except OSError:
            fsize = 0
        return self.frames_pos >= fsize and not self._pending
