# -*- coding: utf-8 -*-
"""er_radar.py - 网络层地图雷达（确定性，无启发式/无扫描）。

数据流:
  1. 战斗流自识别: 扫描 captures 全部 run，逐 flow 统计 PlayerState(arch=131) 通道数，
     取最大者为该 run 的战斗流（与 er_live.detect_battle_server 同一确定性标准）。
     计数持久化 src/core/radar_detect_cache.json；最新 run 优先、flow 按 mtime 新->旧，
     命中 n_ps>=3 即预锁定出图，后台索引线程扫完全部 run 收敛到最大值；
     周期复扫新 run / 新 flow（实时抓包自动跟随），战斗流增长即全量重建。
  2. 确定性解析（复用 er_live 管线）:
     PlayerState(arch=131) -> 名字/player_id/owner/actor
     角色spawn(arch=105)   -> h14/h18 绑定 + SNA loc + spawn_seq
     MovementReplicationActor(handle=21) -> 每 slot 逐帧 (seq,ts,x,y,z,yaw)
     join: 角色出生点 <-> slot@spawn_seq (<500cm) 最小距离唯一认领;
     15 slots = 14 远程 + 1 self: 唯一未认领 slot 即本地玩家轨迹（封闭性）。
  3. 回放: seq 游标按真实时间映射推进（frames.jsonl ts 拟合 seq/sec），
     速度可调；/api/state 与 tools/web_radar.py 同构（self/entities/world/rel/names）。

Usage: python src/core/er_radar.py [--run RUN] [--port 8888]
"""
import argparse, bisect, json, shutil, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, unquote

import numpy as np

import os
ROOT = Path(os.environ.get("ER_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "src/core"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
_DEBUG = os.environ.get("ER_DEBUG") == "1"   # 调试日志总开关（默认关，正式包静默）

from flowload import load_cached, load
import asset_crypto
from blocks import tile
from codec.bitstream import BitReader, BitError
from codec.engine_bunch import read_dfm_packed_int
import sna
import er_live as nl
import repmove4 as rm4
import handshake as hs_mod
import state_track as st
import field13 as f13
from weapon_state import weapon_fields
from death_state import DeathStateTracker
from death_box_state import DeathBoxTracker, box_entity
from box_contents import BoxContentsTracker
from liveview import LOOT_NAMES, confirmed_player_bot
from playerstate_protocol import PlayerStateTracker
from liveview import WEAPON_NAMES, loot_display_fields
from liveview import (LiveView, LiveDetector, extract_loot_full,
                      mark_picked_by_standing, LOOT_PICK_INFER, HERO_NAMES)

DETECT_CACHE = ROOT / "src/core" / "radar_detect_cache.json"
# 坐标合法性判据：与 liveview.WORLD 同源（引擎级 sanity，非地图边界）。
# 见 liveview.py 处的完整说明——硬编码地图窗每上新图必失效，2026-08-29 新图
# (Y∈[-226k,-152k]) 曾使整局样本 100% 被丢。此处直接复用，避免两处判据分叉。
from liveview import WORLD, WORLD_MAX  # noqa: E402  (判据单一来源)

# ---------------------------------------------------------------- 3D 静态资源
# web/m3d/ 由 src/core/build_m3d.py 产出：<mapkey>.glb（白模底图）+ r3d.js（3D 引擎）
# + three.module.js + manifest.json。雷达页按需 import("/m3d/r3d.js")；
# 模型 16~99MB，靠 immutable 长缓存做到一张图只真下一次；.gz 边车把线上
# 字节再压 1/3（手机流量）。改 r3d.js 后必须同步 bump PAGE 里的 ?v= 串。
M3D_DIR = ROOT / "web" / "m3d"
# 干员头像：web/avatars/<干员名>.png（由资源包 resources/operators 复制而来）
AVATAR_DIR = ROOT / "web" / "avatars"
# Korr 前端（第三方 UI）：整棵放在 web/korr/，radar.html + static/{css,js,maps,tx}。
# 由 ER_UI 决定首页用哪套：korr（默认）= Korr 雷达页，er = 本文件内置的老页面。
# 老页面始终可从 /er 打开，两套共用同一个 /api/state。
KORR_DIR = ROOT / "web" / "korr"
KORR_UI = False          # 自用包：只有内置页，Korr 前端不在包里
# Korr 3D 视图要的地形三角形汤（web/korr/maps3d/<key>.f32.gz + manifest.json，
# 由 tools/m3d_to_korr3d.py 从 web/m3d 的地形白模导出）。它的页面按
# /api/map/<地图名>/meta 与 /api/map/<地图名> 两份接口取，见 korr3d_map()。
KORR3D_DIR = KORR_DIR / "maps3d"
M3D_MIME = {
    ".glb": "model/gltf-binary",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".bin": "application/octet-stream",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def m3d_serve(handler, path, cache_s=604800, prefix="/m3d/", root=None, no_cache=False):
    """把 /m3d/<file> 交给静态目录。命中（含 404）返回 True，非本前缀返回 False。

    分块写出，避免几十 MB 模型在内存里再拷一份；带 ETag/If-None-Match，
    来回切图不会重复下载。
    同名 .gz 边车（mtime 不旧于本体）+ 客户端接受 gzip 时直接发压缩体，
    GLB 线上字节 -1/3；URL 带 ?v= 内容版本的一律 immutable——GLB 的 ?v=rev
    与 r3d.js 的 ?v=串换内容必换 URL，长缓存才不会挡住更新。
    """
    root = root or M3D_DIR
    if not path.startswith(prefix):
        return False
    rel = unquote(path[len(prefix):])      # 中文文件名（干员头像）需先解码
    if not rel or rel.endswith("/"):
        rel = "index.html"
    try:
        f = (root / rel).resolve()
        f.relative_to(root.resolve())             # 目录穿越防护
    except (ValueError, OSError):
        handler.send_response(403)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True
    suf = f.suffix.lower()
    pre_gz = False
    if not f.is_file():
        # 独立版只带 .gz 模型（体积 1/3）：明文缺位时按 gzip 体直出
        gz_only = f.with_name(f.name + ".gz")
        if gz_only.is_file():
            f = gz_only
            pre_gz = True
        else:
            handler.send_response(404)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
    st_ = f.stat()
    etag = '"%x-%x"' % (int(st_.st_mtime), st_.st_size)
    body = f
    if not pre_gz and "gzip" in (handler.headers.get("Accept-Encoding") or "").lower():
        gz = f.with_name(f.name + ".gz")
        try:
            gz_st = gz.stat()
            if gz_st.st_mtime >= st_.st_mtime - 1:   # 边车过期回退明文
                etag = '"%x-%x-gz"' % (int(gz_st.st_mtime), gz_st.st_size)
                body = gz
        except OSError:
            pass
    if handler.headers.get("If-None-Match") == etag:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        if body is not f or pre_gz:
            handler.send_header("Vary", "Accept-Encoding")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return True
    handler.send_response(200)
    handler.send_header("Content-Type",
                        M3D_MIME.get(suf, "application/octet-stream"))
    handler.send_header("Content-Length", str(body.stat().st_size))
    handler.send_header("ETag", etag)
    if "?v=" in (handler.path or ""):
        handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
    elif no_cache:
        handler.send_header("Cache-Control", "no-cache")
    else:
        handler.send_header("Cache-Control", "public, max-age=%d" % cache_s)
    if body is not f or pre_gz:
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Vary", "Accept-Encoding")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    if handler.command == "HEAD":
        return True
    try:
        with open(body, "rb") as fh:
            while True:
                chunk = fh.read(262144)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except (ConnectionError, BrokenPipeError, OSError):
        handler.close_connection = True
    return True


# ------------------------------------------------- Korr 3D 的地形接口
_K3D_CACHE = {"mtime": None, "maps": {}, "by_name": {}}


def _korr3d_maps():
    """读 web/korr/maps3d/manifest.json（带 mtime 缓存），建 名字→条目 索引。"""
    p = KORR3D_DIR / "manifest.json"
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {}, {}
    if _K3D_CACHE["mtime"] != mt:
        try:
            maps = (json.loads(p.read_text(encoding="utf-8")) or {}).get("maps") or {}
        except ValueError:
            maps = {}
        by = {}
        for key, ent in maps.items():
            for alias in (key, ent.get("frontName"), ent.get("name")):
                if alias:
                    by[str(alias)] = ent
                    by[str(alias).lower()] = ent
        _K3D_CACHE.update(mtime=mt, maps=maps, by_name=by)
    return _K3D_CACHE["maps"], _K3D_CACHE["by_name"]


def korr3d_map(handler, path):
    """Korr 3D 视图的两个取图接口（它自己的服务端也是这两条）。

        GET /api/map/<名字>/meta?token=…  → {fileTag,sizeBytes,tris,bboxMin/Max,downloadUrl}
        GET /api/map/<名字>?token=…       → 三角形汤（无索引 float32，36 字节/三角形）

    我们不做它的地图授权（本地部署），token 收下不用；没有对应地形就 404，
    前端会显示"地图未找到"并回落到 2D。返回 True 表示这条请求已处理。
    """
    rest = unquote(path[len("/api/map/"):])
    is_meta = rest.endswith("/meta")
    if is_meta:
        rest = rest[:-len("/meta")]
    rest = rest.strip("/")
    if not rest:
        return False
    maps, by_name = _korr3d_maps()
    ent = by_name.get(rest) or by_name.get(rest.lower())
    if not ent:
        return False
    blob = KORR3D_DIR / ent["file"]
    if not blob.is_file():
        return False
    if is_meta:
        body = json.dumps({
            "canonicalName": ent.get("frontName") or ent.get("name"),
            "cacheKey": ent["key"],
            "fileTag": ent.get("fileTag") or "",
            "format": "f32le-triangles-v1",
            "sizeBytes": int(ent.get("sizeBytes") or blob.stat().st_size),
            "tris": int(ent.get("tris") or 0),
            "bboxMin": ent.get("bboxMin") or [0, 0, 0],
            "bboxMax": ent.get("bboxMax") or [0, 0, 0],
            "downloadUrl": "/api/map/%s" % quote(ent.get("frontName") or ent["key"]),
            "urlExpiresIn": 86400,
        }, ensure_ascii=False).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        handler.wfile.write(body)
        return True
    # 三角形汤：磁盘上存的是 .f32.gz，直接按 gzip 编码发（浏览器 fetch 自动解压，
    # 前端拿到的字节数就是 meta.sizeBytes，进度条也照常走）
    size = blob.stat().st_size
    handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Encoding", "gzip")
    handler.send_header("Content-Length", str(size))
    handler.send_header("X-File-Tag", ent.get("fileTag") or "")
    handler.send_header("X-Tris", str(int(ent.get("tris") or 0)))
    handler.send_header("Cache-Control", "public, max-age=86400")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    if handler.command != "HEAD":
        try:
            with open(blob, "rb") as fh:
                shutil.copyfileobj(fh, handler.wfile, 1 << 20)
        except (BrokenPipeError, ConnectionResetError):
            handler.close_connection = True
    return True


# ---------------------------------------------------------------- detection
def resolve_s2c_table(flow_dir):
    """S2C 加密表自解析（s2c_auto，与 live 路径 auto_s2c 同源）。

    0905 起对局下行带 512B 循环表混淆（P[i]=C[i]^C[i-1]^K[i%512]，每条 UDP
    记录独立重置，见 s2c_dec.py），裸读位流是乱码；bare 流（旧对局）返回
    None 原样直读。resolve_flow 自带连接级缓存/盘上状态，重复调用廉价。
    """
    from s2c_auto import resolve_flow
    try:
        dec = resolve_flow(Path(flow_dir))
    except Exception:
        return None
    return dec.table if (dec.ready and dec.table) else None


# ---- 流发现扫描的缓存 ------------------------------------------------------
# 4Hz 的扫描会把 captures 走一遍：每个 flow 读 meta.json、再 stat 两次。实测
# 76 run / 879 flow 时一轮 ~250ms —— 正好吃掉整个扫描节拍，把 60Hz 增量解析挤成
# ~4Hz。三层缓存都按"目录 mtime 变化即失效"实现，语义不变：
#   _META_CACHE    (meta.json 路径, mtime) -> 解析结果
#   _UDP_DIRS      flows/ 目录 mtime -> 该 run 里 proto=udp 的 flow 目录列表
#   _RUN_FRESHEST  flows/ 目录 mtime -> 上一轮算出的"最新 flow 写入时刻"（用于整轮跳过）
_META_CACHE = {}
_UDP_DIRS = {}
_RUN_FRESHEST = {}

LIVE_STALE_SEC = 180.0     # 只认最近还活跃的战斗流（秒）


class Detector:
    """确定性战斗流索引: run -> {flow: n_ps(arch131 通道数)}。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.table = {}
        try:
            if DETECT_CACHE.exists():
                self.table = json.loads(asset_crypto.decrypt_blob(
                    DETECT_CACHE.read_bytes(), asset_crypto.PURPOSE_RUNTIME))
        except Exception:
            self.table = {}

    def save(self):
        with self.lock:
            DETECT_CACHE.write_bytes(asset_crypto.encrypt(
                json.dumps(self.table, ensure_ascii=False).encode("utf-8"),
                asset_crypto.PURPOSE_RUNTIME))

    def known(self, run_name, flow_name):
        with self.lock:
            return self.table.get(run_name, {}).get(flow_name)

    def record(self, run_name, flow_name, n_ps):
        with self.lock:
            self.table.setdefault(run_name, {})[flow_name] = int(n_ps)

    @staticmethod
    def probe_flow(flow_dir):
        """战斗流判定（引擎级，与游戏客户端一致）+ PlayerState 通道数排序值。

        引擎门：UE4 客户端只有在控制通道(ch0)收到 NMT_WELCOME（含 "/Game/"
        地图串）后才把该 UDP 连接当作对局会话并开始复制 Actor。故
            非战斗流（含被拒连接 Failure、半握手 HC）一律返回 0；
            战斗流返回 PlayerState 通道数（路径解析类 GUID，NetGUID 按连接
            分配不能硬编码 131）。
        """
        try:
            hs = hs_mod.scan_control(flow_dir)
            if not hs.ok:
                return 0
            b = load(Path(flow_dir), s2c_table=resolve_s2c_table(flow_dir))
        except Exception:
            return -1
        # sweep 1: 收集全部导出路径 -> 类 GUID 集合
        ps_archs = set()
        for ch in b.values():
            for x in ch:
                if not x.open or not x.hx:
                    continue
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
                try:
                    info, _ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=False, frags=x.frags)
                except sna.Bad:
                    continue
                for e in info.get("exports", []):
                    p = e.get("path")
                    if not p:
                        continue
                    tail = p.rsplit(".", 1)[-1]
                    if tail.startswith("Default__"):
                        tail = tail[9:]
                    if tail in ("BP_DFMPlayerState_C", "BP_SOLPlayerState_C"):
                        ps_archs.add(e["guid"])
        if not ps_archs:
            ps_archs = {131}  # 兼容旧数据
        # sweep 2: 每通道首个 open bunch，arch ∈ ps_archs 计数
        n = 0
        for ch in b.values():
            for x in ch:
                if not x.open:
                    continue
                bits = np.unpackbits(np.frombuffer(x.payload, dtype=np.uint8), bitorder="little")[:x.pbits]
                try:
                    info, _ss = sna.parse_prefix(bits, x.hx, x.hm, quantize_bit=False, frags=x.frags)
                except sna.Bad:
                    continue
                if info.get("arch") in ps_archs:
                    n += 1
                break
        return n

    @staticmethod
    def flows_by_mtime(run_dir, limit=None):
        fds = []
        fdir = Path(run_dir) / "flows"
        if not fdir.is_dir():
            return fds
        for fd in fdir.iterdir():
            fj = fd / "frames.jsonl"
            if fd.is_dir() and fj.exists():
                try:
                    fds.append((fj.stat().st_mtime, fd))
                except OSError:
                    pass
        fds.sort(key=lambda t: -t[0])
        return [fd for _mt, fd in (fds if limit is None else fds[:limit])]

    @staticmethod
    def _meta(flow_dir):
        """读 meta.json；按 (路径, mtime) 缓存 —— 调用方只读，可安全共享。"""
        p = Path(flow_dir) / "meta.json"
        try:
            stamp = p.stat().st_mtime_ns
        except OSError:
            return {}
        key = (str(flow_dir), stamp)
        hit = _META_CACHE.get(key)
        if hit is not None:
            return hit
        try:
            val = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            val = {}
        if len(_META_CACHE) > 4096:
            _META_CACHE.clear()
        _META_CACHE[key] = val
        return val

    @staticmethod
    def udp_flows(run_dir, fresh_within=LIVE_STALE_SEC):
        """meta.json 标记 proto=udp 的 flow（UE 复制连接是 UDP；TCP 流按构造
        不可能是战斗连接）。返回 (size, frames_mtime, dir) 列表，排序交给调用方。

        三层优化见文件顶部注释。**整轮 stale 判据必须用 flow 的 frames.jsonl
        mtime，不能用 run 目录 mtime** —— 实测 run mtime 比它下面最新 flow 的
        mtime 早 20 分钟（写 frames 不会更新 run 目录），拿它当判据会误杀正在进行的对局。"""
        fdir = Path(run_dir) / "flows"
        if not fdir.is_dir():
            return []
        try:
            stamp = fdir.stat().st_mtime_ns
        except OSError:
            return []
        key = str(fdir)
        now = time.time()

        # 1) flows/ 目录没变过（没有新增/删除 flow）且上轮最新写入已过期 -> 整轮跳过
        fresh = _RUN_FRESHEST.get(key)
        if fresh is not None and fresh[0] == stamp and fresh_within:
            if now - fresh[1] > fresh_within:
                return []

        # 2) UDP 成员列表按 flows/ mtime 缓存（新增 flow 会改 flows/ mtime，天然失效）
        hit = _UDP_DIRS.get(key)
        if hit is not None and hit[0] == stamp:
            dirs = hit[1]
        else:
            dirs = [fd for fd in fdir.iterdir()
                    if fd.is_dir() and Detector._meta(fd).get("proto") == "udp"]
            if len(_UDP_DIRS) > 256:
                _UDP_DIRS.clear()
            _UDP_DIRS[key] = (stamp, dirs)

        # 3) 新鲜度必须每次重算（不能缓存：正在写的流 mtime 一直在变）
        out = []
        for fd in dirs:
            try:
                sz = (fd / "response.bin").stat().st_size
            except OSError:
                sz = 0
            try:
                mt = (fd / "frames.jsonl").stat().st_mtime
            except OSError:
                mt = 0
            out.append((sz, mt, fd))
        if len(_RUN_FRESHEST) > 256:
            _RUN_FRESHEST.clear()
        _RUN_FRESHEST[key] = (stamp, max((t[1] for t in out), default=0))
        return out

    @classmethod
    def flows_by_size(cls, run_dir, limit=None):
        """探测调度顺序：UDP 流按 response.bin 大->小（战斗流=数 MB 级长流）。
        仅影响探测先后，分类标准仍是 arch131 通道数最大。"""
        items = sorted(cls.udp_flows(run_dir), key=lambda t: -t[0])
        fds = [fd for _sz, _mt, fd in items]
        return fds if limit is None else fds[:limit]

    def battle_flow_of(self, run_name):
        with self.lock:
            tab = self.table.get(run_name) or {}
        best, best_n = None, 0
        for name, n in tab.items():
            if n > best_n:
                best, best_n = name, n
        return best, best_n


# ---------------------------------------------------------------- parsing
def extract_slots_full(b, mra_ch, seq_ts):
    """slot -> [(seq, ts, x, y, z, yaw)]（确定性逐帧，越界点剔除）。"""
    slots = {}
    for x in b.get(mra_ch, []):
        try:
            blk = tile(x.payload, x.pbits, 0)
        except Exception:
            continue
        if not blk:
            continue
        ts = seq_ts(x.seq)
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
            except Exception:
                continue
            for i, it in fr["items"].items():
                p = it.get("pos")
                if not p or not WORLD(p):
                    continue
                rot = it.get("rot")
                yaw = rot[1] if rot else None
                slots.setdefault(i, []).append((x.seq, ts, float(p[0]), float(p[1]), float(p[2]), yaw))
    for tr in slots.values():
        tr.sort(key=lambda t: t[0])
    return slots


class BattleView:
    """一个战斗流的完整确定性视图（文件增长时全量重建）。"""

    def __init__(self, run_dir, flow_dir):
        self.run_dir = Path(run_dir); self.flow_dir = Path(flow_dir)
        self.lock = threading.Lock()
        self.frames_bytes = -1
        self.build()

    @staticmethod
    def _load_seq_ts(flow_dir):
        seqs, tss = [], []
        try:
            with (flow_dir / "frames.jsonl").open(encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("dir") != "response":
                        continue
                    seqs.append(int(rec["seq"])); tss.append(float(rec["ts"]))
        except OSError:
            pass
        order = np.argsort(seqs)
        return np.asarray(seqs)[order], np.asarray(tss)[order]

    def seq_ts(self, seq):
        s, t = self._seq_ts_map
        i = int(np.searchsorted(s, seq))
        if i <= 0: return float(t[0])
        if i >= len(s): return float(t[-1])
        s0, s1 = int(s[i - 1]), int(s[i])
        if s1 <= s0: return float(t[i])
        f = (seq - s0) / float(s1 - s0)
        return float(t[i - 1] + f * (t[i] - t[i - 1]))

    def frames_size(self):
        try:
            return (self.flow_dir / "frames.jsonl").stat().st_size
        except OSError:
            return -1

    def build(self):
        t0 = time.time()
        self._seq_ts_map = self._load_seq_ts(self.flow_dir)
        # 0905 加密对局：回放侧与 live 侧同走 s2c_auto 自解析（bare 流表=None）
        b = load_cached(self.flow_dir, s2c_table=resolve_s2c_table(self.flow_dir))
        archs = nl.resolve_class_archs(b)
        # 露天物资类 GUID：不依赖 resolve_class_archs 是否带 pk 组（服务器旧版
        # 没有），本地按导出路径补解析（Default__ CDO 前缀同款规则）
        pk_archs = set()
        for g, p in nl.build_guid_paths(b).items():
            t = p.rsplit(".", 1)[-1]
            if t.startswith("Default__"):
                t = t[9:]
            if t.startswith("BP_InventoryPickup"):
                pk_archs.add(g)
        from pickup_state import PickupTracker
        from liveview import LOOT_NAMES, LOOT_META
        self.pickup_tracker = PickupTracker(pk_archs, LOOT_NAMES, LOOT_META, keep_history=True)
        self.loot = self.pickup_tracker.records
        players = nl.extract_players(b, ps_archs=archs["ps"])
        # 只有人类角色参与 slot 认领：MRA 是队伍位置广播，世界 AI 不在其中，
        # 其出生点会以 min-dist 抢占队伍槽位。
        links = nl.extract_char_links(b, char_archs=archs["char"])
        from property_health import HealthBatchTracker
        from weapon_state import WeaponTracker
        self.weapon_tracker = WeaponTracker(keep_history=True)
        health_tracker = HealthBatchTracker(keep_history=True)
        self.box_tracker = DeathBoxTracker(keep_history=True)
        self.box_contents = BoxContentsTracker(keep_history=True)
        ps_tracker = PlayerStateTracker()
        for bunch in sorted((x for xs in b.values() for x in xs), key=lambda x: x.seq):
            health_tracker.consume(bunch)
            closing_box = self.box_tracker.channels.get(bunch.ch)
            self.box_tracker.consume(bunch)
            self.box_contents.consume(bunch)
            if not bunch.open and bunch.close and bunch.close_reason in (0, 2, 4) and closing_box is not None:
                self.box_contents.forget(closing_box, bunch.seq)
            ps_tracker.consume(bunch)
            self.weapon_tracker.consume(bunch)
            self.pickup_tracker.consume(bunch)
        self.health_history = health_tracker.resolved_history()
        self.life_tracker = DeathStateTracker(keep_history=True)
        for actor, link in links.items():
            if link.get("spawn_seq") is not None:
                self.life_tracker.spawn(actor, link["spawn_seq"])
        life_events = [(seq, "health", actor, value) for actor, fields in self.health_history.items()
                       for seq, value in fields.get("IsAlive", [])]
        for seq, ps, fields in ps_tracker.events:
            candidates = [(link.get("spawn_seq", 0), actor) for actor, link in links.items()
                          if link.get("ps_guid") == ps and link.get("spawn_seq", 0) <= seq]
            if candidates:
                life_events.append((seq, "ps", max(candidates)[1], fields))
        for seq, source, actor, value in sorted(life_events, key=lambda x: x[0]):
            if source == "health":
                self.life_tracker.observe(actor, value, seq)
            else:
                self.life_tracker.observe_playerstate(actor, value, {k: seq for k in value}, verified=True)
        self.ps_decode_stats = dict(ps_tracker.stats)
        # ps_tracker 解出的 PS 可能不在 extract_players 的结果里（名字不在开局
        # bunch 的 PS —— 实测 115718 局 PS3600/5868 认领了槽却无名）。旧代码
        # `if ps in players` 直接把这类记录丢掉 -> 回放里真人顶着无名/「玩家」。
        # 这里补建条目并映射全部身份字段（与 liveview._merge_playerstate 同口径）。
        for ps, fields in ps_tracker.records.items():
            p = players.get(ps)
            if p is None:
                if not fields.get("PlayerNamePrivate"):
                    continue
                p = players[ps] = {
                    "name": None, "player_id": None, "bot": None,
                    "actor_guid": ps, "owner_guid": None, "channel": None,
                    "uin": None, "team": None, "camp": None, "inner_team": None,
                    "hero_id": None, "expert_id": None, "spawn_seq": None,
                }
            if fields.get("PlayerNamePrivate"):
                p["name"] = fields["PlayerNamePrivate"]
            # 通道号（自机判定用）：ps_tracker.channels 是 channel -> actor，反查。
            # 缺了也不影响（自机选择逐条跳过 channel=None），但补上更准）。
            if p.get("channel") is None:
                for _ch, _a in ps_tracker.channels.items():
                    if _a == ps:
                        p["channel"] = _ch
                        break
            for key, dest in (("TeamID", "team"), ("Camp", "camp"), ("CampID", "camp"),
                              ("PlayerId", "player_id"),
                              ("Uin", "uin"), ("InnerTeamIndex", "inner_team"),
                              ("HeroId", "hero_id"), ("ExpertId", "expert_id")):
                if key in fields:
                    p[dest] = fields[key]
            _ow = fields.get("Owner")
            if isinstance(_ow, dict):
                _ow = _ow.get("guid")
            if _ow is not None:
                p["owner_guid"] = _ow
        self.links = links
        # 角色消失（close_reason ∈ {0,2,3,4}）：死亡/离开更新距离/销毁
        gone = st.extract_gone_actors(b, archs["char"] | archs["ai"])
        mra_ch, mra_n = nl.find_mra_channel(b)
        slots = extract_slots_full(b, mra_ch, self.seq_ts) if mra_ch is not None else {}
        # 拾取推断（wire 沉默补丁）：close r0 只覆盖秒捡，玩家停驻在物资旁的
        # 拾取姿态补判 r1 后的沉默拾取（回放按 picked_seq 时间旅行过滤）
        if slots:
            mark_picked_by_standing(self.loot, slots)
        # 确定性 join: 角色出生点 <-> slot@spawn_seq (<500cm)，最小距离唯一认领
        explicit = nl.explicit_slot_claims(links)
        reserved = nl.observed_move_slots(links)
        claim = {slot: (actor, 0.0) for slot, actor in explicit.items()}
        for actor, lk in links.items():
            if "move_handle" in lk:
                continue
            loc, S = lk.get("loc"), lk.get("spawn_seq")
            if not loc or S is None or not slots:
                continue
            loc = np.array(loc, dtype=float)
            best = None; bd = 1e18
            for slot, tr in slots.items():
                if slot in reserved:
                    continue
                if not tr:
                    continue
                seqs = [t[0] for t in tr]
                # 时间上最近的采样（左右两侧都看，与 er_live 验证过的语义一致）
                j = bisect.bisect_left(seqs, S)
                for jj in (j - 1, j):
                    if 0 <= jj < len(tr):
                        d = float(np.linalg.norm(loc - np.array(tr[jj][2:5])))
                        if d < bd:
                            bd, best = d, slot
            if best is not None and bd < 500.0:
                prev = claim.get(best)
                if prev is None or bd < prev[1]:
                    claim[best] = (actor, bd)
        # 成员槽判定（结构性、确定性）：真实成员轨迹有数百~数千采样，
        # 瞬态幽灵条目（如 slot0 仅 2 点）不构成成员。
        MEMBER_MIN_POINTS = 16
        members = {s for s, tr in slots.items() if len(tr) >= MEMBER_MIN_POINTS}
        # self 三层判定（与 LiveView 同构，1643 局实证权威 > 闭世界）：
        # ① 闭世界：唯一未认领"成员"槽 = 我，仅当其余成员槽全部被"命名"角色
        #    认领（如 15 = 14 远程 + 1 self）。部分对局（中途进入等）不满足。
        # ② field13 小队广播"我"的最新全量帧位置对齐最近成员槽（300cm² 门限，
        #    payload_hit 预过滤——只解含 SELF_WIRE 的全量帧，增量帧无自机标记）。
        # ③ 权威覆盖：本地玩家 PS = 真人里通道号最低者（UE4 握手先复制本连接
        #    自己的 PlayerState，495/733/988 三局一致；pid 最小规则曾标错两局），
        #    反查其 Character 认领的槽直接钉死。
        unclaimed = [s for s in members if s not in claim]
        self_slot = None
        self_rule = None
        named_actors = set()
        for actor, lk in links.items():
            for g, p in players.items():
                if (lk.get("ps_guid") is not None and g == lk["ps_guid"]) or \
                   (lk.get("owner") is not None and p.get("owner_guid") == lk["owner"]):
                    named_actors.add(actor); break
        claimed_actors = {a for a, _d in claim.values()}
        if len(unclaimed) == 1 and claimed_actors and claimed_actors <= named_actors \
                and len(claim) == len(members) - 1:
            self_slot = unclaimed[0]; self_rule = "closed"
        actor2slot = {a: s for s, (a, _d) in claim.items()}
        # 匿名成员槽补名（与 liveview._derive 第三梯队同口径）：PlayerId 与 MRA 槽号
        # 同出一套分配序，但只是**局部**同序——115718 局 10/10 恒为 242（据此补出
        # slot23 -> 祺捣），215035 局会漂移（248/246/245）。只在高一致时才采信，
        # 有漂移就整条放弃（宁留「未知玩家」也不补错名）。
        self.anon_names = {}
        _pidoff = {}
        _pidoff_tot = 0
        for _s2, (_a, _d3) in claim.items():
            _pg2 = (links.get(_a) or {}).get("ps_guid")
            _p2 = players.get(_pg2) if _pg2 is not None else None
            _pid2 = _p2.get("player_id") if _p2 is not None else None
            if _pid2 is None or _p2.get("bot") is not None or _pid2 >= 8000:
                continue
            _pidoff[_pid2 - _s2] = _pidoff.get(_pid2 - _s2, 0) + 1
            _pidoff_tot += 1
        if _pidoff:
            _off_v, _off_n = max(_pidoff.items(), key=lambda kv: kv[1])
            _off_2nd = max((v for k, v in _pidoff.items() if k != _off_v), default=0)
            if (_off_n >= 4 and _off_n >= _pidoff_tot * 0.8
                    and _off_n > _off_2nd * 3):
                _pid_used = set()
                for _s2, (_a, _d3) in claim.items():
                    _pg2 = (links.get(_a) or {}).get("ps_guid")
                    _p2 = players.get(_pg2) if _pg2 is not None else None
                    if _p2 is not None and _p2.get("player_id") is not None:
                        _pid_used.add(_p2["player_id"])
                _pid_free = {}
                for _g3, _p3 in players.items():
                    _pid3 = _p3.get("player_id")
                    if (_pid3 is None or _pid3 in _pid_used or not _p3.get("name")
                            or _p3.get("bot") is not None):
                        continue
                    _pid_free[_pid3] = _g3
                for _s2 in members:
                    if _s2 in claim or _s2 == self_slot:
                        continue
                    _g3 = _pid_free.get(_s2 + _off_v)
                    if _g3 is not None:
                        self.anon_names[_s2] = players[_g3]
        f13_slot = None
        if self_slot is None:
            fseq, fxy = -1, None
            for ch in sorted(b):
                for x in b[ch]:
                    if x.pbits < 64 or not f13.payload_hit(x.payload):
                        continue
                    try:
                        dec = f13.decode(x.payload, x.pbits)
                    except Exception:
                        continue
                    if not dec or not dec["vals64"] or not dec["locs"]:
                        continue
                    if any(v in f13.SELF_WIRE for v in dec["vals64"]) and x.seq >= fseq:
                        si = min(f13.self_index(dec["vals64"]), len(dec["locs"]) - 1)
                        _loc = dec["locs"][si]
                        fseq, fxy = x.seq, (float(_loc[0]), float(_loc[1]))
            if fxy is not None:
                best = None; bd = 1e18
                for slot, tr in slots.items():
                    if len(tr) < MEMBER_MIN_POINTS:
                        continue
                    seqs = [t[0] for t in tr]
                    j = bisect.bisect_left(seqs, fseq)
                    for jj in (j - 1, j):
                        if 0 <= jj < len(tr):
                            t = tr[jj]
                            d2 = (fxy[0] - t[2]) ** 2 + (fxy[1] - t[3]) ** 2
                            if d2 < bd:
                                bd, best = d2, slot
                if best is not None and bd < 90000.0:
                    self_slot = best; self_rule = "f13"
                    f13_slot = best
        _best_ch = None
        self_ps = None
        self_name = None
        for _g, p in players.items():
            if p.get("bot") is not None or not p.get("name") or p.get("channel") is None:
                continue
            if _best_ch is None or p["channel"] < _best_ch:
                _best_ch, self_ps, self_name = p["channel"], _g, p["name"]
        if self_name is None:   # 回退：pid 最小真人（旧规则）
            for _g, p in sorted(players.items(), key=lambda kv: kv[1].get("player_id") or 1 << 30):
                if p.get("bot") is None and p.get("name"):
                    self_name = p["name"]; break
        if self_ps is not None:
            for _a, _lk in links.items():
                if _lk.get("ps_guid") == self_ps and _a in actor2slot:
                    self_slot = actor2slot[_a]
                    self_rule = "ps(f13=%s)" % f13_slot
                    break
        ents = []
        for actor, lk in links.items():
            name = pid = bot = None
            team = camp = hid = ps_hid = None
            player_state = None
            for g, p in players.items():
                if lk.get("ps_guid") is not None and g == lk["ps_guid"]:
                    name, pid, bot = p["name"], p.get("player_id"), p.get("bot")
                    team, camp = p.get("team"), p.get("camp")
                    player_state = p
                    ps_hid = p.get("hero_id"); break
            if name is None and lk.get("owner") is not None:
                for g, p in players.items():
                    if p.get("owner_guid") == lk["owner"]:
                        name, pid, bot = p["name"], p.get("player_id"), p.get("bot")
                        team, camp = p.get("team"), p.get("camp")
                        player_state = p
                        ps_hid = p.get("hero_id"); break
            # hero：0904 起走角色 fashion 组件（extract_char_links 写入
            # links[a]["hero"]），PS h137 兜底旧流量
            hid = lk.get("hero") or ps_hid
            # self 角色标记：权威 ps_guid 反查优先；无真人 PS 时退回旧
            # 「无 loc 且 28 导出」规则（同一角色的两种识别，互为兜底）
            is_self = (self_ps is not None and lk.get("ps_guid") == self_ps) or \
                (self_ps is None and lk.get("loc") is None and lk.get("num_exports") == 28)
            hero_name = HERO_NAMES.get(hid) if hid else None
            is_bot = confirmed_player_bot(player_state, hero_name is not None)
            if is_self:
                kind = "self"
            elif is_bot:
                kind = "ai"   # 队伍 bot（有名字）
            else:
                kind = "player"
            disp = ("%s(self)" % self_name) if is_self else name
            if disp is None and actor2slot.get(actor) is None and lk.get("loc") is None:
                continue  # 无名且无槽位的角色不显示
            g = gone.get(actor)
            # self 永远在场（服务器对本地玩家的 close 是相关性 cull）
            if is_self:
                g = None
            ents.append({
                "actor": actor,
                "name": disp,
                "kind": kind,
                "slot": None if is_self else actor2slot.get(actor),
                "spawn_loc": lk.get("loc"), "spawn_seq": lk.get("spawn_seq"),
                "player_id": pid,
                "team": team, "camp": camp,
                # 与 LiveView ents 同构：前端气泡读 is_bot（人机后缀）与 hero（干员名）
                "is_bot": is_bot,
                "hero": hero_name,
                "hid": hid,
                # 消失（close_reason ∈ {0,2,3,4}）：close_seq 之后回放游标不再显示
                "close_seq": g[0] if g else None,
                "close_reason": g[1] if g else None,
            })
        with self.lock:
            self.players = players
            self.ents = ents
            self.slots = slots
            self.member_slots = members
            self.claim = dict(actor2slot)
            self.self_slot = self_slot
            self.mra_ch = mra_ch; self.mra_n = mra_n
            self.parse_s = round(time.time() - t0, 2)
            self.frames_bytes = self.frames_size()
        print("[view] %s/%s: %d ents, %d named, %d slotted, self_slot=%s rule=%s (%.1fs)" % (
            self.run_dir.name, self.flow_dir.name, len(ents),
            len([e for e in ents if e["name"]]),
            len([e for e in ents if e["slot"] is not None]),
            self_slot, self_rule, time.time() - t0), flush=True)

    def refresh_if_grown(self):
        if self.frames_size() != self.frames_bytes:
            print("[view] flow grown -> rebuild", flush=True)
            self.build()
            return True
        return False


# ---------------------------------------------------------------- playback
class Playback:
    def __init__(self, view):
        self.view = view
        self.speed = 16.0
        self.cursor_seq = None
        self.last_wall = time.time()
        self._sps = None

    def bounds(self):
        v = self.view
        lo = hi = None
        with v.lock:
            for tr in v.slots.values():
                if tr:
                    a, b_ = tr[0][0], tr[-1][0]
                    lo = a if lo is None else min(lo, a)
                    hi = b_ if hi is None else max(hi, b_)
        return lo, hi

    def seq_per_sec(self):
        if self._sps is None:
            s, t = self.view._seq_ts_map
            if len(s) >= 2 and float(t[-1] - t[0]) > 1.0:
                self._sps = float(s[-1] - s[0]) / float(t[-1] - t[0])
            else:
                self._sps = 56.0
        return self._sps

    def advance(self):
        lo, hi = self.bounds()
        if lo is None:
            return
        now = time.time()
        if self.cursor_seq is None or self.cursor_seq < lo or self.cursor_seq >= hi:
            self.cursor_seq = lo
            self.last_wall = now
            return
        if self.speed > 0:
            self.cursor_seq += (now - self.last_wall) * self.speed * self.seq_per_sec()
            if self.cursor_seq > hi:
                self.cursor_seq = hi
        self.last_wall = now

    def snapshot(self):
        self.advance()
        from property_health import health_at_seq, operator_health
        v = self.view
        cur = int(self.cursor_seq or 0)
        out_ents = []
        self_xyz = None; self_yaw = 0.0; self_name = None
        names = []
        lo, hi = self.bounds()

        def _slot_pos(slot, tr):
            """cursor 处的槽位采样（含轨迹尾）；出生前返回 (None,None,None)。"""
            if slot is None or not tr:
                return None, None, None
            seqs = [t[0] for t in tr]
            j = bisect.bisect_right(seqs, cur) - 1
            if j < 0:
                return None, None, None
            rec = tr[j]
            jj = min(bisect.bisect_right(seqs, cur) + 1, len(tr))
            trail = [[p[2], p[3], p[4]] for p in tr[max(0, jj - 160):jj:4]]
            return [rec[2], rec[3], rec[4]], rec[5], trail

        # 先解析 self（所有 rel 都依赖它）
        with v.lock:
            explicit = nl.explicit_slot_claims(v.links, at_seq=cur)
            current_slots = {actor: slot for slot, actor in explicit.items()}
            def actor_slot(e):
                if "move_handle" in v.links.get(e["actor"], {}):
                    return current_slots.get(e["actor"])
                return v.self_slot if e["kind"] == "self" else e["slot"]

            _self_ent = next((e for e in v.ents if e["kind"] == "self"), None)
            if _self_ent is not None:
                self_slot = actor_slot(_self_ent)
                self_xyz, self_yaw0, _trail0 = _slot_pos(
                    self_slot, v.slots.get(self_slot))
                self_yaw = self_yaw0 or 0.0
                self_name = _self_ent["name"]
            for e in v.ents:
                slot = actor_slot(e)
                if e["kind"] == "self":
                    continue
                if e.get("spawn_seq", 0) > cur:
                    continue
                life = v.life_tracker.at(e["actor"], cur)
                if life["dead"]:
                    continue
                link = v.links.get(e["actor"], {})
                assignments = link.get("move_handle_history", [])
                latest = next((p for p in reversed(assignments) if p[0] <= cur), None)
                if latest is not None and latest[1] >= 0 and e["actor"] not in current_slots:
                    continue
                # 角色消失（close_seq 之后）：回放游标越过 close_seq 即不再显示
                cs = e.get("close_seq")
                if cs is not None and cur >= cs and not \
                        (e.get("close_reason") == 3 and explicit.get(slot) == e["actor"]):
                    continue
                pos, yaw, trail = _slot_pos(slot, v.slots.get(slot))
                if pos is None and slot is None and e.get("spawn_loc"):
                    # 有名字但无槽位轨迹（部分对局）：显示出生点
                    sp = e["spawn_loc"]
                    pos = [float(sp[0]), float(sp[1]), float(sp[2])]
                    out_ents.append({
                        "key": "spawn-%s" % e["actor"], "name": e["name"], "kind": e["kind"],
                        "xyz": pos, "world": pos,
                        "rel": [pos[0] - self_xyz[0], pos[1] - self_xyz[1], pos[2] - self_xyz[2]] if self_xyz else None,
                        "yaw": None, "slot": None, "ch": e["actor"], "eid": e["actor"], "trail": None,
                        "team": e.get("team"), "camp": e.get("camp"),
                        "is_bot": e.get("is_bot"), "hero": e.get("hero"), "hid": e.get("hid"), "hid": e.get("hid"),
                        "hp": health_at_seq(v.health_history, e["actor"], cur),
                    **weapon_fields(v.weapon_tracker, e["actor"], WEAPON_NAMES, cur),
                    })
                    continue
                if pos is None:
                    continue
                rel = [pos[0] - self_xyz[0], pos[1] - self_xyz[1], pos[2] - self_xyz[2]] if self_xyz else None
                out_ents.append({
                    "key": "slot-%s" % slot, "name": e["name"], "kind": e["kind"],
                    "xyz": pos, "world": pos, "rel": rel, "yaw": yaw,
                    "slot": slot, "ch": slot, "eid": e["actor"], "trail": trail,
                    "dead": False,
                    "team": e.get("team"), "camp": e.get("camp"),
                    "is_bot": e.get("is_bot"), "hero": e.get("hero"), "hid": e.get("hid"),
                    "hp": health_at_seq(v.health_history, e["actor"], cur),
                    **weapon_fields(v.weapon_tracker, e["actor"], WEAPON_NAMES, cur),
                    "binding_source": "move_handle" if explicit.get(slot) == e["actor"] else "spawn_proximity",
                })
                if e["name"]:
                    names.append({"ch": slot, "name": e["name"], "kind": e["kind"]})
            # 未绑定角色的成员槽 -> 匿名实体（不猜身份）
            bound = {actor_slot(e) for e in v.ents if actor_slot(e) is not None}
            for s, tr in v.slots.items():
                if s in bound or s == v.self_slot or s not in v.member_slots:
                    continue
                seqs2 = [t[0] for t in tr]
                j = bisect.bisect_right(seqs2, cur) - 1
                if j < 0:
                    continue
                rec = tr[j]
                # 匿名槽补名：BattleView.build 已按「PlayerId ↔ 槽号同序」补齐
                # （见 build 里 anon_names / _pidoff 与 liveview._derive 第三梯队）。
                anon_p = (getattr(v, "anon_names", None) or {}).get(s)
                out_ents.append({
                    "key": "anon-%s" % s,
                    "name": anon_p.get("name") if anon_p else None,
                    "kind": "player" if anon_p else "unknown",
                    "xyz": [rec[2], rec[3], rec[4]],
                    "world": [rec[2], rec[3], rec[4]], "rel": None, "yaw": rec[5],
                    "slot": s, "ch": s, "eid": 0, "trail": None,
                    "dead": False,
                    "team": anon_p.get("team") if anon_p else None,
                    "camp": anon_p.get("camp") if anon_p else None,
                    "is_bot": confirmed_player_bot(
                        anon_p,
                        HERO_NAMES.get((anon_p or {}).get("hero_id")) is not None),
                })
                if anon_p and anon_p.get("name"):
                    names.append({"ch": s, "name": anon_p["name"], "kind": "player"})
            for entity in out_ents:
                if entity.get("eid"):
                    life = v.life_tracker.at(entity["eid"], cur)
                    entity.update(life_state=life["state"], life_status=life["status"],
                                  life_seq=life["seq"], life_source=life["source"])
                    if life["downed"]:
                        entity.update(status="倒地", status_key="down")
            character_positions = {}
            for e in v.ents:
                slot = actor_slot(e)
                pos, _, _ = _slot_pos(slot, v.slots.get(slot))
                if pos is not None:
                    character_positions[e["actor"]] = pos
            names_by_actor = {e["actor"]: e.get("name") for e in v.ents}
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
            for actor in v.box_tracker.records:
                record = v.box_tracker.at(actor, cur)
                if not record or record["kind"] != "box" or record.get("gone_seq") is not None or record.get("hidden"):
                    continue
                pos = v.box_tracker.world_position(actor, character_positions, cur)
                if pos is None or not WORLD(pos):
                    continue
                owner = record.get("deceased_actor") or record.get("owner_actor")
                seen = set()
                while owner in v.box_tracker.records and owner not in seen:
                    seen.add(owner)
                    carrier = v.box_tracker.at(owner, cur)
                    if not carrier:
                        break
                    owner = carrier.get("deceased_actor") or carrier.get("owner_actor")
                name = names_by_actor.get(owner)
                if owner is None and not name and record.get("spawn_seq") is not None and dead_candidates:
                    _best = min((c for c in dead_candidates if c[1] not in used_dead),
                                key=lambda c: abs(c[0] - int(record["spawn_seq"])), default=None)
                    if _best is not None and abs(_best[0] - int(record["spawn_seq"])) <= 12:
                        owner, name = _best[1], _best[2]
                        used_dead.add(_best[1])
                if not name:
                    name = record.get("player_name")
                    if name and ("LOCTABLE" in name or "/Game/" in name):
                        name = None
                entity = box_entity(actor, record, pos, owner, name, self_xyz)
                entity.update(v.box_contents.describe(actor, LOOT_NAMES, cur))
                owner_link = v.links.get(owner) or {}
                owner_cls = str(owner_link.get('cls') or '')
                owner_ai = ('AICharacter' in owner_cls or 'Character_AI' in owner_cls
                            or owner_link.get('ai_key') is not None)
                if entity['is_ai'] is None and (owner_ai or (owner is not None and not owner_link)
                                                or (owner is None and entity.get('name') == '死亡盒')):
                    entity['is_ai'] = True
                if owner is not None:
                    owner_entity = next((e for e in v.ents if e['actor'] == owner), {})
                    owner_link = v.links.get(owner) or {}
                    owner_cls = str(owner_link.get('cls') or '')
                    owner_ai = ('AICharacter' in owner_cls or 'Character_AI' in owner_cls
                                or owner_link.get('ai_key') is not None)
                    if entity['is_ai'] is None and (owner_ai or owner_entity.get('kind') == 'ai'):
                        entity['is_ai'] = True
                    owner_life = v.life_tracker.at(owner, cur)
                    entity['owner_dead'] = owner_life['dead'] if owner_life['confirmed'] else None
                out_ents.append(entity)
            # 露天物资（回放）：游标前已 spawn 且未收到协议级消失 close。
            # 停驻只是 pickup_candidate，不能改变物资是否仍存在的判断。
            for la in v.pickup_tracker.records:
                lr = v.pickup_tracker.at(la, cur)
                if lr is None or lr.get("loc") is None:
                    continue
                candidate = v.loot.get(la, {}).get("pickup_candidate_seq")
                if candidate is not None and lr["seq"] <= candidate <= cur and lr.get("gone_seq") is None:
                    lr = dict(lr, pickup_candidate_seq=candidate, pickup_confidence="candidate")
                if lr["seq"] > cur:
                    continue
                gseq = lr.get("gone_seq")
                if gseq is not None and cur >= gseq:
                    continue
                nm = lr.get("name")
                if (not lr.get("item_id") or not nm or nm in ("物资", "未知物资")
                        or str(nm).startswith("???")):
                    continue
                lp = lr["loc"]
                out_ents.append({
                    "key": "loot-%s" % la,
                    "name": lr.get("name") or "物资",
                    "kind": "loot", "xyz": list(lp), "world": list(lp),
                    "rel": [lp[0] - self_xyz[0], lp[1] - self_xyz[1],
                            lp[2] - self_xyz[2]] if self_xyz else None,
                    "yaw": None, "slot": None, "ch": la, "eid": la, "trail": None,
                    **loot_display_fields(lr),
                })
            meta = {"mra_ch": v.mra_ch, "mra_n": v.mra_n, "built_in_s": v.parse_s}
            flow = "%s / %s" % (v.run_dir.name, v.flow_dir.name)
        # 时间标签用真实 ts
        t_lo = t_hi = None
        s_arr, t_arr = v._seq_ts_map
        if len(s_arr):
            t_lo = float(t_arr[0]); t_hi = float(t_arr[-1])
        for entity in out_ents:
            if entity.get('hp'):
                entity['hp'] = operator_health(entity['hp'], entity['kind'] in ('player', 'mate', 'self') or bool(entity.get('hero')))
        return {
            "self": self_xyz, "self_yaw": self_yaw, "self_name": self_name,
            "self_hero": _self_ent.get("hero") if _self_ent else None,
            "self_hp": operator_health(health_at_seq(v.health_history, _self_ent["actor"], cur)) if _self_ent else None,
            "self_life": v.life_tracker.at(_self_ent["actor"], cur) if _self_ent else None,
            "live_active": False, "origin": [0, 0],
            "entities": out_ents, "names": names,
            "flow": flow, "meta": meta,
            "cursor": {"seq": self.cursor_seq, "lo": lo, "hi": hi,
                       "speed": self.speed, "t_lo": t_lo, "t_hi": t_hi},
        }


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    app = None
    map_info = None

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj):
        # obj may be bytes (pre-encoded /api/state cache hit): send verbatim,
        # skipping the per-tick json.dumps of a ~29KB body.
        if isinstance(obj, (bytes, bytearray)):
            body = bytes(obj)
        else:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        app = Handler.app
        path = urlparse(self.path).path
        q = parse_qs(urlparse(self.path).query)
        if path == "/api/state":
            # 出错时降级应答（以前异常直接冒到 socketserver，浏览器只看到"空响应"，
            # curl: Empty reply，完全无法定位）。traceback 只在 ER_DEBUG=1 时打。
            try:
                self._json(app.api_state(q.get("account", [""])[0]))
            except Exception:
                if _DEBUG:
                    import traceback
                    traceback.print_exc()
                self._json({"self": None, "entities": [], "names": [],
                            "status": "error"})
            return
        if path == "/api/map":
            self._json(Handler.map_info or {}); return
        if path == "/api/flows":
            self._json(app.api_flows(q.get("account", [""])[0])); return
        if path == "/api/select":
            self._json(app.api_select(q.get("run", [""])[0], q.get("flow", [""])[0],
                                      q.get("account", [""])[0])); return
        if path == "/api/ctrl":
            self._json(app.api_ctrl(q)); return
        if m3d_serve(self, path):
            return
        if m3d_serve(self, path, prefix="/avatars/", root=AVATAR_DIR, no_cache=True):
            return
        # Korr 前端（第三方 UI，见 实例对接.md）：/static/* 与 /korr/* 都从 web/korr 出，
        # 这样它内部写死的 /static/... 路径不用改一行。两条入口都常开，方便两套页面对照。
        if path in ("/er", "/er.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404); self.end_headers()


class RadarApp:
    def __init__(self, fixed_run=None, live=False):
        self.det = Detector()
        self.fixed_run = Path(fixed_run) if fixed_run else None
        self.live = live
        self.view = None
        self.play = None
        self.lock = threading.Lock()
        self.current = None       # (run_name, flow_name)
        self.pinned = None        # live 模式手动固定 (run, flow)
        self.pin_lock = None      # 回放模式固定 (run, flow):--flow / /api/select 后不再自动轮转
        self.live_detectors = {}  # (run_name, flow_name) -> LiveDetector（live 模式探测）
        # 污染自愈（见 _maybe_rebuild）：解码错误累计到阈值就把当前这一局后台整局
        # 重解一遍、原子换掉视图。_rebuild_base 是新视图的误差基线。
        self._rebuild_busy = False
        self._rebuild_last = 0.0
        self._rebuild_base = None
        # /api/state 应答缓存：前端自适应轮询（POLL_FAST=50ms，数据没变退到 300ms），
        # 底层数据多数 tick 未变。
        # key = (底层变化指纹) -> body_obj；未变直接复用，省 snapshot() 重建。
        self._state_cache = {"key": None, "body_obj": None}

    _RUNS_TTL = 2.0

    def _runs_index(self):
        """[(run_dir, mtime)] 新->旧，2 秒内复用 —— 4Hz 扫描下省掉每轮 glob + 几十次 stat。"""
        now = time.time()
        cache = getattr(self, "_runs_cache", None)
        if cache is not None and now - cache[0] < self._RUNS_TTL:
            return cache[1]
        cap = ROOT / "captures"
        pairs = []
        for d in cap.glob("run_*"):
            if not (d / "flows").is_dir():
                continue
            try:
                pairs.append((d, d.stat().st_mtime))
            except OSError:
                continue
        pairs.sort(key=lambda t: -t[1])
        self._runs_cache = (now, pairs)
        return pairs

    def runs_newest_first(self):
        runs = [d for d, _ in self._runs_index()]
        if self.fixed_run is not None:
            runs = [d for d in runs if d.name == self.fixed_run.name]
        return runs

    def _switch(self, run_dir, flow_dir):
        with self.lock:
            if self.current == (run_dir.name, flow_dir.name):
                return
            print("[radar] view -> %s / %s" % (run_dir.name, flow_dir.name), flush=True)
            if self.live:
                # 键必须带 run：抓包每局新建 run_*，而流目录在每局里都从 0,1,2… 重编号，
                # 只用 flow 名会命中上一局的同名流探测器 —— LiveView 会拿到旧局文件的
                # loader，解析的是已停写的旧文件，表现为"锁定了却不刷新/无实体"。
                det = self.live_detectors.get((run_dir.name, flow_dir.name))
                if det is not None:
                    det.loader.enable_c2s_self(1)  # 锁定后才解上行自机位姿；不抽稀（俯仰 6.6Hz，抽稀=4 只剩 1.6Hz）
                self.view = LiveView(run_dir, flow_dir,
                                     loader=det.loader if det else None,
                                     guid_paths=det.guid_paths if det else None)
                self.play = None
            else:
                self.view = BattleView(run_dir, flow_dir)
                self.play = Playback(self.view)
            self.current = (run_dir.name, flow_dir.name)
            self._rebuild_base = None      # 换流：重解基线跟着新视图重置
            # 切流即作废 body 缓存（指纹含流身份，但保险起见立即清空，
            # 避免下一场首 tick 误发上一场 body）
            self._state_cache["key"] = None
            self._state_cache["body_obj"] = None

    # ---- 污染自愈 ----------------------------------------------------------
    # 实录：内容解码器是按"每批新增 LB"跑的，增量消费时批边界可能切在一条跨多 LB
    # 的记录中间 → 解码器看到半条记录（framing_error 涨），而**身份绑定一旦错就
    # 粘住**：自机 PS 绑到同局另一个人、真人槽退化成「未知玩家」，之后自己不会回滚。
    # 同一份字节"整局一次性重解"批边界只有一个，实测 0 错误——所以按症状触发重解。
    REBUILD_ERR_STEP = 6        # 相对基线的错误增量阈值
    REBUILD_MIN_GAP = 25.0      # 同一局两次重解的最小间隔（秒）
    REBUILD_ERR_FLOOR = 3       # 低于这个绝对值当作噪音，不重解

    def _maybe_rebuild(self, v):
        """解码错误涨到阈值 → 后台整局重解并原子替换视图。"""
        if self._rebuild_busy or self._rebuild_base is None:
            try:
                self._rebuild_base = v.decode_error_count()
            except Exception:
                self._rebuild_base = 0
            return
        try:
            err = v.decode_error_count()
        except Exception:
            return
        now = time.time()
        if (err < self.REBUILD_ERR_FLOOR
                or err - self._rebuild_base < self.REBUILD_ERR_STEP
                or now - self._rebuild_last < self.REBUILD_MIN_GAP):
            return
        self._rebuild_busy = True
        self._rebuild_last = now
        run_dir, flow_dir = v.run_dir, v.flow_dir

        def _work():
            try:
                fresh = self._clean_view(run_dir, flow_dir)
                with self.lock:
                    if self.view is v:                 # 期间没被切走才替换
                        self.view = fresh
                        self._state_cache["key"] = None
                        self._state_cache["body_obj"] = None
                        self._rebuild_base = fresh.decode_error_count()
                print("[live-flow] 重解 %s/%s 清理脏上下文（解码错误 %d）"
                      % (run_dir.name, flow_dir.name, err), flush=True)
            except Exception as e:
                print("[live-flow] 重解失败:", e, flush=True)
            finally:
                self._rebuild_busy = False

        threading.Thread(target=_work, name="view-rebuild", daemon=True).start()

    def _clean_view(self, run_dir, flow_dir):
        """把这一局从原始字节一次性重解（backlog 口径）→ 干净的视图。"""
        from flowlive import IncrementalFlowLoader
        det = self.live_detectors.get((run_dir.name, flow_dir.name))
        gpaths = dict(getattr(det, "guid_paths", {}) or {}) if det else {}
        loader = IncrementalFlowLoader(flow_dir, auto_s2c=True, c2s_self=True)
        while loader.poll(max_frames=20000):
            pass
        view = LiveView(run_dir, flow_dir, loader=loader, guid_paths=gpaths)
        view.poll(max_frames=1 << 30)
        return view

    # ---- threads
    def worker_prelock(self):
        """按 run 新->旧、run 内 response.bin 大->小（每 run 前 12 个）：
        首个 n_ps>=3 的 flow 立即出图；未命中继续下一个 run。"""
        if self.pin_lock is not None:
            rd = ROOT / "captures" / self.pin_lock[0] / "flows" / self.pin_lock[1]
            if rd.is_dir():
                self._switch(rd.parent.parent, rd)
            return
        for target in self.runs_newest_first()[:8]:
            for fd in self.det.flows_by_size(target, limit=12):
                n = self.det.known(target.name, fd.name)
                if n is None:
                    n = self.det.probe_flow(fd)
                    self.det.record(target.name, fd.name, n)
                    self.det.save()
                print("[detect] %s/%s n_ps=%d" % (target.name, fd.name, n), flush=True)
                if n >= 3:
                    self._switch(target, fd)
                    return
            print("[detect] prelock miss in %s (size order), next run..." % target.name, flush=True)
        print("[detect] prelock exhausted", flush=True)

    def worker_indexer(self):
        """后台全量确定性索引所有 run 的 UDP 流；发现更大 arch131 计数则切换视图。"""
        while True:
            try:
                if self.pin_lock is not None:
                    return  # 回放固定流:禁止自动轮转换图
                for run in self.runs_newest_first():
                    bf, bfn = self.det.battle_flow_of(run.name)
                    items = sorted(self.det.udp_flows(run), key=lambda t: -t[1])  # mtime 新->旧
                    for _sz, _mt, fd in items:
                        n = self.det.known(run.name, fd.name)
                        if n is None:
                            n = self.det.probe_flow(fd)
                            self.det.record(run.name, fd.name, n)
                            self.det.save()
                        if n > bfn:
                            bf, bfn = fd.name, n
                            if self.current and self.current[0] == run.name:
                                cur_run = ROOT / "captures" / self.current[0]
                                self._switch(cur_run, cur_run / "flows" / bf)
                            elif self.current is None and n >= 3:
                                self._switch(run, run / "flows" / bf)
                    if bf and bfn > 0 and self.current is None:
                        self._switch(run, run / "flows" / bf)
            except Exception as e:
                print("[indexer] err:", e, flush=True)
            time.sleep(20)

    def worker_live(self):
        """实时抓包跟随：新 run 预锁 + 当前战斗流增长重建。"""
        while True:
            time.sleep(3)
            try:
                if self.fixed_run is None:
                    runs = self.runs_newest_first()
                    if runs and (self.current is None or runs[0].name != self.current[0]):
                        for fd in self.det.flows_by_size(runs[0], limit=5):
                            n = self.det.known(runs[0].name, fd.name)
                            if n is None:
                                n = self.det.probe_flow(fd)
                                self.det.record(runs[0].name, fd.name, n)
                                self.det.save()
                            if n >= 3:
                                self._switch(runs[0], fd)
                                break
                with self.lock:
                    v = self.view
                if v is not None:
                    v.refresh_if_grown()
            except Exception as e:
                print("[live] err:", e, flush=True)

    def worker_live_flow(self):
        """live 模式战斗流识别 —— 引擎级判据，与游戏客户端完全一致。

        UE4 客户端只有在对局 UDP 连接的控制通道(ch0)上完成握手
            Hello(C) -> Challenge(S) -> Login(C) -> Welcome(S) -> Join(C)
        收到 Welcome（"/Game/..." 地图串）后才把它当作对局会话并开始复制
        Actor。因此 战斗流 ⇔ 握手通过（handshake.Handshake.ok）。

        调度（确定性，非启发式）：
          - 候选按 (run 新->旧, flow mtime 新->旧) 全局排序；客户端同一时刻
            只有一局，全局最新的握手通过流 = 最近一局；
          - 第一道门用 handshake.scan_control（只解 ch0，<=96 帧，毫秒级，
            DNS/语音/被拒连接在门处即死）；通过者才建 LiveDetector 增量解析；
          - 早停锁定后交给 LiveView tail-follow；判定结果带备忘（正判定永久、
            负判定仅在流量静止时缓存，增长中的流每轮重扫直至收敛）；
          - 不用 size 预筛：6901B 的短命战斗流会被 64KB 门漏掉，245KB 的
            被拒连接（Failure 无 Welcome）反而会混进来——握手门两者皆正确。
        """
        RUN_WINDOW = 16            # 每轮回溯的 run 数（新->旧）
        HS_STALE_SEC = 120.0       # 负判定的可缓存条件：流量已静止
        # 扫描节拍：加了缓存后单轮只要几毫秒，所以没锁到流时更积极找新局；已经锁到
        # 流时降频，把时间还给 60Hz 增量解析（原先固定 0.25s，而单轮实测要 ~250ms，
        # 等于把增量解析挤成 ~4Hz）。
        SCAN_SEC_IDLE = 0.15
        SCAN_SEC_LOCKED = 0.5
        TICK_SEC = 1.0 / 60.0      # 当前视图增量解析节拍（本地版 60Hz 定版）
        hs_memo = {}               # flow_key -> bool（引擎门判定备忘）
        last_scan = 0.0
        # 预热 S2C 参考谱（首次 20-30ms）：放后台，别让第一局开局承担这笔开销
        def _prewarm():
            try:
                import s2c_auto
                s2c_auto.reference_spectrum()
            except Exception:
                pass
        threading.Thread(target=_prewarm, name="s2c-prewarm", daemon=True).start()
        while True:
            try:
                now = time.time()
                locked_now = self.pinned is None and self.current is not None
                if now - last_scan >= (SCAN_SEC_LOCKED if locked_now else SCAN_SEC_IDLE):
                    last_scan = now
                    locked = False
                    for run in self.runs_newest_first()[:RUN_WINDOW]:
                        udp = self.det.udp_flows(run)
                        for sz, mt, fd in sorted(udp, key=lambda t: -t[1]):
                            # 只认最近 3 分钟内还在写的流：否则会把旧对局当"当前局"自动播回放
                            if now - mt > LIVE_STALE_SEC:
                                break
                            key = "%s/%s" % (run.name, fd.name)
                            ok = hs_memo.get(key)
                            if ok is None:
                                h = hs_mod.scan_control(fd)
                                ok = h.ok
                                if ok or (now - mt) > HS_STALE_SEC:
                                    hs_memo[key] = ok
                                if ok:
                                    print("[live-flow] handshake %s (%s) rounds=%d" %
                                          (key, h.welcome, h.rounds), flush=True)
                                if not ok:
                                    continue
                            elif not ok:
                                continue
                            # 引擎门通过 -> 建/复用增量探测器并锁定（键含 run，见 _switch）
                            dkey = (run.name, fd.name)
                            d = self.live_detectors.get(dkey)
                            if d is None:
                                d = LiveDetector(fd)
                                self.live_detectors[dkey] = d
                                while len(self.live_detectors) > 32:   # 旧局探测器丢弃
                                    self.live_detectors.pop(next(iter(self.live_detectors)))
                            d.poll(max_frames=4000, early_frames=400)
                            if self.pinned is None and self.current != (run.name, fd.name):
                                self._switch(run, fd)
                                hi = d.hs_info()
                                print("[live-flow] locked %s welcome=%s rounds=%d n_ps=%d" %
                                      (key, hi["welcome"], hi["rounds"], d.poll()), flush=True)
                            locked = True
                            break
                        if locked:
                            break
                # 当前视图增量解析（60Hz：poll 空转时只付一次 stat，追赶期消化新帧）
                with self.lock:
                    v = self.view
                if v is not None and isinstance(v, LiveView):
                    v.poll(max_frames=4000)
                    self._maybe_rebuild(v)
            except Exception as e:
                print("[live-flow] err:", e, flush=True)
            time.sleep(TICK_SEC)

    # ---- api
    def api_state(self, account=""):
        with self.lock:
            v, p = self.view, self.play
        if v is None:
            return {"self": None, "entities": [], "names": [], "status": "detecting"}
        if isinstance(v, LiveView):
            return self._api_state_live(v)
        if p is None:
            return {"self": None, "entities": [], "names": [], "status": "detecting"}
        return p.snapshot()

    def _api_state_live(self, v):
        """live 模式 /api/state 应答（预编码 body 缓存）。

        前端 400ms 轮询；底层数据（slot 轨迹/实体表/死盒）在多数 tick 未变——
        指纹一致即重发上一次已编码的 body（byte-for-byte identical），完全跳过
        snapshot() 全量重建（trail 构建/死盒关联/FSM 查询 ~1ms）与 json.dumps
        （29KB ~2ms）。

        指纹 = (最新 seq, slot 采样总数, LB 总数, boxes 数, gone 数, self_slot,
                mra 锁定状态)。trail 轨迹头随 cur=latest seq 单调漂移，故 seq
        是必要成分；其余成分捕获 links/boxes/gone 变化，指纹一致即结果一致。
        """
        with v.lock:
            cur = None
            n_samples = 0
            for tr in v.slots.values():
                if tr:
                    cur = tr[-1][0] if cur is None else max(cur, tr[-1][0])
                    n_samples += len(tr)
        # 自机俯仰/瞄准朝向只有上行（C2S ch3 parse_fgpmove_params）才有，下行
        # 的玩家状态复制里不带。取不到、或超过 10s 没更新就当作没有——前端退回
        # 水平视角，而不是拿一个陈旧角度卡住镜头（真实对局里该消息每秒十几条）。
        pose = getattr(v.loader, "self_pose", None)
        pose_age = time.time() - getattr(v.loader, "self_pose_t", 0.0)
        if pose is not None and pose_age > 10.0:
            pose = None
        # 第一视角的**位置**也走上行：同一条 ch3 位姿消息里的 loc（15Hz）。
        # 下行槽位有效只有 4~6.5Hz，移动时每步跳 0.20m 中位 / 0.63m p90、间隔 p90
        # 198ms —— 第一视角"横向一顿一顿"主要来自它（旋转只有 0.3°/42ms，所以俯仰
        # 看着很顺）。上行 loc 与下行槽位水平差中位 0.0cm，可直接顶替。
        # 门槛比 pitch/yaw 紧得多：这条消息 45ms 一条，超过 1s 没来就是上行停了 ——
        # 那时必须让前端退回下行，否则相机会冻在最后一个上行坐标上。
        up_loc = None
        up_ts = None
        if pose is not None and pose_age <= 1.0 and pose.get("loc"):
            up_loc = [round(float(c), 1) for c in pose["loc"]]
            # 采样**时刻**也要发：前端拿它当插值的时间轴。只发位置的话，前端只能用
            # "收到响应的时刻"，而那个被 50ms 轮询网格量化（数据间隔才 42ms），
            # 插值会把这个量化误差重新编码成速度抖动（实测每帧速度标准差 1.36m/s，
            # 反而比不做插值还差）。带上 pose 自己的时间轴后，采样间隔是精确的。
            try:
                up_ts = round(float(pose["ts"]), 4)
            except (TypeError, ValueError, KeyError):
                up_ts = None
        key = (cur, n_samples, v.n_lb, len(v.boxes), len(v.gone),
               v.self_slot, v.mra_locked, len(v.loot),
               # 时间桶：对局销毁（流静止超 GAME_END_STALE_SEC）后物资从实体
               # 里消失的跃迁必须能穿透缓存——桶每 10s 变一次，跃迁 ≤10s 可见
               int(time.time() // 10),
               # 指纹必须同时带 yaw 与 pitch：只带 pitch 的话"平地转身"（俯仰不变）
               # 不会让指纹变化 -> 直接命中上一份 body -> 第一视角方向滞后。
               round(float(pose["pitch"]), 1) if pose else None,
               round(float(pose["yaw"]), 1) if pose else None,
               # 上行位置同样要进指纹：它每 45ms 变一次，不进指纹就会命中上一份
               # body，前端拿到旧坐标（等于把刚修好的滞后又加回去）。年龄用 1 秒桶，
               # 保证上行停发后 body 会被重建一次、把 self_up_loc 摘掉。
               (up_loc[0], up_loc[1], up_loc[2]) if up_loc else None,
               up_ts,
               min(3, int(pose_age)))
        ck = self._state_cache
        if ck["key"] == key and ck["body_obj"] is not None:
            return ck["body_obj"]
        body = v.snapshot()
        if pose:
            body["self_pitch"] = round(float(pose["pitch"]), 1)
            body["self_aim_yaw"] = round(float(pose["yaw"]), 1)
        if up_loc is not None:
            body["self_up_loc"] = up_loc
            if up_ts is not None:
                body["self_up_ts"] = up_ts
        ck["key"] = key
        ck["body_obj"] = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return ck["body_obj"]

    def api_flows(self, account=""):
        with self.det.lock:
            table = {k: dict(v) for k, v in self.det.table.items()}
        runs = []
        for run in self.runs_newest_first():
            flows = table.get(run.name) or {}
            bf, bfn = None, -1
            for name, n in flows.items():
                if n > bfn:
                    bf, bfn = name, n
            top = sorted([{"flow": k, "n_ps": v} for k, v in flows.items()],
                         key=lambda d: -d["n_ps"])[:20]
            runs.append({"run": run.name, "battle_flow": bf, "battle_n_ps": bfn, "top": top})
        return {"runs": runs, "current": self.current}

    def api_select(self, run, flow, account=""):
        rd = ROOT / "captures" / run / "flows" / flow
        if not rd.is_dir():
            return {"ok": False, "error": "no such flow"}
        if self.live:
            # live 模式：手动固定 flow，worker 不再自动抢回
            self.pinned = (run, flow)
            self._switch(rd.parent.parent, rd)
            return {"ok": True, "run": run, "flow": flow, "pinned": True}
        n = self.det.known(run, flow)
        if n is None:
            n = self.det.probe_flow(rd)
            self.det.record(run, flow, n); self.det.save()
        self.pin_lock = (run, flow)
        self._switch(rd.parent.parent, rd)
        return {"ok": True, "run": run, "flow": flow, "n_ps": n}

    def api_ctrl(self, q):
        with self.lock:
            p = self.play
        if p is None:
            return {"ok": False, "live": self.live}
        if "speed" in q:
            try:
                p.speed = max(0.0, float(q["speed"][0]))
            except ValueError:
                pass
        if "pause" in q:
            p.speed = 0.0 if q["pause"][0] == "1" else 16.0
        if "seek" in q:
            lo, hi = p.bounds()
            if lo is not None and hi is not None and hi > lo:
                frac = float(q["seek"][0]) / 1000.0
                p.cursor_seq = lo + frac * (hi - lo)
                p.last_wall = time.time()
        return {"ok": True, "speed": p.speed}


def load_map_info():
    # 多地图目录（web/map_catalog.json，由 tools/build_map_catalog.py 从官方
    # 地图小程序逆向数据 dfmap_data.json 生成）：{default, maps:[{key,name,bj,
    # rotate,width,height,centerX,centerY,minZoom,initZoom,initX,initY,
    # tileUrl,tileSize,maxNativeZoom,bounds,poi}, ...]}
    path = ROOT / "web" / "map_catalog.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 兜底：旧单图 map_markers.json（大坝）
    path = ROOT / "web" / "map_markers.json"
    if not path.exists():
        return {"default": "daba", "maps": []}
    doc = json.loads(path.read_text(encoding="utf-8"))
    info = doc.get("info") or {}
    levels = doc.get("levels") or []
    poi = [{"name": p.get("name"), "icon": p.get("icon"), "region": p.get("region"),
            "lat": p.get("lat"), "lng": p.get("lng")} for p in
           (levels[0].get("points") if levels else [])
           if p.get("lat") is not None and p.get("lng") is not None]
    single = {"key": "daba", "name": "零号大坝", "bj": info.get("bj", 128),
              "rotate": 0, "width": info.get("width"), "height": info.get("height"),
              "centerX": info.get("centerX"), "centerY": info.get("centerY"),
              "minZoom": info.get("minZoom", 3), "initZoom": info.get("initZoom", 3),
              "initX": info.get("initX"), "initY": info.get("initY"),
              "tileUrl": info.get("tileUrl"), "tileSize": info.get("tileSize", 256),
              "maxNativeZoom": info.get("maxNativeZoom", 4),
              "bounds": info.get("bounds"), "poi": poi}
    return {"default": "daba", "maps": [single]}


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script type="importmap">{"imports":{"three":"/vendor/three/build/three.module.js","three/addons/":"/vendor/three/examples/jsm/"}}</script>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#04050a">
<meta name="color-scheme" content="dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="icon" type="image/png" sizes="32x32" href="/static/mx-logo-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/mx-logo-192.png">
<link rel="apple-touch-icon" sizes="180x180" href="/static/mx-logo-180.png">
<link rel="shortcut icon" href="/static/favicon.ico">
<title>MX</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
/* ============================================================================
   DFM 战术雷达 · 前端设计系统（0901 全量重写）
   布局哲学：地图是唯一主体，一切 UI 悬浮其上且可让位。
     · 左舷 = 干员台（唯一的"人"的清单，按威胁分组排序）
     · 右舷 = 功能坞（图标轨，高频开关一击可达；低频进设置层）
     · 顶部 = 战况岛（一眼看剩余敌人，点开=完整战况）
     · 底部 = 回放轨（仅回放模式出现）
   移动端：左右两舷退化为同一个底部抽屉（分段切换），功能坞退化为底部快捷条。
   ========================================================================== */
:root{
  /* 底：近黑带一点冷蓝，让地图灰度不显脏 */
  --bg:#04050a;
  --glass:rgba(16,19,27,.72);
  --glass-solid:rgba(12,14,20,.94);
  --glass-hi:rgba(255,255,255,.055);
  --hair:rgba(255,255,255,.09);
  --hair-2:rgba(255,255,255,.16);
  --txt:#eef1f6;
  --txt-2:rgba(233,238,247,.62);
  --txt-3:rgba(233,238,247,.34);
  /* 语义色：克制到 6 个，其余全部走队伍色板 */
  --accent:#4c8dff;      /* 主操作 / 自己 */
  --ally:#37e08a;        /* 我方 */
  --warn:#ffcf4d;        /* 倒地 / 注意 */
  --danger:#ff5f6d;      /* 敌人 / 危险 */
  --box:#a97bff;         /* 死亡盒 */
  --loot:#ffb547;        /* 物资 */
  --blur:saturate(1.7) blur(24px);
  --spring:cubic-bezier(.32,.72,0,1);
  --ease:cubic-bezier(.4,0,.2,1);
  --r-lg:20px; --r-md:14px; --r-sm:10px;
  --shadow:0 18px 48px rgba(0,0,0,.62),0 2px 8px rgba(0,0,0,.4);
  --rail:60px;
  --roster-w:380px;
  --tools-w:330px;
  --gunlen:92px;
  --mks:1.8;
  --fs:1;
  --tagw:600;
  --sat:env(safe-area-inset-top,0px);
  --sab:env(safe-area-inset-bottom,0px);
  --sal:env(safe-area-inset-left,0px);
  --sar:env(safe-area-inset-right,0px);
}
*{-webkit-tap-highlight-color:transparent}
html,body{margin:0;padding:0;height:100%;overflow:hidden;background:var(--bg);color:var(--txt);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","HarmonyOS Sans SC",
    "Microsoft YaHei UI",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;touch-action:manipulation}
button,input,select{font:inherit;color:inherit}
#map{position:fixed;inset:0;background:var(--bg);z-index:1}
#hud{display:none}
.tnum{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1}
/* 顶/底边缘渐隐：让悬浮件与地图之间有层次，而不是硬贴 */
#scrim-t,#scrim-b{position:fixed;left:0;right:0;height:132px;z-index:2;pointer-events:none}
#scrim-t{top:0;background:linear-gradient(180deg,rgba(0,0,0,.62),transparent)}
#scrim-b{bottom:0;background:linear-gradient(0deg,rgba(0,0,0,.55),transparent)}

.glass{background:var(--glass);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  border:1px solid var(--hair);border-radius:var(--r-lg);box-shadow:var(--shadow)}

/* ---------------------------------------------------------------- 战况岛 */
#island{position:fixed;top:calc(var(--sat) + 12px);left:50%;transform:translateX(-50%);
  z-index:900;background:rgba(6,8,13,.92);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  border:1px solid rgba(255,255,255,.08);border-radius:26px;
  box-shadow:0 14px 44px rgba(0,0,0,.72),inset 0 1px 0 rgba(255,255,255,.06);
  cursor:pointer;overflow:hidden;min-width:150px;user-select:none;-webkit-user-select:none;
  transition:min-width .46s var(--spring),border-radius .46s var(--spring),transform .2s var(--ease)}
#island:active{transform:translateX(-50%) scale(.97)}
#island.open{min-width:352px;border-radius:30px}
#isl-main{display:flex;align-items:center;justify-content:center;gap:9px;padding:11px 18px;height:20px;
  box-sizing:content-box;white-space:nowrap}
#isl-dot{width:8px;height:8px;border-radius:50%;background:var(--ally);flex:none;
  box-shadow:0 0 0 0 rgba(55,224,138,.6);animation:pulse 2.6s var(--ease) infinite}
#island.replay #isl-dot{background:var(--accent);box-shadow:0 0 0 0 rgba(76,141,255,.6)}
#island.stale #isl-dot{background:var(--txt-3);animation:none;box-shadow:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 currentColor;opacity:1}70%{box-shadow:0 0 0 7px transparent;opacity:.55}100%{box-shadow:0 0 0 0 transparent;opacity:1}}
/* 折叠主行：敌存 / 倒地 / 阵亡 三个计数并列，数字大字、标签小字 */
.isl-stat{display:inline-flex;align-items:baseline;gap:2px;white-space:nowrap}
.isl-stat b{font-size:17px;font-weight:680;letter-spacing:.1px;line-height:1}
.isl-stat i{font-style:normal;font-size:11px;color:var(--txt-2);font-weight:500}
#isl-sep{width:1px;height:14px;background:var(--hair-2)}
#isl-near{font-size:12px;color:var(--txt-2);font-weight:500}
#isl-near b{color:var(--danger);font-weight:660}
#isl-ext{display:none;grid-template-columns:repeat(5,1fr);gap:6px;padding:2px 12px 0}
#island.open #isl-ext{display:grid;animation:slidein .42s var(--spring)}
@keyframes slidein{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}
.isl-cell{background:var(--glass-hi);border-radius:13px;padding:8px 2px;text-align:center}
.isl-cell b{display:block;font-size:16px;font-weight:660;line-height:1.15}
.isl-cell span{display:block;font-size:10px;color:var(--txt-2);margin-top:2px}
/* 该类实体当前被隐藏：数字仍然给（情报），但画成暗态并提示可点开 */
.isl-cell.off{cursor:pointer;background:rgba(255,255,255,.03)}
.isl-cell.off b{color:var(--txt-3);text-decoration:line-through;text-decoration-thickness:1px}
.isl-cell.off span{color:var(--txt-3);opacity:.7}
#isl-foot{display:none;padding:9px 14px 5px;font-size:10.5px;color:var(--txt-3);text-align:center;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#island.open #isl-foot{display:block}

/* ---------------------------------------- 右舷 · 干员台（干员上 / 物资下，同窗不切换） */
#roster{position:fixed;right:var(--sar);top:calc(var(--sat) + 12px);bottom:0;
  width:var(--roster-w);height:auto;
  z-index:600;display:flex;flex-direction:column;overflow:hidden;
  border-radius:var(--r-lg) 0 0 0;border-right:0;border-bottom:0;
  transition:transform .42s var(--spring),opacity .28s var(--ease)}
#roster.hide{transform:translateX(calc(100% + 24px));opacity:0;pointer-events:none}
/* 边缘把手：面板展开时贴在左缘，收起后停靠屏幕右缘，一键开合 */
#roster-handle{position:fixed;right:calc(var(--sar) + var(--roster-w));top:50%;
  transform:translateY(-50%);z-index:610;width:22px;height:64px;padding:0;
  display:flex;align-items:center;justify-content:center;cursor:pointer;
  border:1px solid var(--hair);border-right:0;border-radius:12px 0 0 12px;
  background:var(--glass);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  color:var(--txt-2);box-shadow:-10px 0 24px rgba(0,0,0,.38);
  transition:right .42s var(--spring),background .16s,color .16s,transform .12s}
#roster-handle:hover{background:rgba(255,255,255,.13);color:var(--txt)}
#roster-handle:active{transform:translateY(-50%) scale(.94)}
#roster-handle svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round;transition:transform .3s var(--spring)}
body.roster-hidden #roster-handle{right:var(--sar)}
body.roster-hidden #roster-handle svg{transform:rotate(180deg)}
/* 视图切换条：悬浮在干员台左上；干员台收起时贴到屏幕右缘 */
#viewswitch{position:fixed;right:calc(var(--sar) + var(--roster-w) + 10px);
  top:calc(var(--sat) + 12px);z-index:620;display:flex;align-items:center;gap:5px;
  padding:5px;border-radius:14px;transition:right .42s var(--spring)}
body.roster-hidden #viewswitch{right:calc(var(--sar) + 12px)}
.vs-seg{display:flex;gap:2px;background:rgba(0,0,0,.34);border-radius:10px;padding:3px}
.vs{border:0;background:transparent;color:var(--txt-2);font-size:12px;font-weight:600;
  padding:5px 13px;border-radius:8px;cursor:pointer;letter-spacing:.3px;
  transition:background .18s var(--ease),color .18s}
.vs:hover{color:var(--txt)}
.vs.on{background:rgba(76,141,255,.26);color:#cfe0ff;box-shadow:inset 0 0 0 1px rgba(76,141,255,.42)}
.vs-ic{width:30px;height:30px;border-radius:9px;border:0;padding:0;cursor:pointer;
  background:rgba(255,255,255,.06);color:var(--txt-2);
  display:flex;align-items:center;justify-content:center;
  transition:background .18s,color .18s,transform .12s}
.vs-ic:hover{background:rgba(255,255,255,.12);color:var(--txt)}
.vs-ic:active{transform:scale(.92)}
.vs-ic.on{background:rgba(76,141,255,.26);color:#cfe0ff;box-shadow:inset 0 0 0 1px rgba(76,141,255,.42)}
.vs-ic svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.9;
  stroke-linecap:round;stroke-linejoin:round}
body:not(.mode3d) #s-pip{opacity:.38}
.pan-head{display:flex;align-items:center;gap:10px;padding:12px 14px;flex:none;
  border-bottom:1px solid var(--hair)}
.pan-title{flex:1;min-width:0;font-size:13.5px;font-weight:620;letter-spacing:.2px;
  display:flex;align-items:center;gap:8px}
.pan-title .sub{font-weight:450;font-size:11px;color:var(--txt-3)}
.icon-btn{flex:none;width:26px;height:26px;border-radius:8px;border:1px solid transparent;
  background:var(--glass-hi);color:var(--txt-2);display:flex;align-items:center;justify-content:center;
  cursor:pointer;padding:0;transition:background .16s,color .16s,transform .12s}
.icon-btn:hover{background:rgba(255,255,255,.11);color:var(--txt)}
.icon-btn:active{transform:scale(.9)}
.icon-btn svg{width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
/* 双区分栏：干员在上、物资在下，各自独立滚动，同窗并存不再切换 */
.pan-split{display:flex;flex-direction:column;flex:1;min-height:0;overflow:hidden}
.pan-sec{display:flex;flex-direction:column;min-height:0;overflow:hidden}
.pan-sec.ops{flex:1.35 1 0}
.pan-sec.loot{flex:1 1 0;border-top:1px solid var(--hair)}
.sec-head{display:flex;align-items:center;gap:6px;padding:7px 14px 3px;flex:none}
.sec-t{font-size:12px;font-weight:620;letter-spacing:.2px;color:var(--txt-2);white-space:nowrap}
.sec-t i{font-style:normal;font-size:10.5px;color:var(--txt-3);margin-left:2px}
.pan-body{overflow-y:auto;overscroll-behavior:contain;padding:2px 0 8px;flex:1;min-height:0;
  scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.16) transparent}
.pan-body::-webkit-scrollbar{width:5px}
.pan-body::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:3px}
/* 排序条：干员区按威胁/队伍，物资区按距离/价值/品质；各自贴在自己区头右侧 */
.sortbar{display:flex;align-items:center;gap:6px;margin-left:auto;font-size:11px;color:var(--txt-3)}
.sortbar .sp{flex:1}
.mini-tab{border:1px solid var(--hair);background:transparent;color:var(--txt-3);font-size:11px;
  padding:3px 9px;border-radius:100px;cursor:pointer;transition:.16s}
.mini-tab.on{background:rgba(76,141,255,.16);border-color:rgba(76,141,255,.42);color:#a9c9ff}
/* 分组头：队伍色竖条 + 队名 + 人数 + 最近距离 */
.grp{padding:9px 14px 5px;display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none}
.grp:hover .grp-name{color:var(--txt)}
.grp-bar{width:3px;height:13px;border-radius:2px;background:var(--c,#888);flex:none;
  box-shadow:0 0 8px var(--c,#888)}
.grp-name{font-size:11.5px;font-weight:620;letter-spacing:.4px;color:var(--txt-2);transition:color .16s}
.grp-cnt{font-size:10.5px;color:var(--txt-3);background:var(--glass-hi);border-radius:6px;padding:1px 6px}
.grp-sp{flex:1}
.grp-near{font-size:10.5px;color:var(--txt-3)}
.grp-car{width:9px;height:9px;stroke:var(--txt-3);fill:none;stroke-width:2.4;stroke-linecap:round;
  stroke-linejoin:round;transition:transform .24s var(--spring)}
.grp.fold .grp-car{transform:rotate(-90deg)}
.grp-items{overflow:hidden}
.grp.fold + .grp-items{display:none}
/* 干员行：左色条 | 名字块 | 右距离块。信息按"看一眼能用"的优先级排 */
.op{display:flex;align-items:center;gap:13px;margin:0 8px 5px;padding:11px 13px;border-radius:16px;
  cursor:pointer;position:relative;transition:background .16s var(--ease),transform .12s}
.op:hover{background:rgba(255,255,255,.06)}
.op:active{transform:scale(.985)}
.op.sel{background:rgba(76,141,255,.15);box-shadow:inset 0 0 0 1px rgba(76,141,255,.34)}
.op.dead{opacity:.5}
.op.out-range{opacity:.46}
.op.out-range .op-av{filter:grayscale(.78);box-shadow:none}
.op.out-range .op-d{color:var(--txt-2)!important}
.op-av{width:52px;height:52px;border-radius:15px;flex:none;display:flex;align-items:center;
  justify-content:center;font-size:18px;font-weight:680;color:#05070c;position:relative;
  background:var(--c,#888);box-shadow:0 0 16px -2px var(--c,#888)}
/* 干员头像：盖在缩写文字上；图片缺失时 onerror 移除，自动露出文字兜底 */
.op-av img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;
  object-position:50% 20%;border-radius:inherit;pointer-events:none}
.op-av.box{border-radius:11px;transform:rotate(45deg);width:36px;height:36px;margin:8px}
/* 状态环常亮。原为 1.6s opacity 脉冲（@keyframes ring），在卡片上表现为持续闪烁，已去掉动画。 */
.op-ring{position:absolute;inset:-3px;border-radius:19px;border:2px solid var(--s)}
.op-col{flex:1;min-width:0}
.op-n{display:flex;align-items:center;gap:6px;font-size:16px;font-weight:560;line-height:1.3;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.op-n .hero{color:var(--txt-2);font-weight:450;font-size:13px}
.op-m{display:flex;align-items:center;gap:6px;margin-top:3px;font-size:12px;color:var(--txt-3);
  white-space:nowrap;overflow:hidden}
.op-m .wp{color:var(--txt-2)}
.badge{font-size:11px;font-weight:640;padding:2px 7px;border-radius:6px;line-height:1.45;flex:none;
  color:#05070c;background:var(--b,#888)}
.badge.ghost{background:transparent;color:var(--b);border:1px solid var(--b)}
.op-r{flex:none;text-align:right;min-width:64px}
.op-d{font-size:19px;font-weight:640;line-height:1.1;letter-spacing:-.2px}
.op-d .du{font-size:10px;color:var(--txt-3)}
.op-z{font-size:11.5px;color:var(--txt-3);margin-top:2px}
.op-z.up{color:#79d2ff}.op-z.dn{color:#ffa8a8}
.empty{padding:26px 16px;text-align:center;color:var(--txt-3);font-size:12px;line-height:1.7}
.empty svg{width:26px;height:26px;stroke:var(--txt-3);fill:none;stroke-width:1.5;opacity:.55;
  margin-bottom:8px;display:block;margin-left:auto;margin-right:auto}
/* 迷你血条（干员行内）：单一连续填充层，宽度直接对应 0–100 剩余血量 */
.op-hp{display:block;width:100%;height:4px;margin-top:5px;border-radius:2px;overflow:hidden;
  background:rgba(255,255,255,.07);box-shadow:inset 0 0 0 1px rgba(255,255,255,.025)}
.op-hp>i{display:block;height:100%;border-radius:inherit;transition:width .16s linear,background-color .16s linear}
.op-m .hpv{font-weight:640;letter-spacing:-.1px}

/* ------------------------------------------------------------ 物资清单 */
.loot-row{display:flex;align-items:center;gap:10px;margin:0 8px 3px;padding:7px 9px;border-radius:12px;
  cursor:pointer;transition:background .16s}
.loot-row:hover{background:rgba(255,255,255,.06)}
.loot-row.sel{background:rgba(255,181,71,.14);box-shadow:inset 0 0 0 1px rgba(255,181,71,.4)}
.loot-thumb{position:relative;display:inline-flex;align-items:center;justify-content:center;flex:none;
  width:34px;height:34px;color:var(--c);background:#151925;border:1px solid var(--c);border-radius:9px;
  box-sizing:border-box;font-size:11px;font-weight:650;overflow:hidden}
.loot-thumb img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;padding:3px;
  box-sizing:border-box;background:#151925}
.loot-col{flex:1;min-width:0}
.loot-n{display:flex;align-items:center;gap:6px;font-size:12.5px;font-weight:540;white-space:nowrap}
.loot-name{overflow:hidden;text-overflow:ellipsis}
.loot-qty{flex:none;font-size:10px;font-weight:650;color:var(--txt-2);font-variant-numeric:tabular-nums}
.loot-m{display:flex;flex-wrap:wrap;gap:2px 7px;font-size:10.5px;color:var(--txt-3);margin-top:3px}
.loot-state{color:var(--warn)}
.loot-d{flex:none;text-align:right;font-size:12.5px;font-weight:600;color:var(--txt-2)}
.loot-d small{display:block;font-size:10px;margin-top:3px;color:var(--txt-3)}
#loot-filter-hint{margin-left:auto;white-space:nowrap}

/* ------------------------------------- 左舷 · 功能台（设置项按板块平铺，开关即按钮） */
#dock{position:fixed;left:var(--sal);top:calc(var(--sat) + 12px);bottom:0;
  width:var(--tools-w);z-index:600;display:flex;flex-direction:column;overflow:hidden;
  border-radius:0 var(--r-lg) 0 0;border-left:0;border-bottom:0;
  transition:transform .42s var(--spring),opacity .28s var(--ease)}
#dock.hide{transform:translateX(calc(-100% - 24px));opacity:0;pointer-events:none}
/* 边缘把手：面板展开时贴在右缘，收起后停靠屏幕左缘 */
#dock-handle{position:fixed;left:var(--tools-w);top:50%;
  transform:translateY(-50%);z-index:610;width:22px;height:64px;padding:0;
  display:flex;align-items:center;justify-content:center;cursor:pointer;
  border:1px solid var(--hair);border-left:0;border-radius:0 12px 12px 0;
  background:var(--glass);-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  color:var(--txt-2);box-shadow:10px 0 24px rgba(0,0,0,.38);
  transition:left .42s var(--spring),background .16s,color .16s,transform .12s}
#dock-handle:hover{background:rgba(255,255,255,.13);color:var(--txt)}
#dock-handle:active{transform:translateY(-50%) scale(.94)}
#dock-handle svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round;transition:transform .3s var(--spring)}
body.dock-hidden #dock-handle{left:var(--sal)}
body.dock-hidden #dock-handle svg{transform:rotate(180deg)}
/* 整行可点（如切换地图），与开关行同构 */
.row-btn{width:calc(100% - 16px);border:0;background:transparent;text-align:left;cursor:pointer;
  font:inherit;color:inherit}
.row-btn:hover{background:rgba(255,255,255,.045)}
.row-go{flex:none;font-size:16px;color:var(--txt-3)}
.dk{position:relative;height:46px;border:0;background:transparent;color:var(--txt-2);border-radius:15px;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;
  cursor:pointer;padding:0;transition:background .18s var(--ease),color .18s,transform .12s}
.dk:hover{background:rgba(255,255,255,.07);color:var(--txt)}
.dk:active{transform:scale(.9)}
.dk svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.85;
  stroke-linecap:round;stroke-linejoin:round}
.dk span{font-size:9px;font-weight:520;letter-spacing:.2px;opacity:.9}
.dk.on{background:rgba(76,141,255,.2);color:#addbff;box-shadow:inset 0 0 0 1px rgba(76,141,255,.34)}
.dk.on svg{stroke:#addbff}
.dk-div{height:1px;background:var(--hair);margin:4px 10px}
/* 桌面 hover 提示（移动端不显示） */
.dk::after{content:attr(data-tip);position:absolute;left:calc(100% + 12px);top:50%;
  transform:translateY(-50%) scale(.94);white-space:nowrap;font-size:11.5px;padding:5px 9px;
  background:var(--glass-solid);border:1px solid var(--hair);border-radius:9px;color:var(--txt);
  opacity:0;pointer-events:none;transition:opacity .16s,transform .16s var(--spring);
  box-shadow:0 8px 22px rgba(0,0,0,.5)}
.dk:hover::after{opacity:1;transform:translateY(-50%) scale(1)}

/* -------------------------------------------------------------- 浮层 */
#veil{position:fixed;inset:0;z-index:800;background:rgba(0,0,0,.5);opacity:0;pointer-events:none;
  transition:opacity .3s var(--ease);-webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px)}
#veil.on{opacity:1;pointer-events:auto}
.sheet{position:fixed;z-index:820;display:flex;flex-direction:column;overflow:hidden;
  left:calc(var(--sal) + var(--tools-w) + 12px);top:calc(var(--sat) + 12px);
  width:330px;max-height:calc(100% - 96px - var(--sat));
  transform:translateX(-18px) scale(.97);opacity:0;pointer-events:none;
  transition:transform .38s var(--spring),opacity .26s var(--ease)}
.sheet.on{transform:none;opacity:1;pointer-events:auto}
.sheet-body{overflow-y:auto;overscroll-behavior:contain;padding:6px 0 12px;
  scrollbar-width:thin;scrollbar-color:rgba(255,255,255,.16) transparent}
.sheet-body::-webkit-scrollbar{width:5px}
.sheet-body::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:3px}
.sec{padding:12px 14px 4px;font-size:10.5px;font-weight:620;letter-spacing:.9px;color:var(--txt-3);
  text-transform:uppercase}
/* 设置行：左标题+说明，右开关。避免"一堆同权重按钮" */
.row{display:flex;align-items:center;gap:10px;margin:0 8px 2px;padding:9px 10px;border-radius:12px;
  transition:background .16s}
.row:hover{background:rgba(255,255,255,.045)}
.row-col{flex:1;min-width:0}
.row-t{font-size:13px;font-weight:520;line-height:1.35}
.row-s{font-size:10.5px;color:var(--txt-3);margin-top:1px;line-height:1.4}
/* iOS 风开关 */
.sw{flex:none;width:42px;height:25px;border-radius:100px;border:0;padding:0;cursor:pointer;
  background:rgba(120,120,128,.3);position:relative;transition:background .28s var(--ease)}
.sw::after{content:"";position:absolute;top:2.5px;left:2.5px;width:20px;height:20px;border-radius:50%;
  background:#fff;box-shadow:0 2px 5px rgba(0,0,0,.35);transition:transform .3s var(--spring)}
.sw.on{background:var(--ally)}
.sw.on::after{transform:translateX(17px)}
.sw:active::after{width:23px}
/* 滑块行 */
.sld{display:flex;align-items:center;gap:10px;margin:0 8px 6px;padding:4px 10px 10px}
.sld input[type=range]{flex:1;accent-color:var(--accent);height:3px}
.sld .v{flex:none;width:38px;text-align:right;font-size:11.5px;color:var(--txt-2)}
/* 选择组（物资门槛等）：分段按钮 */
.pick{display:flex;gap:4px;margin:0 12px 8px;padding:3px;background:rgba(0,0,0,.3);
  border-radius:11px;border:1px solid var(--hair)}
.pick button{flex:1;border:0;background:transparent;color:var(--txt-3);font-size:11.5px;font-weight:540;
  padding:6px 2px;border-radius:8px;cursor:pointer;transition:.18s var(--ease)}
.pick button.on{background:rgba(255,255,255,.12);color:var(--txt)}
/* 地图选择：卡片网格（图形化识别比下拉框可靠得多） */
.maps{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:6px 12px 12px}
.mcard{position:relative;border-radius:13px;overflow:hidden;cursor:pointer;aspect-ratio:4/3;
  border:1px solid var(--hair);background:#0a0d14;transition:transform .18s var(--spring),border-color .18s}
.mcard:hover{transform:translateY(-2px);border-color:var(--hair-2)}
.mcard.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent),0 8px 22px rgba(76,141,255,.24)}
.mcard img{width:100%;height:100%;object-fit:cover;opacity:.72;transition:opacity .2s}
.mcard.on img,.mcard:hover img{opacity:.95}
.mcard b{position:absolute;left:0;right:0;bottom:0;padding:14px 8px 6px;font-size:11.5px;font-weight:600;
  background:linear-gradient(0deg,rgba(0,0,0,.88),transparent)}
.mcard .now{position:absolute;top:6px;right:6px;width:16px;height:16px;border-radius:50%;
  background:var(--accent);display:none;align-items:center;justify-content:center}
.mcard.on .now{display:flex}
.mcard .now svg{width:9px;height:9px;stroke:#fff;stroke-width:3;fill:none;stroke-linecap:round;stroke-linejoin:round}

/* -------------------------------------------------------------- 回放轨 */
/* 回放控制条整体隐藏（不需要播放/倍速控制） */
#playbar{position:fixed;left:calc(var(--sal) + var(--tools-w) + 12px);
  right:calc(var(--sar) + var(--roster-w) + 12px);margin:0 auto;
  bottom:calc(var(--sab) + 14px);z-index:700;max-width:760px;
  display:flex;align-items:center;gap:9px;padding:9px 14px;border-radius:26px}
#playbar.hide{display:none}
#btn-play{width:36px;height:36px;padding:0;border-radius:50%;flex:none;border:0;cursor:pointer;
  background:rgba(255,255,255,.13);color:var(--txt);display:flex;align-items:center;justify-content:center;
  transition:transform .12s,background .18s}
#btn-play:hover{background:rgba(255,255,255,.2)}
#btn-play:active{transform:scale(.9)}
#btn-play svg{width:14px;height:14px;fill:currentColor;stroke:none}
#seek{flex:1;min-width:110px;accent-color:var(--accent);height:3px}
#tlabel{font-size:11.5px;color:var(--txt-2);white-space:nowrap}
.spd{display:flex;gap:2px;padding:3px;background:rgba(0,0,0,.3);border-radius:11px;flex:none}
.spd button{border:0;background:transparent;color:var(--txt-3);font-size:11px;font-weight:560;
  padding:5px 8px;border-radius:8px;cursor:pointer;transition:.16s}
.spd button.on{background:rgba(255,255,255,.13);color:var(--txt)}

/* ------------------------------------------------------ 移动端底部快捷条 */
#quick{display:none;position:fixed;left:50%;transform:translateX(-50%);
  bottom:calc(var(--sab) + 12px);z-index:660;padding:5px;gap:2px;border-radius:20px}
#quick.hide{display:none!important}
.qk{border:0;background:transparent;color:var(--txt-2);border-radius:15px;cursor:pointer;padding:0;
  width:56px;height:44px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;
  transition:background .18s,color .18s,transform .12s}
.qk:active{transform:scale(.9)}
.qk svg{width:17px;height:17px;stroke:currentColor;fill:none;stroke-width:1.85;
  stroke-linecap:round;stroke-linejoin:round}
.qk span{font-size:9px;font-weight:520}
.qk.on{background:rgba(76,141,255,.2);color:#addbff}

/* ------------------------------------------------------------ 地图元素 */
/* 干员标记：朝向锥 + 核心点 + 状态环。yaw 已按地图 rotate 换算到屏幕角 */
.mk{position:relative;width:26px;height:26px;display:flex;align-items:center;justify-content:center;
  transform:scale(var(--mks,1))}
.mk-cone{position:absolute;left:50%;top:50%;width:12px;height:14px;
  fill:var(--c);stroke:#04050a;stroke-width:2;stroke-linejoin:round;
  opacity:1;pointer-events:none;z-index:-1;
  transform-origin:50% 100%;
  transform:translate(-50%,-100%) rotate(var(--rot,0deg)) translateY(-4px);
}
/* 枪线：从朝向中心沿射向延伸的细线，与朝向锥同角度、同显隐条件 */
.mk-gun{position:absolute;left:50%;top:50%;width:1.8px;height:var(--gunlen,92px);border-radius:1px;
  background:linear-gradient(180deg,var(--c) 0%,var(--c) 70%,transparent 100%);
  transform-origin:50% 100%;
  transform:translate(-50%,-100%) rotate(var(--rot,0deg));
  opacity:.95;pointer-events:none;filter:drop-shadow(0 0 4px var(--c))}
.mk.dead .mk-gun,.mk.spawn .mk-gun{display:none}
.mk.out-range .mk-gun{opacity:.12;filter:none}
.mk-core{position:absolute;left:50%;top:50%;width:13px;height:13px;border-radius:50%;
  transform:translate(-50%,-50%);background:var(--c);z-index:10;
  box-shadow:0 0 0 2px rgba(4,5,10,.9),0 0 12px -1px var(--c)}
.mk-team{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
  color:#fff;font-weight:800;font-size:8px;line-height:1;z-index:11;pointer-events:none;
  text-shadow:-1px 0 #000,1px 0 #000,0 -1px #000,0 1px #000}
.tag .hero{opacity:1}.tag .wpn{opacity:1}
.dst{font-size:calc(11px*var(--fs,1));font-weight:800;white-space:nowrap;pointer-events:none;
  color:var(--c);text-shadow:-1.5px 0 #000,1.5px 0 #000,0 -1.5px #000,0 1.5px #000,-1.5px -1.5px #000,1.5px -1.5px #000,-1.5px 1.5px #000,1.5px 1.5px #000;opacity:.95;
  width:100%;text-align:center;box-sizing:border-box}
.dst.out-range{opacity:.4}
.dst .dz{font-weight:900;-webkit-text-stroke:.45px currentColor;display:inline-block;transform:scaleY(1.12)}
/* 有干员头像时用头像代替核心圆点；图片缺失 onerror 移除后回落为圆点缺失的极简态 */
.mk-face{position:absolute;left:50%;top:50%;width:13px;height:13px;z-index:10;
  min-width:13px;max-width:13px;min-height:13px;max-height:13px;flex:none;aspect-ratio:1/1;
  border-radius:50%;transform:translate(-50%,-50%);object-fit:cover;object-position:50% 22%;
  border:1.5px solid var(--c);background:#0b0e14;
  box-shadow:0 0 0 1px rgba(4,5,10,.9),0 0 10px -1px var(--c);pointer-events:none}
.mk.st .mk-face{box-shadow:0 0 0 1px rgba(4,5,10,.9),0 0 0 3px var(--s),0 0 12px var(--s)}
.mk.out-range .mk-face{filter:grayscale(.78)}
.mk.spawn .mk-face{opacity:.9;border-style:dashed}
.mk.st .mk-core{box-shadow:0 0 0 2px rgba(4,5,10,.9),0 0 0 4px var(--s),0 0 14px var(--s)}
.mk.self .mk-core{width:13px;height:13px;z-index:10}
.mk.self::before{content:"";position:absolute;left:50%;top:50%;width:34px;height:34px;
  margin:-17px 0 0 -17px;border-radius:50%;border:1.5px solid var(--c);opacity:.4;
  animation:sonar 2.8s var(--ease) infinite}
@keyframes sonar{0%{transform:scale(.35);opacity:.75}100%{transform:scale(1);opacity:0}}
.mk.dead{opacity:.55}
.mk.dead .mk-cone{display:none}
.mk.out-range{opacity:.28;filter:grayscale(.78)}
.mk.out-range .mk-cone{opacity:.12;filter:none}
.mk.out-range .mk-core{box-shadow:0 0 0 1px rgba(4,5,10,.9),0 0 4px -1px var(--c)}
/* 出生点标记（PVE AI 唯一可得坐标）：空心环 + 虚线，一眼区别于实时点 */
.mk.spawn .mk-core{background:transparent;border:2px dashed var(--c);width:13px;height:13px;
  box-shadow:0 0 0 1px rgba(4,5,10,.85),0 0 10px -2px var(--c);opacity:.92}
.mk.spawn .mk-cone{display:none}
.mk.mark .mk-core{box-shadow:0 0 0 2px #ff2d3f,0 0 0 4px rgba(255,45,63,.5),0 0 14px #ff2d3f}
.mk.mark .mk-face{box-shadow:0 0 0 1px rgba(4,5,10,.9),0 0 0 3px #ff2d3f,0 0 12px #ff2d3f}
.mk.mark .mk-cone{fill:#ff2d3f}
.mk.mark .mk-gun{background:linear-gradient(180deg,#ff2d3f 0%,#ff2d3f 70%,transparent 100%)}
.dbox{width:13px;height:13px;background:var(--box);border:2px solid rgba(255,255,255,.85);
  border-radius:3px;box-shadow:0 0 12px var(--box);transform:rotate(45deg)}
/* 头顶信息条：一行「状态·队号·装备·干员·距离」 */
.tb-wrap{display:flex;justify-content:center;align-items:flex-end}
.tag{--bg:rgba(6,9,14,var(--tagop,.8));--bd:rgba(255,255,255,calc(var(--tagop,.8)*.28));
  position:relative;height:calc(24px*var(--fs,1));padding:0 calc(2px*var(--fs,1));border-radius:0;
  color:var(--tc,#fff);background:transparent;
  font-weight:800;font-size:calc(14px*var(--fs,1));line-height:calc(24px*var(--fs,1));
  font-family:-apple-system,"SF Pro Text","PingFang SC",system-ui,sans-serif;
  letter-spacing:.2px;white-space:nowrap;border:0;
  box-shadow:none;pointer-events:none;
  text-shadow:-2px 0 #000,2px 0 #000,0 -2px #000,0 2px #000,-2px -2px #000,2px -2px #000,-2px 2px #000,2px 2px #000;
  display:flex;align-items:center;gap:5px}
.tag i{font-style:normal;opacity:1;font-weight:inherit}
/* 头盔/护甲：显示耐久，颜色按装备等级（1灰 2绿 3蓝 4紫 5金 6红） */
.tag .gear{opacity:1;font-weight:650}
.tag .gear em{font-style:normal;opacity:.65;font-weight:500;margin-right:2px}
.tag .tm{font-weight:var(--tagw,600)}
.tag .sp{color:var(--warn);font-weight:var(--tagw,600);opacity:.95}
.tag .far{color:var(--warn);font-weight:var(--tagw,600);opacity:1}
.tag.out-range{opacity:.48}
.tag::after{display:none}
/* 贴边保护：超长组合（干员+武器+头甲+距离）不把信息条推出视口。
   桌面留足宽度（信息完整优先），窄屏才收紧到半屏宽。 */
.tag{max-width:420px;overflow:hidden}
.tag i,.tag>span:not(.tm){overflow:hidden;text-overflow:ellipsis;min-width:0}
@media(max-width:900px){ .tag{max-width:min(62vw,300px)} }
/* 敌人标识：仅敌队玩家显示，红底白字，压在信息条正上方。
   尺寸跟随「字体大小」设置（--fs），与信息条/护甲/血条同步缩放。
   2D 的 divIcon 容器不会自动居中，用 max-content + auto margin 自己居中。 */
.foe{display:block;width:max-content;margin:0 auto;padding:0 calc(6px*var(--fs,1));
  border-radius:4px;background:#ff2d3f;color:#fff;font-size:calc(9px*var(--fs,1));
  font-weight:700;letter-spacing:.4px;line-height:calc(14px*var(--fs,1));
  white-space:nowrap;box-shadow:0 1px 5px rgba(0,0,0,.5);
  text-shadow:0 1px 1px rgba(0,0,0,.55)}
/* 敌人倒地：标识由红色「敌人」换成橙色「倒地」，2D/3D 共用这条规则 */
.foe.down{background:#ff8a1e}
/* 头顶血条：单一连续填充层，宽度直接对应 0–100 剩余血量。 */
.hpb{display:block;width:64px;height:5px;pointer-events:none;border-radius:3px;overflow:hidden;
  background:rgba(6,8,14,.72);box-shadow:0 0 0 1px rgba(0,0,0,.55),0 1px 3px rgba(0,0,0,.75);
  transform:scale(var(--fs,1))}
.hpb.out-range{opacity:.34;filter:grayscale(.8)}
.hpb>i{display:block;height:100%;border-radius:inherit;transition:width .16s linear,background-color .16s linear}
/* 装备行：头盔/护甲耐久，颜色按等级；位于标记下方、血条上方，带底便于辨认 */
.grb{display:flex;gap:6px;justify-content:center;align-items:center;height:13px;
  width:fit-content;margin:0 auto;padding:0 6px;border-radius:4px;
  background:rgba(6,8,14,.74);box-shadow:0 0 0 1px rgba(0,0,0,.55),0 1px 3px rgba(0,0,0,.7);
  font-weight:650;font-size:calc(10px*var(--fs,1));line-height:13px;
  font-family:-apple-system,"SF Pro Text","PingFang SC",system-ui,sans-serif;
  letter-spacing:.2px;white-space:nowrap;pointer-events:none;
  text-shadow:0 1px 2px rgba(0,0,0,.95);transform:scale(var(--fs,1))}
.grb em{font-style:normal;opacity:.6;font-weight:500;margin-right:2px}
.grb .cap{opacity:.72;font-weight:500;margin-left:2px}
.grb.out-range{opacity:.34;filter:grayscale(.8)}
.loot-mk{border-radius:8px;background:rgba(8,10,16,.66);border:1px solid rgba(255,255,255,.24);
  box-shadow:0 2px 10px rgba(0,0,0,.6)}
.loot-mk.sel{border-color:var(--loot);box-shadow:0 0 16px rgba(255,181,71,.8)}
.loot-point{background:transparent;border:0}
.loot-point .loot-thumb{width:30px;height:30px;border-radius:8px;box-shadow:0 2px 7px #0008}
.loot-point.sel .loot-thumb{border-width:2px;box-shadow:0 0 0 2px #fff,0 0 14px var(--c)}
.loot-point.candidate .loot-thumb{border-style:dashed}
.loot-point .loot-qty{position:absolute;right:-6px;bottom:-5px;padding:1px 4px;background:#101520;
  border:1px solid #ffffff4d;border-radius:5px;color:#fff;font-size:9px;line-height:12px}
.loot-point .loot-question{position:absolute;left:-4px;top:-5px;width:13px;height:13px;line-height:13px;
  background:#ffcf4d;color:#16191e;text-align:center;border-radius:50%;font-size:10px;font-weight:750}
/* Leaflet 主题覆写 */
.leaflet-container{background:var(--bg);font-family:inherit}
.leaflet-tooltip{background:var(--glass-solid);border:1px solid var(--hair);border-radius:12px;
  color:var(--txt);box-shadow:var(--shadow);padding:9px 11px;font-size:11.5px;line-height:1.6;
  -webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur)}
.leaflet-tooltip:before{display:none}
.tip-h{font-size:12.5px;font-weight:620;margin-bottom:4px;display:flex;align-items:center;gap:6px}
.tip-r{color:var(--txt-2)}
.tip-r b{color:var(--txt);font-weight:560}
.leaflet-control-zoom{border:0!important;box-shadow:none!important;margin:0!important}
.leaflet-bottom.leaflet-right{bottom:calc(var(--sab) + 14px);right:calc(var(--sar) + var(--roster-w) + 12px)}
.leaflet-control-zoom a{background:var(--glass)!important;color:var(--txt)!important;border:1px solid var(--hair)!important;
  border-radius:12px!important;margin:3px!important;width:34px!important;height:34px!important;
  line-height:32px!important;font-size:17px!important;
  -webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);box-shadow:var(--shadow)}
.leaflet-control-zoom a:hover{background:var(--glass-solid)!important}
#map.mini{left:calc(var(--sal) + 12px);top:auto;right:auto;
  bottom:calc(var(--sab) + 76px);width:min(44vw,300px);height:min(28vh,208px);z-index:5;
  border:1px solid var(--hair);border-radius:18px;box-shadow:var(--shadow);overflow:hidden}
/* ------------------------------------------------------------ 3D 视图 */
/* 3D 与 2D 同层（z=1）：切换时 2D 缩成左下角小地图，主体让给白模场景 */
#r3{position:fixed;inset:0;z-index:1;display:none;background:#05070c}
#r3.on{display:block}
#r3-cv{position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none}
#r3-lbl{position:absolute;inset:0;overflow:hidden;pointer-events:none;contain:strict}
/* 敌人连线层：从屏幕上方中心点连到每个敌人的头部（仅 3D + 开关开启时由 JS 画线）。
   stroke 由 JS 逐条写成该敌人队伍的颜色，所以这里不设 stroke；只留线宽与透明度
   兜底（JS 用 style 写，优先级高于本表，但万一没写也不至于是条黑线）。 */
#ray-layer{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
#ray-layer line{stroke-width:1.6;stroke-opacity:.8}
/* 屏幕外预警层：屏幕中心一个基准圆 + 指向画面外敌人的箭头（含距离数字）。
   仅 3D + 开关开启时由 r3d.js 画；圆半径由「预警圆半径」滑块调。 */
#warn-layer{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;overflow:visible}
#warn-layer .ring{fill:none;stroke:rgba(255,255,255,.20);stroke-width:1;stroke-dasharray:5 7}
#warn-layer .arw{stroke:rgba(0,0,0,.5);stroke-width:1;stroke-linejoin:round}
/* 贴脸红点：画在箭头前方（更外侧），固定红色 —— 它表达的是"已经很近"，
   不是队伍信息，所以不跟队伍色走。带一圈暗描边，压在亮底/天空上也看得清。 */
#warn-layer .dotw{fill:#ff2d3f;stroke:rgba(0,0,0,.55);stroke-width:1.5}
#warn-layer text{font:700 11px/1 -apple-system,"PingFang SC",system-ui;letter-spacing:.2px;
  text-anchor:middle;dominant-baseline:middle;
  paint-order:stroke;stroke:rgba(0,0,0,.75);stroke-width:3px;stroke-linejoin:round}
/* 敌人连线开关只在 3D 模式出现 */
body:not(.mode3d) #s-ray{display:none}
/* 屏幕外预警开关与人物方框开关同样只在 3D 模式出现 */
body:not(.mode3d) #s-warn3d{display:none}
body:not(.mode3d) #s-box3d{display:none}
/* 3D 头顶状态栏：做法对齐 2D 的 .tag —— 半透明深底 + 1px 亮边 + 投影，
   隔墙 / 贴地 / 亮天空背景下都读得清。底色挂在 .bd（内容行）而不是 .r3-lbl
   整体上：红底「敌人」徽标是 .r3-lbl 的第一个块，框在外层会把它一起框进黑底，
   而 2D 里它是浮在标签之上的。透明度沿用 2D 的「标签背景」滑块（--tagop）。 */
.r3-lbl{position:absolute;left:0;top:0;white-space:nowrap;will-change:transform;text-align:center;
  font:600 11px/1.2 -apple-system,"PingFang SC",system-ui;letter-spacing:.2px;
  text-shadow:0 1px 3px #000,0 0 8px rgba(0,0,0,.85)}
.r3-lbl .bd{padding:2px 7px;border-radius:7px;
  background:rgba(6,9,14,calc(var(--tagop,.8)*.82));
  box-shadow:0 2px 8px rgba(0,0,0,.55),inset 0 0 0 1px rgba(255,255,255,calc(var(--tagop,.8)*.3))}
.r3-lbl .l1{white-space:nowrap}
.r3-lbl b{font-size:13px}   /* 3D 状态字大一号 */
.r3-lbl .l2{white-space:nowrap;font-size:12px;font-weight:780;opacity:.98;margin-top:1px}
.r3-lbl .l2 span{margin:0 3px}
.r3-lbl .hp{width:52px;height:4px;margin:2px auto 0;border-radius:2px;overflow:hidden;
  background:rgba(6,8,14,.72);box-shadow:0 0 0 1px rgba(0,0,0,.5)}
.r3-lbl .hp>i{display:block;height:100%}
/* 3D 距离标注：独立元素挂在人物【脚下】，不占头顶信息行；由「距离」开关控制。
   同样给个底：脚部数字常压在地板/草地纹理上，纯描边在亮底上会糊。 */
.r3-dst{position:absolute;left:0;top:0;white-space:nowrap;will-change:transform;text-align:center;
  font:700 11px/1.2 -apple-system,"PingFang SC",system-ui;letter-spacing:.2px;
  padding:1px 6px;border-radius:6px;
  background:rgba(6,9,14,calc(var(--tagop,.8)*.82));
  box-shadow:0 2px 8px rgba(0,0,0,.55),inset 0 0 0 1px rgba(255,255,255,calc(var(--tagop,.8)*.3));
  text-shadow:0 1px 3px #000,0 0 8px rgba(0,0,0,.85)}
/* 载入进度：模型 16~37MB，必须有确定性的等待反馈 */
#r3-load{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);z-index:600;display:none;
  min-width:216px;padding:16px 20px;border-radius:18px;text-align:center;
  background:var(--glass-solid);border:1px solid var(--hair);box-shadow:var(--shadow);
  -webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur)}
#r3-load.on{display:block}
#r3-load-t{font-size:12.5px;font-weight:560;color:var(--txt-2);margin-bottom:9px}
#r3-bar{height:4px;border-radius:100px;background:rgba(255,255,255,.1);overflow:hidden}
#r3-bar i{display:block;height:100%;width:0;border-radius:100px;
  background:linear-gradient(90deg,var(--accent),#7fb2ff);transition:width .18s linear}
/* 相机模式：3D 的一等控制，浮在底部中央 */
#camseg{position:fixed;left:50%;bottom:calc(var(--sab) + 14px);transform:translateX(-50%) translateY(8px);
  z-index:520;display:none;gap:2px;padding:4px;border-radius:100px;opacity:0;
  transition:opacity .24s var(--ease),transform .34s var(--spring)}
#camseg.on{display:flex;opacity:1;transform:translateX(-50%) translateY(0)}
#camseg button{border:0;background:transparent;color:var(--txt-2);cursor:pointer;
  padding:6px 13px;border-radius:100px;font-size:12px;font-weight:560;
  transition:background .18s var(--ease),color .18s var(--ease)}
#camseg button.on{background:rgba(76,141,255,.22);color:#addbff;box-shadow:inset 0 0 0 1px rgba(76,141,255,.32)}
body.mode3d #playbar{bottom:calc(var(--sab) + 62px)}
body.mode3d #toast{bottom:calc(var(--sab) + 122px)}
/* 3D 下的 2D 小地图：干员台已移到右舷，小地图统一放左下（左舷现在是空的）。
   小地图只回答「我在图上哪」—— 头顶信息条/血条/缩放键在这个尺寸里全是噪声。 */
body.mode3d #map.mini{left:calc(var(--sal) + var(--tools-w) + 12px);right:auto;
  bottom:calc(var(--sab) + 62px);width:min(26vw,264px);height:min(26vh,196px)}
body.mode3d .leaflet-control-zoom{display:none!important}
/* 3D 小地图操作说明（跟随小地图位置） */
#mini-hint{position:fixed;z-index:640;display:none;pointer-events:none;padding:3px 8px;
  border-radius:8px;background:rgba(6,8,14,.84);border:1px solid var(--hair);
  color:var(--txt-2);font-size:10.5px;line-height:1.5;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;-webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur)}
#mini-hint.on{display:block}
.dk.mode.on{background:rgba(169,123,255,.22);color:#d8c2ff;box-shadow:inset 0 0 0 1px rgba(169,123,255,.36)}
.dk.mode.on svg{stroke:#d8c2ff}
/* manager 注入的账号角标：接管样式，避免与新布局打架 */
#acctbadge{left:calc(var(--sal) + 12px)!important;bottom:calc(var(--sab) + 12px)!important;
  padding:6px 11px!important;border-radius:100px!important;font:500 11px/1.4 -apple-system,"PingFang SC",system-ui!important;
  color:var(--txt-2)!important;background:var(--glass)!important;border:1px solid var(--hair)!important;
  -webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);box-shadow:var(--shadow)!important;z-index:520!important}
/* 提示条（切图/定位等瞬时反馈） */
#toast{position:fixed;left:50%;bottom:calc(var(--sab) + 76px);transform:translateX(-50%) translateY(10px);
  z-index:960;padding:9px 16px;border-radius:100px;font-size:12.5px;font-weight:520;
  background:var(--glass-solid);border:1px solid var(--hair);box-shadow:var(--shadow);
  -webkit-backdrop-filter:var(--blur);backdrop-filter:var(--blur);
  opacity:0;pointer-events:none;transition:opacity .26s var(--ease),transform .34s var(--spring)}
#toast.on{opacity:1;transform:translateX(-50%) translateY(0)}

/* ------------------------------------------------------------ 响应式 */
@media(max-width:1180px){ :root{--roster-w:330px} .sheet{width:308px} }
@media(max-width:900px){
  /* 抽屉必须在 #veil(z=800) 之上，否则遮罩的 backdrop-filter 会把抽屉自身糊掉 */
  #roster{left:0;right:0;top:auto;bottom:0;width:auto;height:min(64vh,74%);z-index:840;
    border-radius:22px 22px 0 0;border-bottom:0;transform:translateY(101%);opacity:1;
    transition:transform .44s var(--spring)}
  #roster.hide{transform:translateY(101%);opacity:1}
  #roster.up{transform:translateY(0)}
  #roster .pan-sec.loot .pan-body{padding-bottom:calc(12px + var(--sab))}
  .sheet{left:0;right:0;top:auto;bottom:0;width:auto;max-height:min(74vh,80%);
    border-radius:22px 22px 0 0;border-bottom:0;transform:translateY(101%);opacity:1;
    transition:transform .44s var(--spring)}
  .sheet.on{transform:translateY(0);opacity:1}
  .sheet-body{padding-bottom:calc(12px + var(--sab))}
  /* 功能台在移动端退化为底部抽屉，与干员台互斥 */
  #dock{left:0;right:0;top:auto;bottom:0;width:auto;height:min(64vh,74%);z-index:830;
    border-radius:22px 22px 0 0;border-bottom:0;border-left:0;
    transform:translateY(101%);opacity:1;transition:transform .44s var(--spring)}
  #dock.hide{transform:translateY(101%);opacity:1}
  #dock.up{transform:translateY(0)}
  #dock .pan-body{padding-bottom:calc(12px + var(--sab))}
  #dock-handle{display:none}
  #viewswitch{display:none}
  #roster-handle{display:none}
  #quick{display:flex}
  #playbar{left:50%;right:auto;transform:translateX(-50%);max-width:min(92vw,760px);bottom:calc(var(--sab) + 68px);gap:6px;padding:8px 11px}
  .leaflet-bottom.leaflet-right{bottom:calc(var(--sab) + 68px);right:calc(var(--sar) + 10px)}
  #map.mini{width:42vw;height:24vh;bottom:calc(var(--sab) + 70px)}
  body.mode3d #map.mini{left:calc(var(--sal) + 12px);right:auto;
    width:42vw;height:22vh;bottom:calc(var(--sab) + 70px)}
  #island{min-width:132px}
  #island.open{min-width:min(360px,94vw)}
  #acctbadge{display:none!important}
  .maps{grid-template-columns:1fr 1fr 1fr}
  /* 抽屉起来时让位：快捷条与回放轨下沉 */
  body.sheeting #quick,body.sheeting #playbar{opacity:0;pointer-events:none;transition:opacity .2s}
}
@media(max-width:420px){
  .maps{grid-template-columns:1fr 1fr}
  #toast{bottom:calc(var(--sab) + 130px)}
}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition-duration:.01ms!important}}
/* Keep replay, camera and navigation controls separate on narrow screens. */
@media(max-width:700px){
  #gateway-diagnostics{top:68px!important;bottom:auto!important;max-width:calc(100vw - 48px)!important}
  #gateway-stats{max-height:38vh;overflow:auto}
  #playbar{left:12px;right:12px;transform:none;width:auto;max-width:none;box-sizing:border-box;flex-wrap:wrap;gap:6px;bottom:calc(var(--sab) + 72px)}
  #playbar #seek{min-width:80px}
  #playbar .spd{flex-basis:100%;justify-content:center}
  body.mode3d #playbar{bottom:calc(var(--sab) + 126px)}
  body.mode3d #camseg{bottom:calc(var(--sab) + 74px);max-width:calc(100vw - 24px)}
  #camseg button{padding:6px 9px;font-size:11px;white-space:nowrap}
}
</style>
<link rel="stylesheet" href="/ui/shubao.css?v=1.1.5">
<script src="/ui/dock.js?v=1.1.2" defer></script>
</head>
<body>
<div id="map"></div>
<div id="mini-hint">Shift+拖动 移动小地图 · Shift+滚轮 缩放</div>

<!-- 3D 白模视图：与 2D 同源数据，按需加载 /m3d/r3d.js -->
<div id="r3">
  <canvas id="r3-cv"></canvas>
  <div id="r3-lbl"></div>
  <svg id="ray-layer" aria-hidden="true"></svg>
  <svg id="warn-layer" aria-hidden="true"></svg>
</div>
<div id="r3-load"><div id="r3-load-t">载入 3D 底图…</div><div id="r3-bar"><i></i></div></div>
<div id="camseg" class="glass">
  <button data-cam="orbit">自由</button>
  <button data-cam="top">俯视</button>
  <button data-cam="fpv">第一跟随</button>
  <button data-cam="chase">第三跟随</button>
</div>

<div id="hud">加载中…</div>
<div id="scrim-t"></div>
<div id="scrim-b"></div>

<!-- 战况岛：折叠态=「剩余敌人 + 最近威胁距离」，点开=完整战况 -->
<div id="island">
  <div id="isl-main">
    <span id="isl-dot"></span>
    <span class="isl-stat"><b id="isl-num" class="tnum">--</b><i>存活</i></span>
    <span class="isl-stat"><b id="isl-down" class="tnum">--</b><i>倒地</i></span>
    <span class="isl-stat"><b id="isl-dead" class="tnum">--</b><i>阵亡</i></span>
    <span id="isl-sep"></span>
    <span id="isl-near" class="tnum">最近 <b>--</b></span>
  </div>
  <div id="isl-ext">
    <div class="isl-cell"><b id="c-alive" class="tnum">-</b><span>存活</span></div>
    <div class="isl-cell"><b id="c-dead" class="tnum">-</b><span>阵亡</span></div>
    <div class="isl-cell"><b id="c-box" class="tnum">-</b><span>盒子</span></div>
    <div class="isl-cell"><b id="c-ai" class="tnum">-</b><span>AI</span></div>
    <div class="isl-cell"><b id="c-team" class="tnum">-</b><span>我方</span></div>
  </div>
  <div id="isl-foot"></div>
</div>

<!-- 顶部公告位已移除（公益提示 / 电报群入口） -->

<!-- 右舷 · 干员台：干员在上、物资在下，同窗并存（移动端=底部抽屉） -->
<div id="roster" class="glass">
  <div class="pan-head">
    <div class="pan-title">
      <span id="uname" style="color:var(--warn);font-weight:640"></span><span id="pan-name">玩家列表</span>
      <span class="sub" id="pan-sub">等待数据</span>
    </div>
    <button class="icon-btn" id="btn-roster-close" title="收起" aria-label="收起">
      <svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>
  </div>
  <div class="pan-split">
    <section class="pan-sec ops">
      <div class="sec-head">
        <span class="sec-t">干员 <i id="n-ops">0</i></span>
        <div class="sortbar" id="sortbar">
          <button class="mini-tab" data-sort="dist">距离</button>
          <button class="mini-tab on" data-sort="team">队伍</button>
          <span id="sort-hint"></span>
        </div>
      </div>
      <div class="pan-body" id="ops-body"><div class="empty">等待对局数据…</div></div>
    </section>
    <section class="pan-sec loot">
      <div class="sec-head">
        <span class="sec-t">物资 <i id="n-loot">0</i></span>
        <div class="sortbar" id="loot-sortbar">
          <button class="mini-tab" data-loot-sort="dist">距离</button>
          <button class="mini-tab" data-loot-sort="price">价值</button>
          <button class="mini-tab" data-loot-sort="grade">品质</button>
          <span id="loot-filter-hint"></span>
        </div>
      </div>
      <div class="pan-body" id="loot-body"></div>
    </section>
  </div>
</div>

<button id="roster-handle" title="收起 / 展开干员台" aria-label="收起或展开干员台">
  <svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>
</button>

<!-- 视图切换：2D / 3D 分段 + 小地图 / 跟随，悬浮在干员台左上 -->
<div id="viewswitch" class="glass">
  <div class="vs-seg" role="group" aria-label="视图模式">
    <button class="vs-ic" id="s-warn3d" title="屏幕外预警" aria-label="屏幕外预警">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M16.5 7.5h-5M16.5 7.5v5M16.5 7.5l-6.5 6.5"/></svg>
    </button>
    <button class="vs-ic" id="s-ray" title="敌人连线" aria-label="敌人连线">
      <svg viewBox="0 0 24 24"><path d="M12 3v5M12 8l-7 13M12 8l7 13"/></svg>
    </button>
    <button class="vs-ic" id="s-box3d" title="人物方框" aria-label="人物方框">
      <svg viewBox="0 0 24 24"><path d="M4 8V4h4M20 8V4h-4M4 16v4h4M20 16v4h-4"/></svg>
    </button>
    <button class="vs on" id="s-2d">2D</button>
    <button class="vs" id="s-3d">3D</button>
  </div>
  <button class="vs-ic" id="s-pip" title="3D 小地图" aria-label="3D 小地图">
    <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 20V4"/></svg>
  </button>
  <button class="vs-ic" id="s-follow" title="镜头跟随" aria-label="镜头跟随">
    <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M23 12h-4M5 12H1"/><circle cx="12" cy="12" r="8"/></svg>
  </button>
</div>

<!-- 左舷 · 功能台：设置项按板块平铺，开关即按钮 -->
<div id="dock" class="glass">
  <div class="pan-head">
    <div class="pan-title"><img src="/static/mx-logo-32.png" alt="MX" width="22" height="22" style="vertical-align:middle;margin-right:6px">MX · 功能台 <span class="sub" id="tools-sub">显示与元素</span></div>
    <button class="icon-btn" id="btn-dock-close" title="收起" aria-label="收起功能台">
      <svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>
  </div>
  <div class="pan-body">
    <button class="row row-btn" id="s-map"><div class="row-col"><div class="row-t">切换地图</div><div class="row-s">2D 底图选择</div></div><span class="row-go">›</span></button>
    <div class="sec">头顶信息</div>
    <div class="row"><div class="row-col"><div class="row-t">干员名</div><div class="row-s">显示干员 / 玩家名</div></div><button class="sw" id="s-name"></button></div>
    <div class="row"><div class="row-col"><div class="row-t">武器</div><div class="row-s">当前手持武器名</div></div><button class="sw" id="s-wpn"></button></div>
    <div class="row"><div class="row-col"><div class="row-t">头盔护甲</div><div class="row-s">头X甲Y 等级标注</div></div><button class="sw" id="s-gear"></button></div>
    <div class="row"><div class="row-col"><div class="row-t">距离</div><div class="row-s">到自己的平面距离</div></div><button class="sw" id="s-dist"></button></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">标记大小</span>
      <input id="s-dotsize" type="range" min="60" max="220" value="180"><span class="v tnum" id="v-dotsize">180%</span></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">字体大小</span>
      <input id="s-fontsize" type="range" min="40" max="220" value="100"><span class="v tnum" id="v-fontsize">100%</span></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">状态字体粗细</span>
      <input id="s-tagw" type="range" min="300" max="900" step="50" value="600"><span class="v tnum" id="v-tagw">600</span></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">信息条底色</span>
      <input id="s-tagop" type="range" min="0" max="100" value="80"><span class="v tnum" id="v-tagop">80%</span></div>

    <div class="sec">地图元素</div>
    <div class="sld"><span>墙体透明度</span><input id="s-walltrans" type="range" min="0" max="100" step="1" value="78"><span class="v tnum" id="v-walltrans">78%</span></div>
    <div class="sld"><span>地面 / 屋顶透明度</span><input id="s-floortrans" type="range" min="0" max="100" step="1" value="0"><span class="v tnum" id="v-floortrans">0%</span></div>
    <div class="row"><div class="row-col"><div class="row-t">自动切图</div>
    <div class="row-s">按自己坐标自动切到当前地图</div></div><button class="sw" id="s-automap"></button></div>
    <div class="row"><div class="row-col"><div class="row-t">朝向锥</div>
    <div class="row-s">显示角色面朝方向</div></div><button class="sw" id="s-cone"></button></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">枪线长度</span>
      <input id="s-gunlen" type="range" min="0" max="400" value="92"><span class="v tnum" id="v-gunlen">92px</span></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">第一视角位置平滑</span>
      <input id="s-fpvtau" type="range" min="0" max="300" step="10" value="80"><span class="v tnum" id="v-fpvtau">80ms</span></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">第一视角高度</span>
      <input id="s-fpvheight" type="range" min="0.6" max="2.6" step="0.1" value="1.6"><span class="v tnum" id="v-fpvheight">1.6m</span></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">视野 FOV</span>
      <input id="s-fov" type="range" min="55" max="120" step="1" value="58"><span class="v tnum" id="v-fov">58°</span></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">人物大小</span>
      <input id="s-charsize" type="range" min="50" max="200" step="5" value="100"><span class="v tnum" id="v-charsize">100%</span></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">预警距离</span>
      <input id="s-warnd" type="range" min="0" max="300" step="10" value="150"><span class="v tnum" id="v-warnd">150m</span></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">预警圆半径</span>
      <input id="s-warnr" type="range" min="60" max="420" step="10" value="200"><span class="v tnum" id="v-warnr">200px</span></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">预警箭头大小</span>
      <input id="s-warnsz" type="range" min="60" max="300" step="10" value="100"><span class="v tnum" id="v-warnsz">100%</span></div>
    <div class="sld"><span>3D 人物模型</span>
      <div class="vs-seg" id="model3dseg" role="group" aria-label="3D 人物模型">
        <button class="vs on" data-model="tactical">战术</button><button class="vs" data-model="mannequin">简洁</button><button class="vs" data-model="beacon">信标</button><button class="vs" data-model="capsule">胶囊</button></div></div>
    <div class="sld"><span>3D 朝向样式</span>
      <div class="vs-seg" id="direction3dseg" role="group" aria-label="3D 朝向样式">
        <button class="vs on" data-direction="arrow">箭头</button><button class="vs" data-direction="line">直线</button></div></div>
    <div class="sld"><span>3D 朝向位置</span>
      <div class="vs-seg" id="directionanchor3dseg" role="group" aria-label="3D 朝向位置">
        <button class="vs on" data-anchor="head">头部</button><button class="vs" data-anchor="foot">脚底</button></div></div>
    <div class="row color-setting visible"><div class="row-col"><div class="row-t">可见颜色</div><div class="row-s">人物未被建筑遮挡</div></div><label class="color-swatch"><input id="s-visiblecolor3d" type="color" value="#49d5ff" aria-label="3D 可见人物颜色"><span id="v-visiblecolor3d">#49D5FF</span></label></div>
    <div class="row color-setting occluded"><div class="row-col"><div class="row-t">掩体后颜色</div><div class="row-s">人物位于墙体或掩体后</div></div><label class="color-swatch"><input id="s-occludedcolor3d" type="color" value="#ff4058" aria-label="3D 掩体后人物颜色"><span id="v-occludedcolor3d">#FF4058</span></label></div>
    <div class="sld"><span>左右侧栏主题</span>
      <div class="vs-seg" id="panelthemeseg" role="group" aria-label="左右侧栏主题">
        <button class="vs on" data-theme="dark">暗色</button><button class="vs" data-theme="light">亮色</button></div></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">3D 画质</span>
      
      <div class="vs-seg" id="q3dseg" role="group" aria-label="3D 画质">
        <button class="vs on" data-q="auto">自动</button><button class="vs" data-q="perf">流畅</button><button class="vs" data-q="balanced">平衡</button><button class="vs" data-q="high">高清</button></div></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">帧率上限</span>
      <div class="vs-seg" id="fpscapseg" role="group" aria-label="帧率上限">
        <button class="vs on" data-f="0">无上限</button><button class="vs" data-f="60">60</button><button class="vs" data-f="120">120</button><button class="vs" data-f="144">144</button></div></div>
    <div class="row"><div class="row-col"><div class="row-t">队友</div><div class="row-s">关闭后只看敌人</div></div><button class="sw" id="s-mate"></button></div>
    <div class="row"><div class="row-col"><div class="row-t">敌人标识</div><div class="row-s">敌人头顶显示红色「敌人」</div></div><button class="sw" id="s-foe"></button></div>
    <div class="row"><div class="row-col"><div class="row-t">移动轨迹</div><div class="row-s">最近一段路径</div></div><button class="sw" id="s-trail"></button></div>
    <div class="row"><div class="row-col"><div class="row-t">血量条</div><div class="row-s">0–100 连续显示剩余血量</div></div><button class="sw" id="s-hp"></button></div>
    <div class="row"><div class="row-col"><div class="row-t">死亡盒</div><div class="row-s">阵亡玩家掉落点</div></div><button class="sw" id="s-box"></button></div>
    <div class="row"><div class="row-col"><div class="row-t">普通容器</div><div class="row-s">仅显示已识别位置，内容未知时明确标注</div></div><button class="sw" id="s-container"></button></div>
    <div class="row"><div class="row-col"><div class="row-t">人机盒子</div><div class="row-s">已识别的小兵 / 人机干员盒子</div></div><button class="sw" id="s-aibox"></button></div>
    <div class="row"><div class="row-col"><div class="row-t">AI 单位</div><div class="row-s">按出生点显示（空心标记 + 情报龄）</div></div><button class="sw" id="s-ai"></button></div>

    <div class="sec">物资</div>
    <div class="row"><div class="row-col"><div class="row-t">物资上图</div><div class="row-s">高价值物资标记到地图</div></div><button class="sw" id="s-loot"></button></div>
    <div class="pick" id="pick-loot">
      <button data-g="0">全部</button>
      <button data-g="3">蓝以上</button><button data-g="4" class="on">紫以上</button>
      <button data-g="5">金以上</button><button data-g="6">仅红</button>
    </div>

    <div class="sec">面板与预警</div>
    <div class="row"><div class="row-col"><div class="row-t">干员台</div><div class="row-s">右侧人员 / 物资清单</div></div><button class="sw" id="s-roster"></button></div>
    <div class="sld"><span style="font-size:11.5px;color:var(--txt-2);white-space:nowrap">贴脸高亮</span>
      <input id="s-alert" type="range" min="0" max="300" step="10" value="80"><span class="v tnum" id="v-alert">80m</span></div>
    <div class="row" style="padding-top:0"><div class="row-col"><div class="row-s">敌人进入该距离时标记闪烁、干员台置顶。拖到 0 关闭。3D 屏幕外预警的「贴脸红点」也用它。</div></div></div>
  </div>
</div>

<button id="dock-handle" title="收起 / 展开功能台" aria-label="收起或展开功能台">
  <svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg>
</button>

<!-- 移动端底部快捷条：干员 / 跟随 / 物资 / 地图 / 设置 -->
<div id="quick" class="glass">
  <button class="qk" id="qk-roster">
    <svg viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/></svg>
    <span>干员</span>
  </button>
  <button class="qk" id="qk-3d">
    <svg viewBox="0 0 24 24"><path d="M12 2l9 5v10l-9 5-9-5V7z"/><path d="M12 12l9-5M12 12v10M12 12L3 7"/></svg>
    <span>3D</span>
  </button>
  <button class="qk" id="qk-follow">
    <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M23 12h-4M5 12H1"/><circle cx="12" cy="12" r="8"/></svg>
    <span>跟随</span>
  </button>
  <button class="qk" id="qk-loot">
    <svg viewBox="0 0 24 24"><path d="M21 8v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8"/><rect x="1" y="3" width="22" height="5" rx="1"/></svg>
    <span>物资</span>
  </button>
  <button class="qk" id="qk-map">
    <svg viewBox="0 0 24 24"><path d="M1 6v16l7-4 8 4 7-4V2l-7 4-8-4-7 4z"/><path d="M8 2v16M16 6v16"/></svg>
    <span>地图</span>
  </button>
  <button class="qk" id="qk-set">
    <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M20 12h2M2 12h2M12 2v2M12 20v2M18.4 5.6l1.4-1.4M4.2 19.8l1.4-1.4M18.4 18.4l1.4 1.4M4.2 4.2l1.4 1.4"/></svg>
    <span>设置</span>
  </button>
</div>

<div id="veil"></div>

<!-- 地图层：卡片式选择 -->
<div id="sheet-map" class="sheet glass">
  <div class="pan-head">
    <div class="pan-title">选择地图 <span class="sub" id="map-now"></span></div>
    <button class="icon-btn" data-close="sheet-map" aria-label="关闭">
      <svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>
  </div>
  <div class="sheet-body"><div class="maps" id="maps"></div></div>
</div>

<!-- 回放轨：仅回放模式显示 -->
<div id="playbar" class="glass hide">
  <button id="btn-play" aria-label="播放/暂停"><svg id="ic-play" viewBox="0 0 24 24"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg></button>
  <input id="seek" type="range" min="0" max="1000" value="0">
  <span id="tlabel" class="tnum">--:-- / --:--</span>
  <div class="spd" id="spd">
    <button data-spd="1" class="on">1×</button><button data-spd="2">2×</button>
    <button data-spd="4">4×</button><button data-spd="16">16×</button>
  </div>
</div>

<details id="gateway-diagnostics" class="glass" style="position:fixed;left:12px;bottom:12px;z-index:950;padding:8px 12px;border-radius:12px;max-width:min(420px,90vw);font-size:12px">
  <summary id="gateway-rate" style="cursor:pointer">数据状态 · 等待解析</summary>
  <pre id="gateway-stats" style="white-space:pre-wrap;margin:8px 0 0;line-height:1.7;font-family:inherit">等待状态</pre>
</details>
<div id="toast"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
"use strict";
/* ============================================================================
   DFM 战术雷达 · 前端逻辑（0901 全量重写）
   数据契约与后端 /api/state 完全一致（liveview.snapshot / RadarApp.state）：
     self,self_yaw,self_name,self_hp,live_active,entities[],flow,meta,cursor,status
   entity: key,kind(self|mate|player|ai|box|loot),name,hero,is_bot,world,rel,yaw,
           team,slot,dead,status,status_key,hp,helmet,vest,helmet_dur,vest_dur,
           bp,cr,weapon,curr_weapon,trail,item_id,grade,price
   ========================================================================== */
const $ = (id) => document.getElementById(id);
const MOBILE = () => window.matchMedia("(max-width:900px)").matches;

/* ------------------------------------------------------------- 偏好存储 */
// 所有开关走同一套读写，避免各处散落 localStorage 键名。
const PREF_DEF = {
  // ai 默认开（0901 改）：烽火地带每局都有 PVE 人机，"看不到 AI"是用户实际
  // 反馈的问题；AI 只按出生点显示、空心标记 + 4 分钟自然过期，噪音可控，
  // 嫌吵一键即关（坞/设置层/战况岛 AI 格三处入口）。
  follow:1, trail:0, hp:0, loot:1, box:1, container:1, aibox:0, ai:0, mate:1, cone:1, gunlen:92, dotsize:180, fontsize:100, tagweight:600,
  name:1, wpn:1, gear:1, dist:1, foe:0, ray:0, tagop:80, lootmin:4, lootsort:"grade", alert:80, sort:"team",
  walltrans:78, floortrans:0, model3d:"tactical", visiblecolor3d:"#49d5ff", occludedcolor3d:"#ff4058", directionstyle3d:"arrow", directionanchor3d:"head", paneltheme:"dark", v3d:0, pip:1, cam3d:"chase", fpvtau:80, fpvheight:1.6, fov:58, charsize:100, q3d:"auto", fpscap:0,
  box3d:0,                                // 3D 人物方框（视图栏 #s-box3d，默认关）
  warn3d:0, warnd:150, warnr:200, warnsz:100,   // 3D 屏幕外预警：距离(m) / 圆半径(px) / 箭头大小(%)
  // 3D：视图 / 小地图 / 相机模式 / 第一视角平滑(ms) / 第一视角眼高(m) / 视野 FOV(度)
  mapkey:"daba",                          // 上次选择的地图(setMap 会回写,启动读回)
  automap:1,                              // 按自己坐标自动切换地图
};
const PREF = {};
for (const k in PREF_DEF) {
  const v = localStorage.getItem("nr2_" + k);
  PREF[k] = (v === null) ? PREF_DEF[k] : (isNaN(v) || v === "" ? v : Number(v));
}
function setPref(k, v) { PREF[k] = v; try { localStorage.setItem("nr2_" + k, v); } catch (e) {} }
// V5.1.5+ removes the ambiguous old "follow" entry; migrate it to third-person follow.
if (PREF.cam3d === "follow") setPref("cam3d", "chase");
// 2026-10-18: 标记大小默认由 100% 调整为 180%；仅迁移仍是旧默认值的用户
try {
  if (!localStorage.getItem("nr2_migrate_dotsize180")) {
    if (Number(PREF.dotsize) === 100 || Number(PREF.dotsize) === 150) setPref("dotsize", 180);
    localStorage.setItem("nr2_migrate_dotsize180", "1");
  }
} catch (e) {}
// 2026-10-18: AI/血条/敌人标识/轨迹改为默认关闭（一次性迁移）
try {
  if (!localStorage.getItem("nr2_migrate_defaults_off")) {
    setPref("ai", 0); setPref("hp", 0); setPref("foe", 0); setPref("trail", 0);
    localStorage.setItem("nr2_migrate_defaults_off", "1");
  }
} catch (e) {}

/* ------------------------------------------------------------- 色彩语义 */
// 队伍色板：同队同色、异队异色。self/mate/box/loot 固定语义色。
const TEAM_COLORS = ["#ff5f6d","#ff9f43","#ffd54f","#c792ea","#4dd0e1","#f48fb1","#aed581","#ff8a65"];
const C_SELF = "#37e08a", C_MATE = "#4c8dff", C_BOX = "#a97bff", C_AI = "#ff9f43", C_UNK = "#c9d1de";
const GRADE_COL = {1:"#c9d1de",2:"#6bcf7f",3:"#4c8dff",4:"#c792ea",5:"#ffb547",6:"#ff5f6d"};
// 背包名 -> 等级(gear_catalog.tier)：装备行「包N」用
const BAG_LV = {"斜挎包": 1, "运动背包": 1, "旅行背包": 1, "DG运动背包": 1, "帆布背囊": 1, "轻型户外背包": 2, "战术快拆背包": 2, "突袭战术背包": 2, "露营背包": 2, "大型登山包": 3, "3H战术背包": 3, "DASH战术背包": 3, "GA野战背包": 3, "雨林猎手背包": 3, "MAP侦察背包": 4, "野战徒步背包": 4, "D2战术登山包": 4, "GT1户外登山包": 4, "生存战术背包": 4, "蛟龙战术背包": 4, "ALS背负系统": 5, "HLS-2重型背包": 5, "D3战术登山包": 5, "GT5野战背包": 5, "不投放": 6, "重型登山包": 6, "D7战术背包": 6, "GTO重型战术包": 6};
const RIG_LV = {"便携胸包":1,"快速侦察胸挂":1,"轻型战术胸挂":1,"简易挂载包":1,"尼龙挎包":2,"HK3便携胸挂":2,"D01轻型胸挂":2,"通用战术胸挂":2,"胸挂包":2,"简易携行弹挂":3,"HD3战术胸挂":3,"DSA战术胸挂":3,"G01战术弹挂":3,"突袭胸挂":3,"强袭战术背心":4,"突击者战术背心":4,"DRC先进侦察胸挂":4,"GIR野战胸挂":4,"蛟龙战术胸挂":4,"飓风战术胸挂":5,"指挥官战术背心":5,"黑鹰野战胸挂":5,"DAR突击手胸挂":5,"通用胸挂":5,"指挥官背心":6,"非法道具 看到报bug":6,"G.T.I 5级胸挂":6};
// 背包名 -> 容量(格)：装备行「包N · M格」用
const BAG_CAP = {"斜挎包": 6, "运动背包": 8, "旅行背包": 10, "DG运动背包": 12, "帆布背囊": 10, "轻型户外背包": 12, "战术快拆背包": 15, "突袭战术背包": 15, "露营背包": 15, "大型登山包": 16, "3H战术背包": 18, "DASH战术背包": 20, "GA野战背包": 20, "雨林猎手背包": 21, "MAP侦察背包": 24, "野战徒步背包": 24, "D2战术登山包": 24, "GT1户外登山包": 25, "生存战术背包": 28, "ALS背负系统": 28, "HLS-2重型背包": 28, "D3战术登山包": 28, "GT5野战背包": 30, "重型登山包": 40, "D7战术背包": 35, "GTO重型战术包": 45};
const GRADE_NM  = {1:"1级",2:"2级",3:"3级",4:"4级",5:"5级",6:"6级"};
const STATE_COL = {down:"#ffcf4d", dying:"#ffcf4d", dead:"#a97bff", revive:"#37e08a", box_carried:"#ffcf4d", box_searching:"#ffcf4d", box_empty:"#88939e", box_looted:"#a97bff"};
const STATE_TXT = {down:"倒地", dying:"倒地", dead:"阵亡", revive:"救援", box_carried:"搬运中", box_searching:"搜刮中", box_empty:"已搜空", box_looted:"已搜刮"};
const KIND_NM   = {player:"玩家", ai:"AI", self:"自己", box:"死亡盒", container:"容器", loot:"物资", mate:"队友", unknown:"未知"};
const LOOT_IMG  = (id) => "/resources/items/" + encodeURIComponent(id) + ".png";
// 干员头像：换图后把版本号 +1，绕开浏览器长缓存
const AVATAR_V = "3";
const avatarUrl = (hero) => "/avatars/" + encodeURIComponent(hero) + ".png?v=" + AVATAR_V;

// 死亡盒配色：玩家盒金色，人机盒保持原紫色
const boxColor = (e) => (e.is_ai === true || e.is_bot === true) ? C_BOX : "#ffb547";
// 贴脸预警的滞回状态（key -> bool）：避免距离在阈值边缘抖动时每帧翻转
const alertHold = new Map();
function entColor(e) {
  // 自己绿、队友蓝；敌队按队号取色板（一队一色）
  if (e.kind === "self") return C_SELF;
  if (e.kind === "mate") return C_MATE;
  if (e.kind === "container") return "#38bdf8";
  if (e.kind === "box")  return boxColor(e);
  if (e.kind === "loot") return GRADE_COL[e.grade] || "#ffb547";
  if (e.kind === "ai")   return C_AI;
  if (e.team > 0) return TEAM_COLORS[(e.team - 1) % TEAM_COLORS.length];
  return C_UNK;
}
// 干员台头像里的两字缩写：优先干员名，其次玩家名
function initials(s) {
  s = String(s || "").trim();
  if (!s) return "?";
  const cn = s.match(/[\u4e00-\u9fa5]/g);
  if (cn && cn.length) return cn.slice(0, 2).join("");
  return s.slice(0, 2).toUpperCase();
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function fmtT(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  return String(Math.floor(sec / 60)).padStart(2, "0") + ":" + String(sec % 60).padStart(2, "0");
}
// 情报龄：出生点标记等「非实时」坐标必须带龄，否则会被当成实时位置
function agoTxt(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  return sec < 60 ? sec + "s前" : Math.floor(sec / 60) + "m前";
}
const UPDATE_RANGE_M = 300;
const RANGE_ENTITY_KINDS = new Set(["player", "ai", "mate", "unknown"]);
const DMAX_M = 5000;   // 超过任何地图尺度的距离判无效（自机坐标未解出时的护栏）
function dist2(e) {
  if (!e.rel) return null;
  const x = e.rel[0], y = e.rel[1];
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const d = Math.hypot(x, y) / 100;
  return d <= DMAX_M ? d : null;
}
function isOutOfUpdateRange(e) {
  // Keep the last-known point visible, but make it visually distinct from a
  // live point. Death markers and AI spawn markers already carry their own
  // historical-position semantics and should not get a second warning.
  return RANGE_ENTITY_KINDS.has(e.kind) && !e.dead && !e.spawn_mark
    && e.out_of_range === true;
}
function dz(e) {
  if (!e.rel) return null;
  const z = e.rel[2];
  if (!Number.isFinite(z)) return null;
  const v = z / 100;
  return Math.abs(v) <= DMAX_M ? v : null;
}
// 「（人机干员）」后缀只标伪真人 bot，真 AI 不带
// UE 本地化包装：INVTEXT("名字","1") -> 名字
function cleanUeName(s) {
  if (s == null) return s;
  const v = String(s);
  const m = v.match(/^INVTEXT\(\s*(?:"([^"]*)"|'([^']*)')/);
  return m ? (m[1] !== undefined ? m[1] : m[2]) : v;
}
function cleanWeapon(w) {
  if (w == null) return w;
  const v = cleanUeName(String(w)).trim();
  return /^武器\s*\d+$/.test(v) ? "未知武器" : v;
}
function nameOf(e) {
  if (e.kind === "loot") return cleanUeName(lootName(e));
  const n = cleanUeName(e.hero || e.name || KIND_NM[e.kind] || "未知");
  return (e.is_bot && (e.hero || e.name)) ? n + "（人机）" : n;
}

let _toastT = 0;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg; t.classList.add("on");
  clearTimeout(_toastT); _toastT = setTimeout(() => t.classList.remove("on"), 1700);
}

/* ------------------------------------------------------------ 地图与投影 */
let MAP_INFO = null, MAPS = {}, DEF_MAP = "daba", map = null, tileLayer = null;
// 世界坐标 -> 平面 (lng,lat)，与官方 main.js getMapPos 一致（含 rotate）
function worldToPlane(wx, wy) {
  const i = MAP_INFO, bj = i.bj || 128, xB = i.width / bj, yB = i.height / bj;
  const cx = i.centerX, cy = i.centerY, r = Number(i.rotate || 0);
  if (r === 90)  return { lng: bj - (cy + wy) / yB, lat: -bj + (cx - wx) / xB };
  if (r === -90) return { lng: bj + (cy + wy) / yB, lat: -bj - (cx - wx) / xB };
  return { lng: bj - (cx - wx) / xB, lat: -bj - (cy + wy) / yB };
}
function pointLatLng(w) { const p = worldToPlane(w[0], w[1]); return L.latLng(p.lat, p.lng); }
// 世界 yaw -> 屏幕旋转角（0=向上，顺时针为正）。
// 直接投影 p 与 p+dir 两点求平面增量，天然吃掉各地图的 rotate 差异。
// dy 必须是 +Δlat：CRS.Simple 的 lat 向上，而 worldToPlane 里 +wy 映到
// −lat（r=0 支），两个负号相互抵消——写负号会把南北朝向镜像（0902 实修，
// 73976 局 13193 对位移-方位角实证 yaw 前向=(cos,+sin)，中位误差 14.4°）。
function yawToScreen(w, yaw) {
  if (yaw == null) return null;
  const r = yaw * Math.PI / 180, L0 = 1500;
  const a = worldToPlane(w[0], w[1]);
  const b = worldToPlane(w[0] + Math.cos(r) * L0, w[1] + Math.sin(r) * L0);
  const dx = b.lng - a.lng, dy = b.lat - a.lat;
  if (!dx && !dy) return null;
  return Math.atan2(dx, dy) * 180 / Math.PI;
}

// 用自己世界坐标反推每张图的平面坐标，落在哪张图 bounds 里就是哪张图
function detectMapKey(self) {
  if (!self || !Array.isArray(self) || self.length < 2) return null;
  const wx = Number(self[0]), wy = Number(self[1]);
  if (!isFinite(wx) || !isFinite(wy)) return null;
  for (const key in MAPS) {
    const m = MAPS[key];
    if (!m || !m.bounds || !m.width || !m.height) continue;
    const bj = m.bj || 128, xB = m.width / bj, yB = m.height / bj;
    if (!xB || !yB) continue;
    const cx = m.centerX, cy = m.centerY, r = Number(m.rotate || 0);
    let lng, lat;
    if (r === 90) { lng = bj - (cy + wy) / yB; lat = -bj + (cx - wx) / xB; }
    else if (r === -90) { lng = bj + (cy + wy) / yB; lat = -bj - (cx - wx) / xB; }
    else { lng = bj - (cx - wx) / xB; lat = -bj - (cy + wy) / yB; }
    const a = m.bounds[0], b = m.bounds[1];
    const minLat = Math.min(a[0], b[0]), maxLat = Math.max(a[0], b[0]);
    const minLng = Math.min(a[1], b[1]), maxLng = Math.max(a[1], b[1]);
    const pad = 0.02 * Math.max(maxLat - minLat, maxLng - minLng);
    if (lat >= minLat - pad && lat <= maxLat + pad && lng >= minLng - pad && lng <= maxLng + pad) return key;
  }
  return null;
}
let _autoMapCandidate = "", _autoMapAt = 0;
function autoSwitchMap(self, status) {
  if (!PREF.automap || status !== "live" || !self || !map) return;
  const key = detectMapKey(self);
  if (!key || key === PREF.mapkey || !MAPS[key]) { _autoMapCandidate = ""; return; }
  if (_autoMapCandidate !== key) { _autoMapCandidate = key; return; }  // 连续两帧命中才切
  const now = performance.now();
  if (now - _autoMapAt < 3000) return;
  _autoMapAt = now; _autoMapCandidate = "";
  setMap(key);
  toast("自动切换地图：" + (MAPS[key].name || key));
}

/* ---------------------------------------------------------- 渲染缓存/状态 */
let markers = new Map(), trails = new Map(), tags = new Map(), hpbars = new Map(), gearbars = new Map(), distbars = new Map(), foebars = new Map();
let selfMarker = null, pickMarker = null;
let follow = !!PREF.follow, seeking = false, followKey = "__self";
let sel = null;           // 干员台选中项 key（地图上闪烁 + 视图定位）
let lastSnap = null;
const lootRows = new Map();
const opRows = new Map();   // 干员台行复用池（key -> {node, html}），避免整块重建打断 hover
const foldGrp = new Set();
// 「值没变就不碰 DOM」缓存：draw 是 100ms 级热路径
const _rc = { pos:new Map(), tip:new Map(), tag:new Map(), icon:new Map(), iconBase:new Map(),
              hp:new Map(), trail:new Map(), roster:"", isl:"", head:"" };

/* ------------------------------------------- 位置平滑（指数趋近 + rAF） */
// 把低频坐标连成连续移动；τ=120ms。传送阈值按世界厘米计算，不能把
// CRS.Simple 的投影单位当经纬度：旧 0.002 在大坝仅约 1.3cm，走路也会吸附。
const smPos = new Map(), smTgt = new Map();
const SM_TAU = 0.12, SM_SNAP_CM = 3000;

/* ------------------------------------------------ 自机位置跳变过滤 */
// 后端在数据变稀时偶尔会把自机位置判到别人（实测：队友）身上。直接画出去会让 2D
// 标记"飘走"、跟随/第一视角的 3D 相机跟着飞（黑屏 + 抽搐）。这里做一次合理性检查：
// 单次跳变超过 SELF_JUMP_CM 先按住旧位置，等新位置连续 SELF_HOLD_N 次都报上来才
// 接受 —— 真实复活/换点最多晚约 0.3 秒显示。
// 20m 的依据：走路约 0.5m/帧、轮询回退时最多几米，单帧 20m 只可能是误判或复活。
const SELF_JUMP_CM = 2000;    // 20m
const SELF_HOLD_N = 6;
let selfHold = null;
function normalizeSelf(raw) {
  if (!raw || raw.length < 3) { selfHold = null; return raw; }
  const x = Number(raw[0]), y = Number(raw[1]);
  if (!isFinite(x) || !isFinite(y)) return raw;      // 交给后端的世界坐标判据兜底
  if (!selfHold) { selfHold = { p: raw.slice(), n: 1 }; return raw; }
  const d = Math.hypot(x - selfHold.p[0], y - selfHold.p[1]);
  if (d <= SELF_JUMP_CM) { selfHold = { p: raw.slice(), n: 1 }; return raw; }
  selfHold.n += 1;
  if (selfHold.n >= SELF_HOLD_N) { selfHold = { p: raw.slice(), n: 1 }; return raw; }
  return selfHold.p.slice();                          // 按住旧位置，等它被连续确认
}
let smRaf = 0, smLast = 0, smSelfW = null;
function smDistanceCm(p, t) {
  const info = MAP_INFO;
  if (!info || !(info.width > 0) || !(info.height > 0)) return Infinity;
  const x = info.width / (info.bj || 128), y = info.height / (info.bj || 128);
  const rotate = Number(info.rotate || 0), lat = t.lat - p.lat, lng = t.lng - p.lng;
  return rotate === 90 || rotate === -90 ? Math.hypot(lat * x, lng * y) : Math.hypot(lng * x, lat * y);
}
function smStep() {
  smRaf = 0;
  const now = performance.now();
  const dt = Math.min((now - smLast) / 1000, 0.25) || 0.016; smLast = now;
  const a = 1 - Math.exp(-dt / SM_TAU);
  let active = false;
  for (const [k, t] of smTgt) {
    const m = markers.get(k) || (k === "__self" ? selfMarker : null);
    if (!m) { smPos.delete(k); smTgt.delete(k); continue; }
    let p = smPos.get(k);
    if (!p) { p = { lat: t.lat, lng: t.lng }; smPos.set(k, p); }
    let chg = false;
    const dl = Math.abs(t.lat - p.lat), dg = Math.abs(t.lng - p.lng);
    if (smDistanceCm(p, t) > SM_SNAP_CM) { p.lat = t.lat; p.lng = t.lng; chg = true; }
    else if (dl > 1e-9 || dg > 1e-9) { p.lat += (t.lat - p.lat) * a; p.lng += (t.lng - p.lng) * a; chg = true; active = true; }
    if (chg) {
      m.setLatLng([p.lat, p.lng]);
      const g = tags.get(k); if (g) g.setLatLng([p.lat, p.lng]);
      const h = hpbars.get(k); if (h) h.setLatLng([p.lat, p.lng]);
      const gb = gearbars.get(k); if (gb) gb.setLatLng([p.lat, p.lng]);
      const db = distbars.get(k); if (db) db.setLatLng([p.lat, p.lng]);
      const fb = foebars.get(k); if (fb) fb.setLatLng([p.lat, p.lng]);
    }
    if (follow && k === followKey) {
      const center = map.latLngToLayerPoint(map.getCenter());
      const target = map.latLngToLayerPoint([p.lat, p.lng]);
      // 阈值放宽：跟随移动目标时避免每帧微调造成小地图抖动
      if (Math.hypot(target.x - center.x, target.y - center.y) >= 3)
        map.panTo([p.lat, p.lng], { animate: false });
    }
  }
  if (active) smRaf = requestAnimationFrame(smStep);
}
function smKick() { if (!smRaf) { smLast = performance.now(); smRaf = requestAnimationFrame(smStep); } }
function smSet(key, ll) {
  smTgt.set(key, { lat: ll.lat, lng: ll.lng });
  if (!smPos.has(key)) smPos.set(key, { lat: ll.lat, lng: ll.lng });
  smKick();
}

/* -------------------------------------------------------------- 血量 */
// hp.total = [真实 Health, 已知 MaxHealth 或 null]。部位属性独立保留，
// 不能相加充当总血量；上限缺失时显示当前血量数值，比例保持未知。
const HP_ORDER = ["head","thorax","abdomen","larm","rarm","lleg","rleg"];
const HP_MAX = { head:40, thorax:50, abdomen:30, larm:30, rarm:30, lleg:40, rleg:30 };
const HP_NM  = { head:"头", thorax:"胸", abdomen:"腹", larm:"左臂", rarm:"右臂", lleg:"左腿", rleg:"右腿" };
function hpCol(f) { return !Number.isFinite(f) ? "#88939e" : f <= 0 ? "#8d2233" : f > .66 ? "#37e08a" : f > .33 ? "#ffcf4d" : "#ff5f6d"; }
function hpPart(hp, k) {
  const rec = hp ? hp[k] : null;
  const mx = (rec && Number.isFinite(rec[1]) && rec[1] >= 5 && rec[1] <= 500)
    ? rec[1] : HP_MAX[k];
  const cur = (rec && Number.isFinite(rec[0]) && rec[0] >= 0 && rec[0] <= mx)
    ? rec[0] : null;
  return { known: cur != null, mx: mx, cur: cur,
           f: cur == null ? null : cur / mx };
}
function hpStat(hp) {
  let worst = null;
  for (const k of HP_ORDER) {
    const p = hpPart(hp, k);
    if (p.known && (!worst || p.f < worst.f)) { worst = p; worst.k = k; }
  }
  const total = hp && hp.total;
  const cur = total && Number.isFinite(total[0]) && total[0] >= 0 ? total[0] : null;
  const mx = total && Number.isFinite(total[1]) && total[1] > 0 && total[1] >= cur ? total[1] : null;
  const complete = cur != null && (mx != null || cur === 0);
  const frac = cur === 0 ? 0 : complete ? Math.max(0, Math.min(1, cur / mx)) : null;
  return { frac, upper: complete ? frac : 1, complete,
           known: cur != null ? 1 : 0, cur, mx, worst };
}
function hpHurt(hp) {
  if (!hp) return false;
  const total = hpStat(hp);
  if (total.complete && total.cur < total.mx) return true;
  for (const k of HP_ORDER) { const p = hpPart(hp, k); if (p.known && p.f < 1) return true; }
  return false;
}
function hpBarHtml(hp, cls) {
  const st = hpStat(hp);
  const pct = st.frac * 100;
  const value = st.complete ? ' aria-valuenow="' + Math.round(pct) + '"' : "";
  const uncertain = st.complete ? "" : '<span style="display:block;height:100%;width:'
    + ((st.upper - st.frac) * 100).toFixed(1)
    + '%;background:repeating-linear-gradient(135deg,#66717a 0 2px,#333b43 2px 4px)"></span>';
  return '<div class="' + cls + '" role="meter" aria-label="剩余血量" aria-valuemin="0"'
    + ' aria-valuemax="100"' + value + ' aria-valuetext="' + hpPct(hp)
    + '" title="' + hpPct(hp) + '" style="display:flex"><i style="flex:none;width:'
    + pct.toFixed(1) + "%;background:" + hpCol(st.frac) + '"></i>' + uncertain + '</div>';
}
function hpPct(hp) {
  const st = hpStat(hp);
  if (!st.known) return "未知";
  const cur = Number(st.cur.toFixed(1));
  return st.mx != null ? cur + "/" + Number(st.mx.toFixed(1)) : String(cur);
}
function gearValue(durability, level) {
  return (Array.isArray(durability) && durability[0] != null) ? String(Math.round(durability[0])) : String(level || "?");
}
function gearIconHtml(kind, level, value) {
  const color = GRADE_COL[Number(level)] || "#c9d1de";
  const labels = {helmet:"头盔", armor:"护甲", bag:"背包", rig:"胸挂"};
  return '<span class="gear-chip" title="' + (labels[kind] || "装备") + ' ' + level
    + '级" style="--gear:' + color + '"><i class="gear-icon ' + kind + '"></i><b>' + value + '</b></span>';
}
function updHp(key, ll, hp, outOfRange) {
  const want = PREF.hp && hp != null;
  let hb = hpbars.get(key);
  if (!want) { if (hb) { hb.remove(); hpbars.delete(key); _rc.hp.delete(key); } return; }
  const html = hpBarHtml(hp, "hpb" + (outOfRange ? " out-range" : ""));
  const mk = () => L.divIcon({ className: "", html: html, iconSize: [64, 5], iconAnchor: [32, -38] });
  if (!hb) {
    hb = L.marker(ll, { icon: mk(), keyboard: false, interactive: false, zIndexOffset: 900 });
    hb.addTo(map); hpbars.set(key, hb); _rc.hp.set(key, html);
  } else if (_rc.hp.get(key) !== html) { hb.setIcon(mk()); _rc.hp.set(key, html); }
}
/* 标记下方装备行：头甲耐久 + 背包/胸挂等级，图标颜色统一表示等级。 */
function gearBarHtml(e) {
  // 只给敌方真人/AI 显示；队友不显示头甲，没戴也画灰色 0 级
  if (e.kind !== "player" && e.kind !== "ai") return "";
  const parts = [
    gearIconHtml("helmet", e.helmet || 0, e.helmet || 0),
    gearIconHtml("armor", e.vest || 0, e.vest || 0),
  ];
  return '<div class="grb' + (e._out_of_range ? " out-range" : "") + '">' + parts.join("") + "</div>";
}
function updGear(key, ll, html, ay) {
  const want = PREF.gear && html;
  let gb = gearbars.get(key);
  if (!want) { if (gb) { gb.remove(); gearbars.delete(key); } return; }
  const mk = () => L.divIcon({ className: "", html: html, iconSize: [156, 18], iconAnchor: [78, ay] });
  if (!gb) {
    gb = L.marker(ll, { icon: mk(), keyboard: false, interactive: false, zIndexOffset: 880 });
    gb.addTo(map); gearbars.set(key, gb); gb._h = html; gb._a = ay;
  } else if (gb._h !== html || gb._a !== ay) { gb.setIcon(mk()); gb._h = html; gb._a = ay; }
}
/* 距离/高差行：挂在标记下方，独立于信息条。 */
function distBarHtml(e, col) {
  if (!PREF.dist || e.kind === "self" || e.kind === "mate") return "";
  const d = dist2(e);
  if (d == null) return "";
  const z = dz(e);
  let s = d.toFixed(0) + "m";
  if (z != null && Math.abs(z) >= 1)
    s += ' <span class="dz">' + (z > 0 ? "↑" : "↓") + "</span>" + Math.abs(z).toFixed(0);
  return '<div class="dst' + (e._out_of_range ? " out-range" : "") + '" style="--c:' + col + '">' + s + "</div>";
}
function updDist(key, ll, html, ay) {
  const want = PREF.dist && html;
  let db = distbars.get(key);
  if (!want) { if (db) { db.remove(); distbars.delete(key); } return; }
  const mk = () => L.divIcon({ className: "", html: html, iconSize: [120, 22], iconAnchor: [60, ay] });
  if (!db) {
    db = L.marker(ll, { icon: mk(), keyboard: false, interactive: false, zIndexOffset: 870 });
    db.addTo(map); distbars.set(key, db); db._h = html; db._a = ay;
  } else if (db._h !== html || db._a !== ay) { db.setIcon(mk()); db._h = html; db._a = ay; }
}
/* 敌人标识：仅敌队玩家（team>0）显示，红底"敌人"，压在信息条正上方。
   跟随「干员名」显示开关（PREF.name）；纵向位置随信息条高度与字体设置联动。 */
function updFoe(key, ll, on, tagAnchorY, down) {
  const want = on && PREF.foe;
  let fb = foebars.get(key);
  if (!want) { if (fb) { fb.remove(); foebars.delete(key); } return; }
  const fs = (Number(PREF.fontsize) || 100) / 100;
  const h = Math.round(14 * fs);
  // 锚点 = 信息条顶边距 + 标识高度：标识底边正好贴住信息条顶边，不留缝
  const ay = Math.round(tagAnchorY + h);
  // 倒地（down/dying）→ 橙色「倒地」；否则红色「敌人」
  const d = !!down;
  const mk = () => L.divIcon({
    className: "",
    html: '<div class="foe' + (d ? " down" : "") + '">' + (d ? "倒地" : "敌人") + "</div>",
    iconSize: [44, h], iconAnchor: [22, ay] });
  if (!fb) {
    fb = L.marker(ll, { icon: mk(), keyboard: false, interactive: false, zIndexOffset: 1010 });
    fb.addTo(map); foebars.set(key, fb); fb._a = ay; fb._d = d;
  } else if (fb._a !== ay || fb._d !== d) { fb.setIcon(mk()); fb._a = ay; fb._d = d; }
}
/* ---------------------------------------------------------- 地图标记构建 */
// 单个干员标记 = 朝向锥 + 核心点 + 状态环，全部由 CSS 变量驱动，
// 只有 html 指纹变化才 setIcon（避免每 tick 重建 DOM）。
function mkIcon(e, col, rot) {
  const st = e.status_key && STATE_COL[e.status_key] ? STATE_COL[e.status_key] : null;
  const cls = ["mk"];
  if (e.kind === "self") cls.push("self");
  if (st) cls.push("st");
  if (e.dead) cls.push("dead");
  if (e._out_of_range) cls.push("out-range");
  // 出生点标记：非实时坐标，画成空心，和实时点在视觉上必须能区分
  if (e.spawn_mark) cls.push("spawn");
  if (sel === e.key) cls.push("mark");
  if (e._alert) cls.push("mark");
  let style = "--c:" + col;
  if (st) style += ";--s:" + st;
  if (rot != null) style += ";--rot:" + rot.toFixed(1) + "deg";
  const aim = PREF.cone && rot != null && !e.dead;
  const gun = aim ? '<i class="mk-gun"></i>' : "";
  const cone = aim ? '<svg class="mk-cone" viewBox="0 0 12 14" aria-hidden="true"><path d="M6 .5 L12 13 L11.590 13 A7.5 7.5 0 0 0 0.410 13 L0 13 Z"/></svg>' : "";
  // 核心圆点始终渲染，头像加载成功时覆盖在圆点上；加载失败仍保留圆点
  const core = '<i class="mk-core"></i>';
  const face = e.hero
    ? '<img class="mk-face" alt="" src="' + avatarUrl(e.hero) + '" onerror="this.remove()">'
    : '';
  const team = (e.team > 0 && e.kind !== "mate" && e.kind !== "self")
    ? '<span class="mk-team">' + e.team + '</span>' : '';
  return '<div class="' + cls.join(" ") + '" style="' + style + '">' + gun + cone + core + face + team + '</div>';
}
function divIcon(html, size, anchor) {
  return L.divIcon({ className: "", html: html, iconSize: size, iconAnchor: anchor });
}
// 头顶信息条：一行「状态 · T队号 · 头甲 · 武器 · 干员 · 距离」，按设置裁剪
function tagHtml(e, d) {
  const parts = [];
  const col = entColor(e);          // 整条文字用队伍色
  const st = e.status_key ? STATE_TXT[e.status_key] : null;
  if (st) parts.push("<b>" + st + "</b>");
  if (e._out_of_range) parts.push('<span class="far">超距·最后位置</span>');
  // 出生点：位置来自出生事件而非实时轨迹，条上直说 + 带龄
  if (e.spawn_mark) parts.push('<span class="sp">出生点'
    + (e.age_sec != null ? " " + agoTxt(e.age_sec) : "") + "</span>");
  if (PREF.name && e.hero) parts.push('<span class="hero">[' + esc(cleanUeName(e.hero)) + ']</span>');
  {
    const n = e.name ? (e.is_bot && !e.hero ? e.name + "（人机）" : e.name) : "";
    const w = cleanWeapon(e.curr_weapon_known ? e.curr_weapon : (e.curr_weapon || e.weapon)) || "";
    let s = PREF.name ? esc(cleanUeName(n)) : "";
    if (PREF.name && !n && !e.hero) s = "未知目标";
    if (PREF.wpn && w) s += '<span class="wpn">[' + esc(w) + ']</span>';
    if (s) parts.push(s);
  }
  if (!parts.length) return "";
  return '<div class="tb-wrap"><div class="tag' + (e._out_of_range ? " out-range" : "")
    + '" style="--tc:' + col + '">' + parts.join("") + "</div></div>";
}
// 信息条真实宽度：离屏容器渲染一次实测，按 HTML 指纹缓存（同样文本只量一次）。
// 估算字宽在中英混排 + 分段 gap 下必然偏小，会导致 Leaflet 固定框裁字。
const _tagW = new Map();
let _ruler = null;
function measureTag(html) {
  const hit = _tagW.get(html);
  if (hit) return hit;
  if (!_ruler) {
    _ruler = document.createElement("div");
    _ruler.style.cssText = "position:absolute;left:-9999px;top:-9999px;visibility:hidden;white-space:nowrap;pointer-events:none";
    document.body.appendChild(_ruler);
  }
  _ruler.innerHTML = html;
  const el = _ruler.querySelector(".tag");
  const w = Math.min(460, Math.max(56, Math.ceil((el ? el.getBoundingClientRect().width : 120) + 6)));
  if (_tagW.size > 600) _tagW.clear();
  _tagW.set(html, w);
  return w;
}

// Tooltip：点击/悬停才看的完整档案，信息量可以大
function tipHtml(e, d) {
  const col = entColor(e);
  const h = ['<div class="tip-h"><span style="width:8px;height:8px;border-radius:50%;background:'
    + col + ';display:inline-block"></span>' + esc(nameOf(e))
    + (e.team > 0 ? '<span style="color:var(--txt-3);font-weight:400">T' + e.team + "</span>" : "") + "</div>"];
  if (e.kind === "container") {
    h.push('<div class="tip-r">搜索容器 · 内容未知</div>');
    if (e.class_path) h.push('<div class="tip-r">' + esc(e.class_path.split('.').pop()) + '</div>');
  } else if (e.kind === "box") {
    h.push('<div class="tip-r">物资 <b>' + (e.empty === true ? '已搜空' : e.empty === false ? '未空' : '未知') + '</b></div>');
    if (e.searching_count || e.looting_count) h.push('<div class="tip-r">正在交互 <b>' + Math.max(e.searching_count || 0, e.looting_count || 0) + ' 人</b></div>');
    h.push(boxContentsHtml(e));
  }
  if (e.kind === "loot") {
    const value = lootValue(e), count = lootCount(e);
    h.push('<div class="tip-r">品质 <b style="color:' + (GRADE_COL[e.grade] || "#fff") + '">'
      + (GRADE_NM[e.grade] || "未知") + "</b> · 数量 <b>" + (count == null ? "未知" : count) + "</b></div>");
    if (value.total != null) h.push('<div class="tip-r">参考总价 <b>' + lootMoney(value.total) + '</b></div>');
    if (value.unit != null) h.push('<div class="tip-r">单件参考价 <b>' + lootMoney(value.unit) + '</b></div>');
    if (Number.isFinite(e.price_per_slot) && e.price_per_slot >= 0)
      h.push('<div class="tip-r">单格参考价 <b>' + lootMoney(e.price_per_slot) + '</b></div>');
    if (!lootItemId(e)) h.push('<div class="tip-r">尚未识别物品 ID</div>');
    else if (lootName(e) === "未知物资") h.push('<div class="tip-r">名称表未收录 · ID ' + esc(lootItemId(e)) + '</div>');
    if (value.unit == null) h.push('<div class="tip-r">参考价未知</div>');
    if (e.source_type === 2) h.push('<div class="tip-r">来源 <b>丢弃物</b></div>');
    if (e.pickup_status === "candidate")
      h.push('<div class="tip-r" style="color:var(--warn)">疑似拾取 · 有人曾在此停留，尚未确认</div>');
    if (e.channel_state === "irrelevant") h.push('<div class="tip-r">最后已知位置</div>');
  } else {
    if (e._out_of_range) h.push('<div class="tip-r" style="color:var(--warn)">超出更新范围 · 最后已知位置</div>');
    if (e.status) h.push('<div class="tip-r">状态 <b style="color:'
      + (STATE_COL[e.status_key] || "#fff") + '">' + esc(e.status) + "</b></div>");
    const g = [];
    if (e.helmet) g.push("头" + e.helmet + durTxt(e.helmet_dur));
    if (e.vest) g.push("甲" + e.vest + durTxt(e.vest_dur));
    const w = cleanWeapon(e.curr_weapon_known ? e.curr_weapon : (e.curr_weapon || e.weapon));
    if (w) g.push(esc(w));
    if (g.length) h.push('<div class="tip-r">装备 <b>' + g.join(" · ") + "</b></div>");
    // 血量：总量 + 最惨部位 + 情报完整度。部分同步时显示已知范围，
    // 不把缺失部位伪装成满血。
    if (!e.dead && e.hp != null) {
      const st = hpStat(e.hp);
      if (st.known) {
        h.push('<div class="tip-r">血量 <b style="color:' + hpCol(st.frac) + '">' + hpPct(e.hp) + "</b>"
          + (st.worst && st.worst.f < 1
             ? ' <span style="color:var(--txt-3)">最低 ' + HP_NM[st.worst.k] + " "
               + Math.round(st.worst.cur) + "/" + Math.round(st.worst.mx) + "</span>" : "")
          + "</div>");
        h.push(hpBarHtml(e.hp, "op-hp"));
        if (!st.complete)
          h.push('<div class="tip-r" style="font-size:10.5px;opacity:.66">血量上限未知</div>');
      }
    }
    if (e.spawn_mark) h.push('<div class="tip-r" style="color:var(--warn)">出生点情报 <b>'
      + (e.age_sec != null ? agoTxt(e.age_sec) : "—") + "</b>（非实时位置）</div>");
  }
  if (d != null) h.push('<div class="tip-r tnum">距离 <b>' + d.toFixed(0) + "m</b>"
    + (dz(e) != null ? " · 高差 <b>" + (dz(e) > 0 ? "+" : "") + dz(e).toFixed(0) + "m</b>" : "") + "</div>");
  h.push('<div class="tip-r tnum" style="font-size:10.5px;opacity:.72">x' + Math.round(e.world[0])
    + " y" + Math.round(e.world[1]) + " z" + Math.round(e.world[2])
    + (e.yaw != null ? " · " + Math.round(e.yaw) + "°" : "") + "</div>");
  return h.join("");
}
function durTxt(v) {
  if (!Array.isArray(v) || v[0] == null) return "";
  return " " + Math.round(v[0]) + "/" + (v[1] != null ? Math.round(v[1]) : "?");
}

function boxContentsHtml(e) {
  const items = Array.isArray(e.contents) ? e.contents : [];
  if (!e.contents_known && !items.length) return '<div class="tip-r">物品清单 <b>未同步</b></div>'
    + (e.looted ? '<div class="tip-r">已搜刮，但尚未收到该盒子的物品明细</div>' : '');
  const heading = e.contents_known ? '最近同步清单' : '上次清单（待更新）';
  const unknown = items.filter(item => !item.revealed).length;
  const lines = ['<div class="tip-r">' + heading + ' <b>' + items.length + ' 条'
    + (unknown ? ' · ' + unknown + ' 条未搜索' : '') + '</b></div>'];
  lines.push('<div style="max-height:220px;overflow:auto">');
  for (const item of items) {
    const count = Number.isSafeInteger(item.count) && item.count >= 0 ? item.count : '?';
    lines.push('<div class="tip-r" style="gap:12px"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
      + esc(item.name || '未识别物品') + '</span><b class="tnum">×' + count + '</b></div>');
  }
  lines.push('</div>');
  return lines.join('');
}

/* -------------------------------------------------------- Loot display rules */
function lootCount(e) {
  return Number.isSafeInteger(e.stack_count) && e.stack_count >= 0 ? e.stack_count : null;
}
function lootName(e) {
  return typeof e.name === "string" && e.name.trim() && e.name !== "物资" ? e.name : "未知物资";
}
function lootItemId(e) {
  const id = String(e.item_id == null ? "" : e.item_id);
  return /^[1-9][0-9]{0,19}$/.test(id) ? id : null;
}
function lootValue(e) {
  const unit = Number.isFinite(e.price) && e.price >= 0 ? e.price : null, count = lootCount(e);
  const product = unit != null && count != null ? unit * count : null;
  return { unit, total: product != null && Number.isFinite(product) && product <= Number.MAX_SAFE_INTEGER ? product : null };
}
const LOOT_MONEY_FORMAT = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });
function lootMoney(value) { return LOOT_MONEY_FORMAT.format(value); }
function lootMinGrade() {
  const g = Number(PREF.lootmin);
  return Number.isInteger(g) && g >= 0 && g <= 6 ? g : 4;
}
function lootFilterName() {
  const g = lootMinGrade();
  return ({0:"全部品质",3:"蓝以上",4:"紫以上",5:"金以上",6:"仅红"})[g] || g + "级以上";
}
function lootAvailable(e) {
  return e.kind === "loot" && Array.isArray(e.world) && e.world.length >= 3
    && e.world.slice(0, 3).every(v => Number.isFinite(v) && Math.abs(v) <= 2097152)
    && e.world.slice(0, 3).some(v => v !== 0)
    && !["removed", "confirmed"].includes(e.pickup_status) && e.hidden !== true
    && e.item_state !== 5 && lootCount(e) !== 0;
}
function lootVisible(e) {
  const nm = lootName(e);
  if (!PREF.loot || !lootAvailable(e)) return false;
  if (!lootItemId(e) || nm === "未知物资" || /^\?{2,}/.test(nm)) return false;
  const g = lootMinGrade();
  if (g === 0) return true;
  if (!Number.isInteger(e.grade)) return true;
  return e.grade >= g && e.grade <= 6;
}
function lootIdentity(e) {
  const gid = typeof e.inventory_gid === "string" ? e.inventory_gid :
    Number.isSafeInteger(e.inventory_gid) ? String(e.inventory_gid) : "";
  return /^[1-9][0-9]*$/.test(gid) && lootItemId(e) ? gid + ":" + lootItemId(e) : null;
}
function prepareLootEntities(entities) {
  const latest = new Map(), other = [];
  for (const e of entities) {
    if (e.kind !== "loot") { other.push(e); continue; }
    const id = lootIdentity(e) || "actor:" + e.key, old = latest.get(id);
    const seq = x => Number.isFinite(x.updated_seq) ? x.updated_seq : -1;
    if (!old || seq(e) > seq(old) || (seq(e) === seq(old) && String(e.key) > String(old.key))) latest.set(id, e);
  }
  return other.concat([...latest.values()].filter(lootAvailable));
}
function lootSort(a, b) {
  const near = e => { const d = dist2(e); return Number.isFinite(d) ? d : 1e18; };
  const va = lootValue(a), vb = lootValue(b);
  const value = (va.total == null) - (vb.total == null) || (vb.total || 0) - (va.total || 0)
    || (va.unit == null) - (vb.unit == null) || (vb.unit || 0) - (va.unit || 0);
  const quality = (b.grade || 0) - (a.grade || 0), distance = near(a) - near(b);
  const order = PREF.lootsort === "dist" ? distance || quality || value :
    PREF.lootsort === "grade" ? quality || value || distance : value || quality || distance;
  return order || lootName(a).localeCompare(lootName(b), "zh") || String(a.key).localeCompare(String(b.key));
}
function lootThumb(e) {
  const id = lootItemId(e), label = lootName(e);
  return '<span class="loot-thumb" style="--c:' + (GRADE_COL[e.grade] || "#c9d1de") + '">'
    + '<span aria-hidden="true">' + esc(label === "未知物资" ? "?" : initials(label)) + '</span>'
    + (id ? '<img loading="lazy" alt="" src="' + LOOT_IMG(id) + '" onerror="this.remove()">' : '') + '</span>';
}
function lootIconHtml(e) {
  const count = lootCount(e);
  return lootThumb(e) + (count != null && count > 1 ? '<span class="loot-qty">×' + count + '</span>' : '')
    + (e.pickup_status === "candidate" ? '<span class="loot-question">?</span>' : '');
}
function loot3dSnapshot(s) {
  return { ...s, entities: prepareLootEntities(s.entities || []).filter(visible)
    .map(e => e.kind !== "loot" ? e : { ...e, name: lootName(e)
      + (lootCount(e) != null && lootCount(e) > 1 ? " ×" + lootCount(e) : "")
      + (e.pickup_status === "candidate" ? " · 疑似拾取" : "") }) };
}
/* ---------------------------------------------------- End loot display rules */

/* -------------------------------------------------------------- 主渲染 */
function visible(e) {
  if (e.kind === "container" && !PREF.container) return false;
  if (e.kind === "box"  && !PREF.box)  return false;
  if (e.kind === "box" && !PREF.aibox && (e.is_ai === true || e.is_bot === true)) return false;
  if (e.kind === "ai"   && !PREF.ai)   return false;
  if (e.kind === "loot") return lootVisible(e);
  if (e.kind === "mate" && !PREF.mate) return false;
  return true;
}
function draw(s) {
  if (lastSnap && sel) {
    const old = (lastSnap.entities || []).find(e => e.key === sel);
    const next = (s.entities || []).find(e => e.key === sel);
    if (lastSnap.flow !== s.flow || (old && next && old.kind === "loot" && next.kind === "loot"
      && lootIdentity(old) && lootIdentity(next) && lootIdentity(old) !== lootIdentity(next))) {
      sel = null; clearPick();
    }
  }
  lastSnap = s;
  const alertR = Number(PREF.alert) || 0;

  /* --- 自己 --- */
  const self = s.self;
  if (self) {
    const ll = pointLatLng(self);
    const rot = yawToScreen(self, s.self_yaw);
    const ownLife = s.self_life;
    const ownState = ownLife && ownLife.dead ? "dead" : ownLife && ownLife.downed ? "down" : null;
    const pe = { key:"__self", kind:"self", world:self, yaw:s.self_yaw, name:s.self_name || "自己", status_key:ownState, status:STATE_TXT[ownState] || null };
    const html = mkIcon(pe, C_SELF, rot);
    if (!selfMarker) {
      selfMarker = L.marker(ll, { icon: divIcon(html, [26, 26], [13, 13]), keyboard:false, zIndexOffset:1200 });
      selfMarker.bindTooltip("", { direction:"top", offset:[0,-12] });
      selfMarker.addTo(map);
      selfMarker.on("click", () => focusEntity("__self"));   // 点地图上的自己 = 跟随自己
      _rc.icon.set("__self", html);
      smPos.set("__self", { lat: ll.lat, lng: ll.lng });
    }
    if (_rc.icon.get("__self") !== html) {
      selfMarker.setIcon(divIcon(html, [26, 26], [13, 13])); _rc.icon.set("__self", html);
    }
    const spos = self[0] + "," + self[1];
    if (_rc.pos.get("__self") !== spos) { _rc.pos.set("__self", spos); smSet("__self", ll); }
    // 3D 下的 2D 小地图不靠 raf 平滑循环：数据一变就把中心对到自己
    // （仅当跟随目标是自己；点选别人时由 smStep 跟随该目标，别把人拽回来）
    if (follow && is3d() && followKey === "__self") map.panTo(ll, { animate: false });
    smSelfW = { x:self[0], y:self[1], yaw:s.self_yaw };
    const tip = tipHtml({ ...pe, world:self, rel:[0,0,0], hp:s.self_hp }, 0);
    if (_rc.tip.get("__self") !== tip) { selfMarker.setTooltipContent(tip); _rc.tip.set("__self", tip); }
    updHp("__self", ll, s.self_hp);
  } else if (selfMarker) {
    selfMarker.remove(); selfMarker = null;
    ["__self"].forEach(k => { _rc.pos.delete(k); _rc.tip.delete(k); _rc.icon.delete(k);
      smTgt.delete(k); smPos.delete(k); });
    updHp("__self", null, null);
  }

  /* --- 实体 --- */
  const seen = new Set();
  const ents = prepareLootEntities(s.entities || []);
  for (const e of ents) {
    if (!e.world || !visible(e)) continue;
    seen.add(e.key);
    const ll = pointLatLng(e.world), col = entColor(e), d = dist2(e);
    e._out_of_range = isOutOfUpdateRange(e);
    // 贴脸预警只认实时坐标：出生点标记是历史情报，闪红会造成假警报。
    // 带滞回：进入用 alertR、退出用 1.25×alertR。否则距离在阈值边缘抖动时
    // _alert 会逐帧翻转，导致徽章反复显隐 + 列表反复重排（视觉上就是闪）。
    const alertPrev = alertHold.get(e.key) === true;
    const alertLimit = alertPrev ? alertR * 1.25 : alertR;
    const alertOn = alertR > 0 && d != null && d <= alertLimit
      && !e.spawn_mark && (e.kind === "player" || e.kind === "ai") && !e.dead;
    if (alertOn !== alertPrev) alertHold.set(e.key, alertOn);
    e._alert = alertOn;
    const ppos = e.world[0] + "," + e.world[1] + "," + e.world[2];
    let m = markers.get(e.key);

    if (e.kind === "loot") {
      const cls = "loot-point" + (sel === e.key ? " sel" : "")
        + (e.pickup_status === "candidate" ? " candidate" : "");
      const html = lootIconHtml(e), signature = cls + html;
      const icon = () => L.divIcon({html, className: cls, iconSize:[30,30], iconAnchor:[15,15]});
      if (!m) {
        m = L.marker(ll, { icon:icon(), keyboard:false, zIndexOffset:sel === e.key ? 1400 : 0 });
        m.bindTooltip("", {direction:"top",offset:[0,-16]});
        m.addTo(map); markers.set(e.key,m);
        m.on("click", () => focusEntity(e.key));
        smSet(e.key, ll);
        _rc.pos.set(e.key,ppos); _rc.icon.set(e.key,signature);
      } else {
        if (_rc.pos.get(e.key) !== ppos) { m.setLatLng(ll); _rc.pos.set(e.key,ppos); smSet(e.key, ll); }
        if (_rc.icon.get(e.key) !== signature) { m.setIcon(icon()); _rc.icon.set(e.key,signature); }
        m.setZIndexOffset(sel === e.key ? 1400 : 0);
      }
      const tip = tipHtml(e,d);
      if (_rc.tip.get(e.key) !== tip) { m.setTooltipContent(tip); _rc.tip.set(e.key,tip); }
    } else if (e.kind === "box" || e.kind === "container") {
      const cls = "dbox";
      if (!m) {
        m = L.marker(ll, { icon: divIcon('<div class="' + cls + '" style="--box:'
            + boxColor(e) + '"></div>', [16,16], [8,8]), keyboard:false });
        m.bindTooltip("", { direction:"top", offset:[0,-10], interactive:true });
        m.on("tooltipopen", event => {
          const el = event.tooltip.getElement();
          if (el) L.DomEvent.disableScrollPropagation(el);
        });
        m.addTo(map); markers.set(e.key, m); _rc.pos.set(e.key, ppos);
        m.on("click", () => focusEntity(e.key));
        smSet(e.key, ll);
      } else if (_rc.pos.get(e.key) !== ppos) { m.setLatLng(ll); _rc.pos.set(e.key, ppos); smSet(e.key, ll); }
      const tip = tipHtml(e, d);
      if (_rc.tip.get(e.key) !== tip) { m.setTooltipContent(tip); _rc.tip.set(e.key, tip); }

    } else {
      const rot = yawToScreen(e.world, e.yaw);
      const html = mkIcon(e, col, rot);
      // 只有朝向会高频变化：base（不含 --rot）不变时原地改 CSS 变量即可，避免
      // setIcon 重建 DOM —— 重建会销毁并重建头像 <img>，表现为「头像突然闪一下」。
      const htmlBase = rot != null ? mkIcon(e, col, null) : html;
      if (!m) {
        m = L.marker(ll, { icon: divIcon(html, [26,26], [13,13]), keyboard:false,
          zIndexOffset: e.kind === "mate" ? 400 : 600 });
        m.bindTooltip("", { direction:"top", offset:[0,-12] });
        m.addTo(map); markers.set(e.key, m);
        m.on("click", () => focusEntity(e.key));   // 点地图上的干员 = 跟随他
        _rc.icon.set(e.key, html); _rc.iconBase.set(e.key, htmlBase); _rc.pos.set(e.key, ppos);
        smPos.set(e.key, { lat: ll.lat, lng: ll.lng });
      } else {
        if (_rc.iconBase.get(e.key) === htmlBase) {
          if (rot != null && _rc.icon.get(e.key) !== html) {
            const el = m.getElement() && m.getElement().firstElementChild;
            if (el) el.style.setProperty("--rot", rot.toFixed(1) + "deg");
            _rc.icon.set(e.key, html);
          }
        } else if (_rc.icon.get(e.key) !== html) {
          m.setIcon(divIcon(html, [26,26], [13,13]));
          _rc.icon.set(e.key, html); _rc.iconBase.set(e.key, htmlBase);
        }
        if (_rc.pos.get(e.key) !== ppos) { _rc.pos.set(e.key, ppos); smSet(e.key, ll); }
      }
      const tip = tipHtml(e, d);
      if (_rc.tip.get(e.key) !== tip) { m.setTooltipContent(tip); _rc.tip.set(e.key, tip); }

      // 头顶信息条：队友不上（同队距离无情报价值），自己不上
      let g = tags.get(e.key);
      // 信息条底边固定贴住标记顶部上方 4px：随标记大小/字体大小一起让位，不挡头像。
      // 敌人标识要叠在它正上方，所以锚点先算出来共用。
      const tFsz = (Number(PREF.fontsize) || 100) / 100;
      const tMks = (Number(PREF.dotsize) || 100) / 100;
      const tH = Math.round(24 * tFsz);
      const gearHtml = e.dead ? "" : gearBarHtml(e);
      const gearOn = PREF.gear && gearHtml;
      // 朝向锥指向上方时（±45°）信息条/头甲上提让位；指向别处时贴近圆圈
      const rotNorm = rot == null ? 180 : (((rot % 360) + 360) % 360);
      const coneUp = rot != null && Math.min(rotNorm, 360 - rotNorm) < 45;
      const circleR = 6.5 * tMks;
      const coneTip = 18 * tMks;          // 根部 4px + 锥长 14px（随标记缩放）
      const above = coneUp ? coneTip + 3 : circleR + 2;
      // 头甲行固定贴在圆圈上方，不再随朝向锥移动
      const gearAy = Math.round(circleR + 2 + 24);
      const th = (e.kind !== "mate") ? tagHtml(e, d) : "";
      const tAnchorY = Math.round((gearOn ? gearAy + 4 : above + 4) + tH);
      if (th) {
        // iconSize 必须能装下整条内容，否则 Leaflet 的固定框会把文字裁掉。
        // 估宽不可靠（字重/间距/分段 gap 都影响），改为离屏实测一次真实宽度并缓存。
        const w = measureTag(th);
        const ic = () => divIcon(th, [w, tH], [w / 2, tAnchorY]);
        if (!g) { g = L.marker(ll, { icon: ic(), keyboard:false, interactive:false, zIndexOffset:1000 });
          g.addTo(map); tags.set(e.key, g); _rc.tag.set(e.key, th); }
        else if (_rc.tag.get(e.key) !== th) { g.setIcon(ic()); _rc.tag.set(e.key, th); }
      } else if (g) { g.remove(); tags.delete(e.key); _rc.tag.delete(e.key); }

      updHp(e.key, ll, e.dead ? null : e.hp, e._out_of_range);
      updGear(e.key, ll, gearHtml, gearAy);
      updDist(e.key, ll, distBarHtml(e, col), -26);
      // 只有敌队玩家挂红色「敌人」标识：自己/队友/AI/盒子都不挂
      updFoe(e.key, ll, e.kind === "player" && e.team > 0, tAnchorY,
             !e.dead && (e.status_key === "down" || e.status_key === "dying"));
    }

    /* 轨迹：滚动窗口，位置不动 = 轨迹不变，用首尾+长度做指纹 */
    let tl = trails.get(e.key);
    if (PREF.trail && e.trail && e.trail.length > 1 && e.kind !== "loot" && e.kind !== "box") {
      const t0 = e.trail[0], t1 = e.trail[e.trail.length - 1];
      const th = t0[0] + "," + t0[1] + "|" + t1[0] + "," + t1[1] + "|" + e.trail.length
        + "|" + (e._out_of_range ? "far" : "near");
      if (_rc.trail.get(e.key) !== th) {
        const lls = e.trail.map(pointLatLng);
        if (!tl) { tl = L.polyline(lls, { color: col, weight: 1.6,
            opacity: e._out_of_range ? .10 : .38, interactive: false });
          tl.addTo(map); trails.set(e.key, tl); }
        else { tl.setLatLngs(lls); tl.setStyle({ color: col, opacity: e._out_of_range ? .10 : .38 }); }
        _rc.trail.set(e.key, th);
      }
    } else if (tl) { tl.remove(); trails.delete(e.key); _rc.trail.delete(e.key); }
  }

  /* --- 清理离场实体 --- */
  for (const [k, m] of markers) if (!seen.has(k)) {
    m.remove(); markers.delete(k);
    _rc.pos.delete(k); _rc.tip.delete(k); _rc.icon.delete(k); _rc.trail.delete(k);
    smPos.delete(k); smTgt.delete(k); alertHold.delete(k);
    const t = trails.get(k); if (t) { t.remove(); trails.delete(k); }
  }
  for (const [k, g] of tags)   if (!seen.has(k)) { g.remove(); tags.delete(k); _rc.tag.delete(k); }
  for (const [k, h] of hpbars) if (!seen.has(k) && k !== "__self") { h.remove(); hpbars.delete(k); _rc.hp.delete(k); }
  for (const [k, g] of gearbars) if (!seen.has(k) && k !== "__self") { g.remove(); gearbars.delete(k); }
  for (const [k, d] of distbars) if (!seen.has(k) && k !== "__self") { d.remove(); distbars.delete(k); }
  for (const [k, f] of foebars) if (!seen.has(k)) { f.remove(); foebars.delete(k); }
  if (sel && !seen.has(sel) && sel !== "__self") { sel = null; clearPick(); }

  drawIsland(s, ents);
  drawRoster(s, ents);
  drawPlaybar(s);
}

/* -------------------------------------------------------------- 战况岛 */
// 折叠态只回答一个问题：还剩几个敌人、最近的有多远。
function drawIsland(s, ents) {
  const nP = ents.filter(e => e.kind === "player").length;
  const nA = ents.filter(e => e.kind === "ai").length;
  const nB = ents.filter(e => e.kind === "box").length;
  const nM = ents.filter(e => e.kind === "mate").length;
  const nSelf = s.self ? 1 : 0;
  const humDead = ents.filter(e => e.kind === "player" && e.dead).length;
  const nDown = ents.filter(e => e.kind === "player" && !e.dead && e.status_key === "down").length;
  const nAlive = Math.max(0, nP - humDead - nDown);
  // 阵亡只算真人：玩家阵亡数 与「真人盒子数」取大（人机盒子不计入）
  const nBHuman = ents.filter(e => e.kind === "box"
    && e.is_ai !== true && e.is_bot !== true).length;
  const nDead = Math.max(humDead, nBHuman);
  // 最近威胁：活着的敌方玩家（AI 不算威胁基线）
  let near = null;
  for (const e of ents) {
    if (e.kind !== "player" || e.dead) continue;
    const d = dist2(e);
    if (d != null && (near == null || d < near)) near = d;
  }
  const live = s.live_active;
  const stale = ["offline", "waiting", "resync", "delayed", "error"].includes(s.status);
  const key = [nAlive, nDown, nDead, nB, nA, nM + nSelf, live ? 1 : 0, stale ? 1 : 0,
               PREF.ai ? 1 : 0, near == null ? "-" : near.toFixed(0), s.flow || "", s.status || "", s.error || ""].join("|");
  if (_rc.isl === key) return;
  _rc.isl = key;
  $("isl-num").textContent = nAlive;
  $("isl-down").textContent = nDown;
  $("isl-dead").textContent = nDead;
  $("isl-near").innerHTML = near == null ? '最近 <b>--</b>' : '最近 <b>' + near.toFixed(0) + "m</b>";
  $("c-alive").textContent = nAlive;
  $("c-dead").textContent = nDead;
  $("c-box").textContent = nB;
  $("c-ai").textContent = nA;
  // AI 格常态是「数得到、看不见」的困惑源（AI 默认关）：关着就画成暗态，
  // 点一下即开（绑定见 js4），不必去翻设置。
  $("c-ai").parentElement.classList.toggle("off", !PREF.ai);
  $("c-team").textContent = nM + nSelf;
  const il = $("island");
  il.classList.toggle("replay", !live);
  il.classList.toggle("stale", stale);
  const stMap = { offline:"未连接", waiting:"等待流量", detecting:"识别对局中", error:"解析异常", delayed:"数据暂时落后", resync:"数据缺失，请重新进入对局恢复" };
  const foot = (s.error || stMap[s.status] || (live ? "实时解析" : "回放")) + " · " + (s.flow || "识别中…");
  if ($("isl-foot").textContent !== foot) $("isl-foot").textContent = foot;
}

/* ------------------------------------------------------------ 干员台 */
// 呈现哲学：人不是一张平表，而是「按队伍成组、组内按距离升序」。
// 一行只给三件事：是谁（色+名+干员）、什么状态（徽标/血条）、多远（大字距离）。
function threatSort(a, b) {
  const da = dist2(a), db = dist2(b);
  if (da == null && db == null) return 0;
  if (da == null) return 1;
  if (db == null) return -1;
  return da - db;
}
function grpOf(e) {
  // 自己与队友同属「我方」组（组头取自己色）；组内再按绿/蓝区分
  if (e.kind === "self" || e.kind === "mate") return { id:"ally", nm:"我方", c:C_SELF, o:0 };
  if (e.kind === "box")  return { id:"box",  nm:"死亡盒", c:C_BOX, o:8 };
  if (e.kind === "ai")   return { id:"ai",   nm:"AI 单位", c:C_AI, o:7 };
  // 敌队一律同权重 o=2：组间顺序完全交给「最近距离」决定（威胁优先），
  // 否则队号会盖过距离，出现 5队165m 排在 3队94m 前面的反直觉排序。
  if (e.team > 0) return { id:"t" + e.team, nm:"第 " + e.team + " 队", c:TEAM_COLORS[(e.team-1)%TEAM_COLORS.length], o:2 };
  return { id:"unk", nm:"未知队伍", c:C_UNK, o:6 };
}
function opRow(e) {
  // 自己的相对距离恒为 0，显示成 -- 免得看着像贴脸的敌人
  const isSelf = e.kind === "self";
  const col = entColor(e), d = isSelf ? null : dist2(e), z = isSelf ? null : dz(e);
  const st = e.status_key;
  const isBox = e.kind === "box";
  const badges = [];
  // 盒子的「已搜空 / 搜刮中 / 搬运中」单独占一行，不跟名称挤在一起
  const boxState = (isBox && st && STATE_TXT[st])
    ? '<span class="badge" style="--b:' + STATE_COL[st] + '">' + STATE_TXT[st] + "</span>" : "";
  if (st && STATE_TXT[st] && !isBox) badges.push('<span class="badge" style="--b:' + STATE_COL[st] + '">' + STATE_TXT[st] + "</span>");
  else if (e.dead && !isBox) badges.push('<span class="badge ghost" style="--b:' + C_BOX + '">阵亡</span>');
  // 贴脸徽章常驻占位：显隐交给 syncOpLive，避免阈值边界抖动触发整块重建
  badges.push('<span class="badge" data-alert="1" style="--b:#ff5f6d" hidden>贴脸</span>');
  if (e._out_of_range) badges.push('<span class="badge ghost" style="--b:var(--warn)">超距·最后位置</span>');
  if (e.is_bot && !e.hero) badges.push('<span class="badge bot-confirmed" title="PlayerState 多字段一致确认">人机</span>');
  // 出生点标记（PVE AI 没有槽位轨迹，坐标只能来自出生点）：必须标出来，
  // 否则用户会当成实时位置。带龄，越旧越不可信。
  // 年龄随时间增长，不能进 html 指纹；此处只放占位，文本由 syncOpLive 刷新
  if (e.spawn_mark) badges.push('<span class="badge ghost" style="--b:var(--txt-3)">出生点'
    + (e.age_sec != null ? ' <i class="sp-age" data-age="1"></i>' : "") + "</span>");
  const gear = [];
  if (e.helmet) gear.push(gearIconHtml("helmet", e.helmet, gearValue(e.helmet_dur, e.helmet)));
  if (e.vest) gear.push(gearIconHtml("armor", e.vest, gearValue(e.vest_dur, e.vest)));
  const w = cleanWeapon(e.curr_weapon_known ? e.curr_weapon : (e.curr_weapon || e.weapon));
  const meta = [];
  if (gear.length) meta.push('<span class="gear-inline">' + gear.join("") + "</span>");
  if (w) meta.push('<span class="wp">' + esc(w) + "</span>");
  if (e.slot != null && !meta.length) meta.push("槽 " + e.slot);
  // 干员台专属：背包/胸挂显示名（1108/1107 段，liveview GEAR_NAMES 解析；
  // user 定版 0831 只上干员台，不进气泡/头顶信息条/地图）。单独一行——
  // .op-m nowrap+hidden，挤进装备行会被右侧裁切
  const bpCr = [];
  if (e.bp) bpCr.push(esc(e.bp));
  if (e.cr) bpCr.push(esc(e.cr));
  const disp = cleanUeName(e.hero || e.name || KIND_NM[e.kind] || "未知");
  const sub = (e.hero && e.name && e.hero !== e.name)
    ? '<span class="hero">' + esc(cleanUeName(e.name)) + "</span>" : "";
  // 血量：有数据就画条；部分同步时用范围表达不确定性。
  const showHp = !isBox && !e.dead && e.hp != null;   // 血量进单独容器，避免每帧重绘整块
  const details = [];
  if (boxState) details.push(boxState);
  details.push(...badges);
  details.push(...meta);
  if (bpCr.length) details.push('<span class="bags">' + bpCr.join(" · ") + "</span>");
  return '<div class="op' + (sel === e.key ? " sel" : "") + (e.dead ? " dead" : "")
    + (isBox && sel === e.key ? " expanded" : "")
    + (e._out_of_range ? " out-range" : "")
    + '" data-k="' + esc(e.key) + '">'
    + '<div class="op-av' + (isBox ? " box" : "") + '" style="--c:' + col + '"'
    + (e.hid ? ' title="干员ID ' + esc(String(e.hid)) + '"' : "") + '>'
    + (isBox ? "" : esc(initials(disp)))
    + (!isBox && e.hero ? '<img alt="" src="' + avatarUrl(e.hero)
        + '" onerror="this.remove()">' : "")
    + (st && STATE_COL[st] ? '<i class="op-ring" style="--s:' + STATE_COL[st] + '"></i>' : "")
    + "</div>"
    + '<div class="op-col">'
    +   '<div class="op-n"><span class="op-title">' + esc(disp) + sub + "</span></div>"
    +   (details.length ? '<div class="op-m">' + details.join('<i class="op-sep">·</i>') + "</div>" : "")
    +   (showHp ? '<div class="op-hp-live" data-hp="1"></div>' : "")
    +   (isBox && sel === e.key ? boxContentsHtml(e) : "")
    + "</div>"
    + '<div class="op-r"><div class="op-d tnum" data-d="1"><span class="dv">--</span><span class="du" hidden>m</span></div>'
    + (z != null ? '<div class="op-z tnum" data-z="1"></div>' : "")
    + "</div></div>";
}
function lootDistanceHtml(e) {
  const d = dist2(e), z = dz(e);
  return (Number.isFinite(d) ? d.toFixed(0) + "m" : "--")
    + (Number.isFinite(z) && Math.abs(z) >= 3 ? '<small>' + (z > 0 ? '↑ ' : '↓ ') + Math.abs(z).toFixed(0) + 'm</small>' : '');
}
function lootRow(e) {
  const col = GRADE_COL[e.grade] || "#c9d1de";
  const count = lootCount(e), value = lootValue(e);
  const detail = ['<span>' + (GRADE_NM[e.grade] || "品质未知") + '</span>'];
  detail.push('<span>' + (value.total != null ? "参考总价 " + lootMoney(value.total) :
    value.unit != null ? "单件参考 " + lootMoney(value.unit) : "参考价未知") + '</span>');
  if (count == null) detail.push('<span>数量未知</span>');
  if (e.source_type === 2) detail.push('<span>丢弃物</span>');
  if (e.pickup_status === "candidate") detail.push('<span class="loot-state">疑似拾取</span>');
  if (e.channel_state === "irrelevant") detail.push('<span>最后已知位置</span>');
  return '<div class="loot-row' + (sel === e.key ? " sel" : "") + '" data-k="' + esc(e.key) + '">'
    + lootThumb(e) + '<div class="loot-col"><span class="loot-n" style="color:' + col + '">'
    + '<span class="loot-name">' + esc(lootName(e)) + '</span>'
    + (count != null ? '<span class="loot-qty">×' + count + '</span>' : '') + '</span>'
    + '<span class="loot-m">' + detail.join('') + '</span></div>'
    + '<div class="loot-d tnum">' + lootDistanceHtml(e) + '</div></div>';
}
function renderLootRows(list, allLoot) {
  const body = $("loot-body");
  // 空态：物资关闭 / 筛选无结果 / 暂无。把文案当缓存键，变化时才重建。
  const empty = list.length ? null
    : !PREF.loot ? ["物资显示已关闭", "打开物资开关即可查看"]
    : allLoot.length ? ["当前品质筛选无结果", "可在设置中降低门槛或选择全部"]
    : ["暂无露天物资", "发现物资后显示，已确认移除的物资会自动消失"];
  const kind = empty ? "empty:" + empty[0] : "loot";
  if (body.dataset.renderKind !== kind) {
    body.replaceChildren(); lootRows.clear(); body.dataset.renderKind = kind;
    if (empty) { body.innerHTML = emptyHtml(empty[0], empty[1]); return; }
  }
  if (empty) return;
  const seen = new Set();
  let cursor = body.firstElementChild;
  for (const e of list) {
    seen.add(e.key);
    // Movement changes distance, not the thumbnail/name/quantity DOM.
    const html = lootRow({...e, rel:null});
    let row = lootRows.get(e.key);
    if (!row || row.html !== html) {
      const box = document.createElement("div"); box.innerHTML = html;
      const fresh = box.firstElementChild;
      if (!row) { row = {node:fresh}; lootRows.set(e.key,row); }
      else { row.node.className = fresh.className; row.node.replaceChildren(...fresh.childNodes); }
      row.html = html; row.distance = null;
    }
    const distance = lootDistanceHtml(e);
    if (row.distance !== distance) {
      row.node.querySelector(".loot-d").innerHTML = distance; row.distance = distance;
    }
    if (row.node !== cursor) body.insertBefore(row.node,cursor);
    cursor = row.node.nextElementSibling;
  }
  for (const [key,row] of lootRows) if (!seen.has(key)) { row.node.remove(); lootRows.delete(key); }
}
// 自己不在后端 entities 里（作为顶层 self 字段单独输出），这里补一条并入清单。
function selfAsOp(s) {
  if (!s.self) return null;
  const life = s.self_life;
  const ownState = life && life.dead ? "dead" : life && life.downed ? "down" : null;
  return { key:"__self", kind:"self", world:s.self, rel:[0,0,0], yaw:s.self_yaw || 0,
           name:s.self_name || "自己", hero:s.self_hero || null,
           status_key:ownState, status:STATE_TXT[ownState] || null,
           hp:s.self_hp, dead:!!(life && life.dead) };
}
function drawRoster(s, ents) {
  const ops = ents.filter(e => e.world && visible(e)
    && (e.kind === "player" || e.kind === "ai" || e.kind === "mate" || e.kind === "box"));
  const selfOp = selfAsOp(s);
  if (selfOp) ops.unshift(selfOp);
  const allLoot = ents.filter(lootAvailable), loot = allLoot.filter(lootVisible);
  $("n-ops").textContent = ops.length;
  $("n-loot").textContent = loot.length;
  const nEnemy = ops.filter(e => e.kind === "player" && !e.dead).length;
  const opsHead = nEnemy ? nEnemy + " 名敌人在场" : (ops.length ? ops.length + " 个目标" : "暂无目标");
  const lootHead = !PREF.loot ? "物资关闭"
    : loot.length ? loot.length + " 处物资"
    : allLoot.length ? "共 " + allLoot.length + " 处 · 筛选无结果" : "暂无物资";
  const head = opsHead + " · " + lootHead;
  if (_rc.head !== head) { $("pan-sub").textContent = head; _rc.head = head; }
  $("loot-filter-hint").textContent = lootFilterName();

  // 上区：干员（威胁 / 队伍两种读法）。展开成分组布局后增量复用节点，
  // 不再整块 innerHTML 重建 —— 重建会替换元素本身，打断鼠标下的 :hover 与过渡（表现为闪）。
  const layout = [];
  if (PREF.sort === "team") {
    // 按队伍成组：自己 → 我方 → 各敌队（按最近距离升序） → 未知 → AI → 盒子
    const groups = new Map();
    for (const e of ops) {
      const g = grpOf(e);
      if (!groups.has(g.id)) groups.set(g.id, { g: g, items: [] });
      groups.get(g.id).items.push(e);
    }
    const arr = [...groups.values()];
    for (const x of arr) {
      x.items.sort(threatSort);
      x.near = x.items.reduce((m, e) => { const d = dist2(e); return (d != null && (m == null || d < m)) ? d : m; }, null);
    }
    arr.sort((a, b) => a.g.o - b.g.o || (a.near ?? 1e9) - (b.near ?? 1e9));
    for (const x of arr) {
      const fold = foldGrp.has(x.g.id);
      layout.push({
        id: x.g.id,
        headHtml: '<div class="grp' + (fold ? " fold" : "") + '" data-g="' + x.g.id + '">'
          + '<i class="grp-bar" style="--c:' + x.g.c + '"></i>'
          + '<span class="grp-name">' + x.g.nm + "</span>"
          + '<span class="grp-cnt tnum">' + x.items.length + "</span>"
          + '<span class="grp-sp"></span>'
          + (x.near != null ? '<span class="grp-near tnum">最近 ' + x.near.toFixed(0) + "m</span>" : "")
          + '<svg class="grp-car" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg></div>',
        itemsClass: "grp-items",
        rows: x.items.map(e => ({ key: e.key, html: opRow(e) })),
      });
    }
  } else {
    // 纯威胁排序：贴脸的永远在最上面，最直接的战术读法
    const list = ops.slice().sort((a, b) => {
      const pa = (a._alert ? 0 : 1), pb = (b._alert ? 0 : 1);
      if (pa !== pb) return pa - pb;
      const ka = a.kind === "box" ? 1 : 0, kb = b.kind === "box" ? 1 : 0;
      if (ka !== kb) return ka - kb;
      return threatSort(a, b);
    });
    layout.push({ id: "flat", headHtml: null, itemsClass: null, rows: list.map(e => ({ key: e.key, html: opRow(e) })) });
  }
  renderOpRows(layout);
  syncOpLive(ops);

  // 下区：物资（同一份快照，增量渲染，距离单独更新）
  renderLootRows(loot.slice().sort(lootSort), allLoot);
}
/* 干员台增量渲染（keyed 复用）。
   内容或顺序变化时只更新受影响的节点，绝不整块 innerHTML 重建 ——
   重建会替换元素本身，导致鼠标下的 :hover 与 CSS 过渡被打断（视觉上就是卡片闪）。
   布局：每组 = 组头(.grp) + 行容器(.grp-items)；威胁模式是单个无组头的扁平分组。 */
function htmlToNode(html) {
  const box = document.createElement("div");
  box.innerHTML = html;
  return box.firstElementChild;
}
function renderOpRows(layout) {
  const body = $("ops-body");
  if (!body) return;
  const empty = !layout.length || layout.every(g => !g.rows.length);
  if (empty) {
    if (body.dataset.renderKind !== "empty") {
      body.replaceChildren(); opRows.clear(); body.dataset.renderKind = "empty";
      body.innerHTML = emptyHtml("暂无目标", "等待对局数据");
    }
    return;
  }
  if (body.dataset.renderKind !== "static") {
    body.replaceChildren(); opRows.clear(); body.dataset.renderKind = "static";
  }
  const seen = new Set();
  let cursor = body.firstElementChild;
  for (const grp of layout) {
    if (grp.headHtml != null) {
      const key = "h:" + grp.id;
      seen.add(key);
      let row = opRows.get(key);
      if (!row || row.html !== grp.headHtml) {
        const fresh = htmlToNode(grp.headHtml);
        if (!row) { row = { node: fresh }; opRows.set(key, row); }
        else { row.node.className = fresh.className; row.node.replaceChildren(...fresh.childNodes); }
        row.html = grp.headHtml;
      }
      if (row.node !== cursor) body.insertBefore(row.node, cursor);
      cursor = row.node.nextElementSibling;
    }
    const cKey = "c:" + grp.id;
    seen.add(cKey);
    let holder = opRows.get(cKey);
    if (!holder) {
      holder = { node: document.createElement("div") };
      if (grp.itemsClass) holder.node.className = grp.itemsClass;
      opRows.set(cKey, holder);
    }
    if (holder.node !== cursor) body.insertBefore(holder.node, cursor);
    cursor = holder.node.nextElementSibling;
    let inner = holder.node.firstElementChild;
    for (const item of grp.rows) {
      seen.add(item.key);
      let row = opRows.get(item.key);
      if (!row || row.html !== item.html) {
        const fresh = htmlToNode(item.html);
        if (!row) { row = { node: fresh }; opRows.set(item.key, row); }
        else { row.node.className = fresh.className; row.node.replaceChildren(...fresh.childNodes); }
        row.html = item.html;
      }
      if (row.node !== inner) holder.node.insertBefore(row.node, inner);
      inner = row.node.nextElementSibling;
    }
  }
  // 本帧未安排的节点（离场实体、消失的分组、切换排序后的旧容器）连同池条目一起清掉
  for (const [key, row] of [...opRows]) {
    if (!seen.has(key)) { row.node.remove(); opRows.delete(key); }
  }
}
/* 距离/高度差单独刷新：不进 html 指纹，避免每跳 1 米就重建整块列表 */
function syncOpLive(ops) {
  const body = $("ops-body");
  if (!body) return;
  const rows = new Map();
  for (const row of body.querySelectorAll(".op")) rows.set(row.dataset.k, row);
  for (const e of ops) {
    const row = rows.get(e.key);
    if (!row) continue;
    const isSelf = e.kind === "self";
    const d = isSelf ? null : dist2(e), z = isSelf ? null : dz(e);
    const dEl = row.querySelector(".op-d");
    if (dEl) {
      const dtxt = d != null ? String(d.toFixed(0)) : "--";
      if (dEl._v !== dtxt) {
        // 结构固定（.dv 数值 + .du 单位），只改文本，避免每次重建内部节点
        const dv = dEl.querySelector(".dv"), du = dEl.querySelector(".du");
        if (dv) dv.textContent = dtxt;
        if (du) du.hidden = d == null;
        dEl._v = dtxt;
      }
      const dcol = e._alert ? "#ff5f6d" : "var(--txt)";
      if (dEl._c !== dcol) { dEl.style.color = dcol; dEl._c = dcol; }
    }
    // 贴脸徽章与出生点年龄同样不进 html 指纹（随阈值/时间变化），在此单独刷新
    const alEl = row.querySelector("[data-alert]");
    if (alEl) alEl.hidden = !e._alert;
    const agEl = row.querySelector(".sp-age");
    if (agEl && e.age_sec != null) {
      const atxt = agoTxt(e.age_sec);
      if (agEl._v !== atxt) { agEl.textContent = atxt; agEl._v = atxt; }
    }
    const hpBox = row.querySelector("[data-hp]");
    if (hpBox && e.hp) {
      const hst = hpStat(e.hp);
      let hh = "";
      if (hst && hst.known && hst.frac < 1) {
        hh += '<div class="op-m"><span class="hpv" style="color:' + hpCol(hst.frac) + '">血 '
          + hpPct(e.hp) + "</span>"
          + (hst.worst && hst.worst.f < 1
             ? '<span style="color:var(--txt-3)"> ' + HP_NM[hst.worst.k] + " "
               + Math.round(hst.worst.cur) + "/" + Math.round(hst.worst.mx) + "</span>" : "")
          + "</div>";
      }
      hh += hpBarHtml(e.hp, "op-hp");
      if (hpBox._v !== hh) { hpBox.innerHTML = hh; hpBox._v = hh; }
    }
    const zEl = row.querySelector(".op-z");
    if (zEl && z != null) {
      const ztxt = (z > 0 ? "↑" : z < 0 ? "↓" : "") + Math.abs(z).toFixed(0) + "m";
      if (zEl._v !== ztxt) { zEl.textContent = ztxt; zEl._v = ztxt; }
      zEl.className = "op-z tnum " + (z > 3 ? "up" : z < -3 ? "dn" : "");
    }
  }
}
function emptyHtml(t, s) {
  return '<div class="empty"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/>'
    + '<path d="M12 8v4M12 16h.01"/></svg>' + t + "<br><span style=\"opacity:.6\">" + s + "</span></div>";
}

/* -------------------------------------------------------------- 敌人连线 */
/* 从屏幕上方中心点连到每个敌人的头部。颜色 = 该敌人的队伍颜色（就是 3D 标签
   自己的 color，与人物标记同色），所以一队一色，能一眼看出连的是哪一队。
   只认敌队玩家的标签（r3d 在标签上打了 data-foe），开关关闭或非 3D 时清空。
   每帧跟随标签位置重画。 */
function updateRays() {
  const svg = $("ray-layer");
  if (!svg) return;
  // 用 body 的类判断，而不是 is3d()：后者是 const 且定义在本函数之后，
  // 而下面的 IIFE 首帧就会执行到这里，引用它会命中 TDZ 并中断整个脚本。
  if (!PREF.ray || !document.body.classList.contains("mode3d")) {
    if (svg.childElementCount) svg.replaceChildren();
    return;
  }
  const box = svg.getBoundingClientRect();
  const sx = Math.round(box.width / 2), sy = 0;
  const pts = [];
  // data-foe 而不是 .foe：连线的开与关只该由「敌人连线」决定，
  // 不该顺手被「敌人标识」联动（之前关掉敌人标识，连线会一起消失）。
  for (const lbl of document.querySelectorAll('#r3-lbl .r3-lbl[data-foe="1"]')) {
    const r = lbl.getBoundingClientRect();
    if (r.width <= 0 || r.bottom < 0 || r.top > innerHeight) continue;
    // _placeLabel 把队伍色写在 style.color 上，直接拿来当线色
    pts.push([Math.round(r.left + r.width / 2 - box.left), Math.round(r.bottom - box.top),
              lbl.style.color || "#ff2d3f"]);
  }
  while (svg.childElementCount > pts.length) svg.lastElementChild.remove();
  while (svg.childElementCount < pts.length) {
    svg.appendChild(document.createElementNS("http://www.w3.org/2000/svg", "line"));
  }
  for (let i = 0; i < pts.length; i++) {
    const ln = svg.children[i];
    ln.setAttribute("x1", sx); ln.setAttribute("y1", sy);
    ln.setAttribute("x2", pts[i][0]); ln.setAttribute("y2", pts[i][1]);
    // 用内联 style 而不是属性：样式表里的 #ray-layer line 规则优先级低于内联，
    // 但要压掉原来写死的红色，只有内联（或改样式表）才稳。
    if (ln.style.stroke !== pts[i][2]) ln.style.stroke = pts[i][2];
  }
}
(function rayTick() { updateRays(); requestAnimationFrame(rayTick); })();

/* -------------------------------------------------------------- 回放轨 */
function drawPlaybar(s) {
  const live = !s.replay;
  $("playbar").classList.toggle("hide", !!live);
  if (live) return;
  const c = s.cursor || {};
  if (!seeking && c.lo != null && c.hi != null && c.hi > c.lo && c.seq != null) {
    $("seek").value = Math.round(1000 * (c.seq - c.lo) / (c.hi - c.lo));
  }
  if (s.replay) {
    document.querySelectorAll("#spd button").forEach(b => b.classList.toggle("on", Number(b.dataset.spd) === s.replay.speed));
    playing = !s.replay.paused;
    $("ic-play").innerHTML = playing ? IC_PAUSE : IC_PLAY;
  }
  let lbl = "--:-- / --:--";
  if (c.lo != null && c.hi != null && c.seq != null && c.t_lo != null && c.t_hi != null) {
    const f = c.hi > c.lo ? (c.seq - c.lo) / (c.hi - c.lo) : 0;
    const dur = c.t_hi - c.t_lo;
    lbl = fmtT(f * dur) + " / " + fmtT(dur);
  }
  if (s.replay?.indexing) lbl = "正在索引完整记录…";
  else if (s.replay?.seeking) lbl = "正在重建人物关系… " + lbl;
  if ($("tlabel").textContent !== lbl) $("tlabel").textContent = lbl;
}

/* -------------------------------------------------------------- 选中/定位 */
function clearPick() { if (pickMarker) { pickMarker.remove(); pickMarker = null; } }
function focusEntity(key) {
  const s = lastSnap; if (!s) return;
  // 点自己 = 跟随镜头（自己不在 entities 里，单独走一条分支）。
  // 3D 下点自己直接切到第三跟随；2D 下走视图跟随。
  if (key === "__self") {
    followKey = "__self";
    if (typeof R3 !== "undefined" && R3 && R3.setFollowKey) R3.setFollowKey("__self");
    setFollow(true);   // 2D 视图与小地图都跟随自己（3D 下小地图同样靠它驱动 panTo）
    if (is3d() && R3) {
      R3.setCam("chase");
      setPref("cam3d", "chase");
      r3CamSync();
    } else if (s.self) {
      const ll = pointLatLng(s.self), mb = MAP_INFO.bounds;
      const lat = Math.min(Math.max(ll.lat, Math.min(mb[0][0], mb[1][0])), Math.max(mb[0][0], mb[1][0]));
      const lng = Math.min(Math.max(ll.lng, Math.min(mb[0][1], mb[1][1])), Math.max(mb[0][1], mb[1][1]));
      map.setView([lat, lng], Math.min(Math.max(map.getZoom(), 5), map.getMaxZoom()), { animate: true });
    }
    toast("镜头跟随自己");
    if (MOBILE()) closeRoster();
    return;
  }
  const e = prepareLootEntities(s.entities || []).find(x => x.key === key && visible(x));
  sel = (sel === key || !e) ? null : key;
  _rc.roster = ""; _rc.icon.clear();
  clearPick();
  if (!sel || !e || !e.world) {
    followKey = "__self";
    if (typeof R3 !== "undefined" && R3) R3.setFollowKey("__self");
    if (lastSnap) draw(lastSnap); return;
  }
  // 点中谁就跟随谁；再点一次取消，回到跟自己
  followKey = key;
  if (typeof R3 !== "undefined" && R3 && R3.setFollowKey) R3.setFollowKey(key);
  setFollow(true);
  const ll = pointLatLng(e.world);
  // 视图目标夹进地图 bounds：原点漂移局不把视图甩进无瓦片黑屏区
  const mb = MAP_INFO.bounds;
  const lat = Math.min(Math.max(ll.lat, Math.min(mb[0][0], mb[1][0])), Math.max(mb[0][0], mb[1][0]));
  const lng = Math.min(Math.max(ll.lng, Math.min(mb[0][1], mb[1][1])), Math.max(mb[0][1], mb[1][1]));
  map.setView([lat, lng], Math.min(Math.max(map.getZoom(), 5), map.getMaxZoom()), { animate: true });
  // The existing marker owns selection; no separate item overlay can outlive it.
  draw(lastSnap);
  if (typeof r3Focus === "function") r3Focus(sel);
  if (MOBILE()) closeRoster();
}

/* ------------------------------------------------------------ 地图初始化 */
function setMap(key, quiet) {
  const m = MAPS[key] || Object.values(MAPS)[0];
  if (!m) return;
  MAP_INFO = m;
  setPref("mapkey", key);
  const mb = L.latLngBounds(m.bounds[0], m.bounds[1]);
  if (tileLayer) { map.removeLayer(tileLayer); tileLayer = null; }
  tileLayer = L.tileLayer(m.tileUrl, { tileSize: m.tileSize || 256,
    maxNativeZoom: m.maxNativeZoom || 4, bounds: mb, keepBuffer: 3 }).addTo(map);
  map.setMaxBounds(mb.pad(0.25));
  map.fitBounds(mb);
  // 切图：平面区间变了，全部缓存清空强制重算，否则旧 marker 卡在上一张图坐标
  for (const k in _rc) if (_rc[k] instanceof Map) _rc[k].clear();
  _rc.roster = ""; _rc.isl = ""; _rc.head = "";
  for (const t of trails.values()) t.remove(); trails.clear();
  for (const g of tags.values()) g.remove(); tags.clear();
  for (const h of hpbars.values()) h.remove(); hpbars.clear();
  for (const g of gearbars.values()) g.remove(); gearbars.clear();
  for (const d of distbars.values()) d.remove(); distbars.clear();
  for (const d of distbars.values()) d.remove(); distbars.clear();
  for (const f of foebars.values()) f.remove(); foebars.clear();
  for (const mk of markers.values()) mk.remove(); markers.clear();
  smPos.clear(); smTgt.clear();
  if (selfMarker) { selfMarker.remove(); selfMarker = null; }
  clearPick();
  $("map-now").textContent = m.name;
  document.querySelectorAll("#maps .mcard").forEach(c => c.classList.toggle("on", c.dataset.k === key));
  if (!quiet) toast("已切换到 " + m.name);
  if (lastSnap) draw(lastSnap);
  if (typeof r3SwitchMap === "function") r3SwitchMap();
}
// 地图卡片缩略图：取瓦片金字塔第 1 层（2x2）的右下块——比 (0,0) 角块更靠近
// 图幅主体（角块常是纯地形/纯海），一眼能认出是哪张图。
function mapThumb(m) {
  return String(m.tileUrl).replace("{z}", "1").replace("{x}", "1").replace("{y}", "1");
}
fetch("/api/map").then(r => r.json()).then(info => {
  const list = (info && info.maps) || [];
  DEF_MAP = (info && info.default) || "daba";
  for (const m of list) MAPS[m.key] = m;
  map = L.map("map", { crs: L.CRS.Simple, minZoom: 2, maxZoom: 7, preferCanvas: true,
    attributionControl: false, zoomControl: false, zoomSnap: .25, wheelPxPerZoomLevel: 90 });
  L.control.zoom({ position: "bottomright" }).addTo(map);
  $("maps").innerHTML = list.map(m =>
    '<div class="mcard" data-k="' + esc(m.key) + '">'
    + '<img loading="lazy" src="' + esc(mapThumb(m)) + '" onerror="this.style.display=\'none\'">'
    + '<div class="now"><svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></div>'
    + "<b>" + esc(m.name) + "</b></div>").join("");
  $("maps").addEventListener("click", ev => {
    const c = ev.target.closest(".mcard");
    if (c) { setMap(c.dataset.k); closeSheet(); }
  });
  const want = (PREF.mapkey && MAPS[PREF.mapkey]) ? PREF.mapkey : DEF_MAP;
  setMap(want, true);
  map.on("dragstart", () => { if (follow) setFollow(false); });
  poll(); startWs();
});

/* Local diagnostics distinguish source samples, snapshots and rendered frames. */
let gwPageFrames=0,gwUiSamples=0,gwPageFps=0,gwSnapshotHz=0,gwRateAt=performance.now();
function gwFrame(){gwPageFrames++;requestAnimationFrame(gwFrame);}requestAnimationFrame(gwFrame);
async function gwStatus(){
  const now=performance.now(),dt=Math.max(.1,(now-gwRateAt)/1000);
  gwPageFps=gwPageFrames/dt;gwSnapshotHz=gwUiSamples/dt;gwPageFrames=gwUiSamples=0;gwRateAt=now;
  try {
    const response=await fetch('/api/status',{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status);
    const s=await response.json(),r=s.rates||{},c=s.counts||{},t=s.transport||{},p=s.stats||{};
    $('gateway-rate').textContent='人物坐标 '+Number(r.characters||0).toFixed(1)+' 样本/秒 · '+(s.replay?.paused?'已暂停':s.replay?'离线回放':'实时解析');
    const render=window.gateway3d?.stat();
    $('gateway-stats').textContent=[
      '人物含待核实标记；自身姿态 '+Number(r.self||0).toFixed(1)+' Hz',
      '快照 '+gwSnapshotHz.toFixed(1)+' Hz · '+(is3d()?'3D渲染 '+Number(render?.fps||0).toFixed(1):'页面动画 '+gwPageFps.toFixed(1))+' FPS',
      '人物 '+(c.characters||0)+'（含自身 '+(c.charactersIncludingSelf||0)+'） · 确认人机 '+(c.confirmedBots ?? c.suspectedBots ?? 0)+' · AI单位 '+(c.ai||0)+' · 物资 '+(c.loot||0),
      is3d()?'3D人物 '+(render?.players||0)+' / 应显示 '+(render?.expectedCharacters||0)+' · 未标记人机 '+(render?.humanPlayers||0)+' · 地图 '+Number((render?.tris||0)/1000000).toFixed(2)+'M 面':'2D视图',
      '死亡盒 '+(c.box||0)+' · 容器 '+(c.container||0)+' · 物资ID待识别 '+(s.unknownLoot||0),
      'UDP 上行 '+(t.udpUpPackets??p.upPackets??0)+' / 下行 '+(t.udpDownPackets??p.downPackets??0),
      'Key候选 '+(t.bankCandidates||0)+' · 已验证连接 '+(t.udpBoundFlows||0),
      '输入异常 '+(p.inputErrors||0)+' · 序号缺口 '+(p.eventGaps||0)+' · 待解密 '+(t.pendingPackets||0),
      s.error||t.archiveError||t.parser?.error||'状态正常；平滑动画不会新增真实坐标样本'
    ].join('\n');
  }catch(e){$('gateway-rate').textContent='状态连接中断';}
  setTimeout(gwStatus,1000);
}gwStatus();
/* -------------------------------------------------- 数据通道：WS 推送 + 轮询兜底 */
// 服务端 /ws 有变化就推；推送失败自动回落到自适应 HTTP 轮询。
let lastUiErrorAt=0;
const POLL_FAST = 20, POLL_MAX = 100;
let pollIv = POLL_FAST, pollBusy = false, _lastBody = "";
let wsActive = false, wsRetry = 0, ws = null;

function applyState(s) {
  if (Number.isFinite(s.self_aim_yaw) && s.self_aim_age_ms <= 250) s.self_yaw = s.self_aim_yaw;
  if (s.self_position_source === 'upstream' && s.self_position_age_ms <= 250 && s.self) {
    selfHold = { p: s.self.slice(), n: 1 };
  } else s.self = normalizeSelf(s.self);
  const j = JSON.stringify(s);
  const changed = j !== _lastBody; _lastBody = j; if(changed)gwUiSamples++;
  draw(s);
  if (R3 && is3d()) R3.update(loot3dSnapshot(s));
  autoSwitchMap(s.self, s.status);
}

async function poll() {
  let changed = false;
  if (wsActive) { setTimeout(poll, POLL_MAX); return; }
  if (!pollBusy) {
    pollBusy = true;
    try {
      const r = await fetch("/api/state",{cache:"no-store"});
      applyState(await r.json());
      changed = true;
    } catch (e) { if (performance.now()-lastUiErrorAt>3000) { console.error("页面更新失败", e); toast("页面更新失败："+e.message); lastUiErrorAt=performance.now(); } }
    pollBusy = false;
  }
  pollIv = document.hidden ? 1000 : (changed ? POLL_FAST : Math.min(pollIv * 1.7 + 1, POLL_MAX));
  setTimeout(poll, pollIv);
}

function startWs() {
  if (document.hidden) { setTimeout(startWs, 1000); return; }
  let url;
  try {
    const qs = new URLSearchParams(location.search);
    const key = qs.get("key") || "";
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    url = proto + location.host + "/ws" + (key ? "?key=" + encodeURIComponent(key) : "");
  } catch (e) { return; }
  try { ws = new WebSocket(url); } catch (e) { setTimeout(startWs, 3000); return; }
  ws.onopen = () => { wsActive = true; wsRetry = 0; };
  ws.onmessage = ev => {
    const handle = txt => {
      try {
        const s = JSON.parse(txt);
        if (!s || s.type === "ping") return;
        if (s.type === "error") { console.warn("WS:", s.error); return; }
        applyState(s);
      } catch (e) {}
    };
    if (typeof ev.data === "string") handle(ev.data);
    else if (typeof Blob !== "undefined" && ev.data instanceof Blob) ev.data.text().then(handle);
    else if (ev.data instanceof ArrayBuffer) handle(new TextDecoder().decode(ev.data));
  };
  ws.onclose = () => {
    wsActive = false;
    const delay = Math.min(30000, 1000 * Math.pow(2, wsRetry++));
    setTimeout(startWs, delay);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollIv = POLL_FAST; wsRetry = 0; } });

/* ------------------------------------------------------------ 交互装配 */
// 开关统一注册：一个 id 对应一个 PREF 键 + 若干副作用，避免到处写重复代码。
function bindToggle(ids, key, after) {
  const els = ids.map($).filter(Boolean);
  const sync = () => els.forEach(el => el.classList.toggle("on", !!PREF[key]));
  els.forEach(el => el.addEventListener("click", () => {
    setPref(key, PREF[key] ? 0 : 1);
    sync();
    if (after) after();
    if (lastSnap) draw(lastSnap);
    if (R3) { R3.setPrefs(PREF); if (lastSnap && is3d()) R3.update(loot3dSnapshot(lastSnap)); }
  }));
  sync();
}
// 需要拆点重绘的开关（过滤类）：先把该类实体全部撤下，下个 draw 自然重建
function purge() {
  for (const k in _rc) if (_rc[k] instanceof Map) _rc[k].clear();
  _rc.roster = "";
  for (const m of markers.values()) m.remove(); markers.clear();
  for (const t of trails.values()) t.remove(); trails.clear();
  for (const g of tags.values()) g.remove(); tags.clear();
  for (const h of hpbars.values()) h.remove(); hpbars.clear();
  for (const g of gearbars.values()) g.remove(); gearbars.clear();
  for (const f of foebars.values()) f.remove(); foebars.clear();
  smPos.clear(); smTgt.clear();
}
function setFollow(v) {
  follow = !!v; setPref("follow", follow ? 1 : 0);
  ["s-follow","qk-follow"].forEach(i => { const el = $(i); if (el) el.classList.toggle("on", follow); });
  if (follow) smKick();
}
$("s-follow").addEventListener("click", () => { setFollow(!follow); toast(follow ? "镜头跟随开启" : "镜头跟随关闭"); });
$("qk-follow").addEventListener("click", () => { setFollow(!follow); });
setFollow(follow);

// 功能台开关与移动端快捷条共用同一个 PREF 键，bindToggle 把所有 id 的 on 态一起同步。
bindToggle(["s-trail"], "trail", () => { if (!PREF.trail) { for (const t of trails.values()) t.remove(); trails.clear(); _rc.trail.clear(); } });
bindToggle(["s-hp"], "hp", () => { if (!PREF.hp) { for (const h of hpbars.values()) h.remove(); hpbars.clear(); _rc.hp.clear(); } });
bindToggle(["s-loot","qk-loot"], "loot", purge);
bindToggle(["s-box"], "box", purge);
bindToggle(["s-container"], "container", purge);
bindToggle(["s-aibox"], "aibox", purge);
bindToggle(["s-ai"], "ai", () => { purge(); toast(PREF.ai ? "AI 单位已显示（出生点情报）" : "AI 单位已隐藏"); });
bindToggle(["s-mate"], "mate", purge);
bindToggle(["s-foe"], "foe", purge);
bindToggle(["s-ray"], "ray");   // 连线每帧自取自清，不需要重建标记
// 人物方框：方框在 r3d 里是常驻的 LineSegments，开关只切 visible，所以这里
// 同样不用重建标记 —— bindToggle 末尾那次 R3.setPrefs + R3.update 就是即时生效。
bindToggle(["s-box3d"], "box3d");
// 屏幕外预警：箭头同样在 r3d 里每帧自取自清。距离用自己的「预警距离」
// （PREF.warnd），和「贴脸高亮」解耦 —— 后者只管箭头前方那个贴上来的小红点。
// 距离 = 0 时开了也不会有箭头，那种"开了没反应"最容易被当成 bug，直接提示。
bindToggle(["s-warn3d"], "warn3d", () => {
  if (PREF.warn3d && !(Number(PREF.warnd) > 0))
    toast("屏幕外预警已开，但「预警距离」是 0 —— 先给它设个距离");
});
bindToggle(["s-name"], "name", () => _rc.tag.clear());
bindToggle(["s-wpn"], "wpn", () => _rc.tag.clear());
bindToggle(["s-gear"], "gear", () => { _rc.tag.clear(); if (!PREF.gear) { for (const g of gearbars.values()) g.remove(); gearbars.clear(); } });
bindToggle(["s-dist"], "dist", () => { if (!PREF.dist) { for (const d of distbars.values()) d.remove(); distbars.clear(); } });
bindToggle(["s-dist"], "dist", () => _rc.tag.clear());
bindToggle(["s-cone"], "cone", () => _rc.icon.clear());
bindToggle(["s-automap"], "automap");
// 战况岛「AI」格直接当开关用：数得到却看不见是最容易被当成 bug 的体验，
// 一击即开，省掉「找设置」这一步。
{
  const cell = $("c-ai") && $("c-ai").parentElement;
  if (cell) {
    cell.title = "点击切换 AI 单位显示";
    cell.addEventListener("click", (ev) => { ev.stopPropagation(); $("s-ai").click(); });
  }
}

/* --- 功能台 / 干员台开合（两舷对称，移动端互斥） --- */
function openRoster() {
  if (MOBILE()) closeDock();
  $("roster").classList.remove("hide");
  document.body.classList.remove("roster-hidden");
  if (MOBILE()) { $("roster").classList.add("up"); $("veil").classList.add("on"); document.body.classList.add("sheeting"); }
  ["s-roster","qk-roster"].forEach(i => { const el = $(i); if (el) el.classList.add("on"); });
}
function closeRoster() {
  $("roster").classList.remove("up");
  if (!MOBILE()) { $("roster").classList.add("hide"); document.body.classList.add("roster-hidden"); }
  if (MOBILE()) { $("veil").classList.remove("on"); document.body.classList.remove("sheeting"); }
  ["s-roster","qk-roster"].forEach(i => { const el = $(i); if (el) el.classList.remove("on"); });
}
function rosterOpen() { return MOBILE() ? $("roster").classList.contains("up") : !$("roster").classList.contains("hide"); }
function toggleRoster() { rosterOpen() ? closeRoster() : openRoster(); }

function openDock() {
  if (MOBILE()) closeRoster();
  $("dock").classList.remove("hide");
  document.body.classList.remove("dock-hidden");
  if (MOBILE()) { $("dock").classList.add("up"); $("veil").classList.add("on"); document.body.classList.add("sheeting"); }
}
function closeDock() {
  $("dock").classList.remove("up");
  if (!MOBILE()) { $("dock").classList.add("hide"); document.body.classList.add("dock-hidden"); }
  if (MOBILE()) { $("veil").classList.remove("on"); document.body.classList.remove("sheeting"); }
}
function dockOpen() { return MOBILE() ? $("dock").classList.contains("up") : !$("dock").classList.contains("hide"); }
function toggleDock() { dockOpen() ? closeDock() : openDock(); }

$("s-roster").addEventListener("click", toggleRoster);
$("qk-roster").addEventListener("click", toggleRoster);
$("btn-roster-close").addEventListener("click", closeRoster);
$("roster-handle").addEventListener("click", toggleRoster);
$("btn-dock-close").addEventListener("click", closeDock);
$("dock-handle").addEventListener("click", toggleDock);
$("qk-set").addEventListener("click", toggleDock);
// 桌面两舷默认展开（战术信息是主体），移动端默认收起（屏幕小，地图优先）
if (MOBILE()) { $("roster").classList.remove("hide"); closeRoster(); closeDock(); } else openRoster();

/* --- 排序 / 行点击 --- */
$("sortbar").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-sort]"); if (!b) return;
  setPref("sort", b.dataset.sort);
  document.querySelectorAll("#sortbar .mini-tab").forEach(x => x.classList.toggle("on", x === b));
  _rc.roster = "";
  if (lastSnap) draw(lastSnap);
});
document.querySelectorAll("#sortbar .mini-tab").forEach(x => x.classList.toggle("on", x.dataset.sort === PREF.sort));
$("loot-sortbar").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-loot-sort]"); if (!b) return;
  setPref("lootsort", b.dataset.lootSort);
  document.querySelectorAll("#loot-sortbar button").forEach(x => x.classList.toggle("on", x === b));
  lootRows.clear(); $("loot-body").dataset.renderKind = "";
  if (lastSnap) draw(lastSnap);
});
document.querySelectorAll("#loot-sortbar button").forEach(x => x.classList.toggle("on", x.dataset.lootSort === PREF.lootsort));
$("roster").addEventListener("click", ev => {
  const g = ev.target.closest(".grp");
  if (g) { const id = g.dataset.g; foldGrp.has(id) ? foldGrp.delete(id) : foldGrp.add(id);
    _rc.roster = ""; if (lastSnap) draw(lastSnap); return; }
  const row = ev.target.closest("[data-k]");
  if (row) focusEntity(row.dataset.k);
});

/* --- 浮层（地图） --- */
let openedSheet = null;
function openSheet(id) {
  if (openedSheet === id) return closeSheet();
  closeSheet();
  openedSheet = id;
  $(id).classList.add("on");
  // 桌面：浮层是侧边抽屉而非模态。移动端：真模态，压暗背景并收起两舷面板。
  if (MOBILE()) { $("veil").classList.add("on"); document.body.classList.add("sheeting"); closeRoster(); closeDock(); }
  const dk = { "sheet-map":"s-map" }[id];
  if (dk && $(dk)) $(dk).classList.add("on");
  const qk = { "sheet-map":"qk-map" }[id];
  if (qk && $(qk)) $(qk).classList.add("on");
}
function closeSheet() {
  if (openedSheet) {
    $(openedSheet).classList.remove("on");
    ["s-map","qk-map"].forEach(i => { const el = $(i); if (el) el.classList.remove("on"); });
    openedSheet = null;
  }
  if (!(MOBILE() && (rosterOpen() || dockOpen()))) { $("veil").classList.remove("on"); document.body.classList.remove("sheeting"); }
}
// 桌面：点浮层与功能台以外的地方即关闭（无遮罩也要有「点外面收起」的直觉）
document.addEventListener("pointerdown", function (ev) {
  if (!openedSheet || MOBILE()) return;
  if (ev.target.closest("#" + openedSheet) || ev.target.closest("#dock")) return;
  closeSheet();
}, true);
$("s-map").addEventListener("click", () => openSheet("sheet-map"));
$("qk-map").addEventListener("click", () => openSheet("sheet-map"));
document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => closeSheet()));
$("veil").addEventListener("click", () => { closeSheet(); if (MOBILE()) { closeRoster(); closeDock(); } });
document.addEventListener("keydown", ev => {
  if (ev.key === "Escape") { closeSheet(); if (MOBILE()) { closeRoster(); closeDock(); } }
});

/* --- 滑块：信息条底色 / 贴脸预警 --- */
function bindSlider(id, key, fmt, after) {
  const el = $(id), out = $("v-" + id.replace("s-", ""));
  el.value = PREF[key];
  const apply = () => { setPref(key, Number(el.value)); if (out) out.textContent = fmt(el.value); if (after) after(); };
  el.addEventListener("input", () => { apply(); if (R3) R3.setPrefs(PREF); });
  apply();
}
bindSlider("s-tagop", "tagop", v => v + "%", () => {
  document.documentElement.style.setProperty("--tagop", (Number(PREF.tagop) / 100).toFixed(2));
});
// 枪线长度：2D 走 CSS 变量；3D 读同一份 PREF（引用共享，改值即可见）
bindSlider("s-gunlen", "gunlen", v => Number(v) === 0 ? "不显示" : v + "px", () => {
  const g = Number(PREF.gunlen) || 0;
  document.documentElement.style.setProperty("--gunlen", g + "px");
  _rc.icon.clear();
  if (lastSnap) draw(lastSnap);
});
// Korr adapter receives input changes immediately. Position smoothing uses a time constant.
bindSlider("s-fpvtau", "fpvtau", v => Number(v) === 0 ? "最跟手" : v + "ms");
// 眼高/FOV 通过 setPrefs 即时传递，启动时也从本地偏好恢复。
bindSlider("s-fpvheight", "fpvheight", v => Number(v).toFixed(1) + "m");
bindSlider("s-fov", "fov", v => v + "°");
bindSlider("s-walltrans", "walltrans", v => v + "%");
bindSlider("s-floortrans", "floortrans", v => v + "%");
// 人物模型大小：只影响 3D 里的人物（自己/队友/敌人/AI），不含物资与死亡盒
bindSlider("s-charsize", "charsize", v => v + "%");
(function bindModelChoices(){
  const seg=$("model3dseg"); if(!seg)return;
  const sync=()=>seg.querySelectorAll("button").forEach(b=>b.classList.toggle("on",b.dataset.model===PREF.model3d));
  seg.querySelectorAll("button").forEach(b=>b.addEventListener("click",()=>{setPref("model3d",b.dataset.model);sync();if(R3)R3.setPrefs(PREF);toast("3D 人物模型已切换");}));
  sync();
})();
(function bindDirectionChoices(){
  const style=$('direction3dseg'),anchor=$('directionanchor3dseg');
  const sync=()=>{
    style?.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.direction===PREF.directionstyle3d));
    anchor?.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.anchor===PREF.directionanchor3d));
  };
  style?.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{setPref('directionstyle3d',b.dataset.direction);sync();if(R3)R3.setPrefs(PREF);}));
  anchor?.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{setPref('directionanchor3d',b.dataset.anchor);sync();if(R3)R3.setPrefs(PREF);}));
  sync();
})();
(function bindPanelTheme(){
  const seg=$("panelthemeseg"); if(!seg)return;
  const apply=()=>{document.body.classList.toggle("panels-light",PREF.paneltheme==="light");seg.querySelectorAll("button").forEach(b=>b.classList.toggle("on",b.dataset.theme===PREF.paneltheme));};
  seg.querySelectorAll("button").forEach(b=>b.addEventListener("click",()=>{setPref("paneltheme",b.dataset.theme);apply();}));
  apply();
})();
function bindColor(id,key){
  const el=$(id),out=$("v-"+id.replace("s-","")); if(!el)return;
  const valid=/^#[0-9a-f]{6}$/i.test(String(PREF[key]||""))?PREF[key]:PREF_DEF[key]; el.value=valid;
  const apply=()=>{const v=el.value.toLowerCase();setPref(key,v);if(out)out.textContent=v.toUpperCase();if(R3)R3.setPrefs(PREF);};
  el.addEventListener("input",apply); if(out)out.textContent=valid.toUpperCase();
}
bindColor("s-visiblecolor3d","visiblecolor3d");
bindColor("s-occludedcolor3d","occludedcolor3d");
// 预警距离（m）：进入这个距离的屏外敌人才画箭头。**独立于「贴脸高亮」** ——
// 贴脸是"近到要紧张"（默认 80m），预警是"该看一眼方向了"（默认 150m），
// 两者绑一起就只能二选一。拖到 0 = 关（和贴脸一样）。与 2D 一样带滞回？
// 不需要：箭头只是位置指示，阈值边缘抖动最多让箭头闪一下，没有重排代价。
bindSlider("s-warnd", "warnd", v => Number(v) === 0 ? "关" : v + "m");
// 预警基准圆半径（px）：只影响 r3d 画的那个圆的半径，改值即刻可见（无重建）
bindSlider("s-warnr", "warnr", v => v + "px");
// 预警箭头大小（%）：缩放三角与它旁边的距离数字（圆半径不跟着变，两者独立调）
bindSlider("s-warnsz", "warnsz", v => v + "%");
// 3D 画质：auto / perf / balanced / high（只调渲染分辨率，不动几何贴图）。
// 「自动」会按设备先给一档，之后实测帧率偏低就自动降档（r3d 的 setQuality/_autoDowngrade）。
(function bindQuality() {
  const seg = $("q3dseg");
  if (!seg) return;
  const sync = () => seg.querySelectorAll("button").forEach(b =>
    b.classList.toggle("on", b.dataset.q === (PREF.q3d || "auto")));
  seg.querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    setPref("q3d", b.dataset.q);
    sync();
    if (R3) R3.setQuality(PREF.q3d);
  }));
  sync();
})();
// 帧率上限：0 = 无上限（跟随显示器刷新率）。按固定间隔出帧可让节拍更均匀。
(function bindFpsCap() {
  const seg = $("fpscapseg");
  if (!seg) return;
  const sync = () => seg.querySelectorAll("button").forEach(b =>
    b.classList.toggle("on", Number(b.dataset.f) === (Number(PREF.fpscap) || 0)));
  seg.querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    setPref("fpscap", Number(b.dataset.f) || 0); if(R3) R3.setPrefs(PREF);
    sync();
  }));
  sync();
})();
// 标记大小 / 头顶条字体大小：整条缩放，不动 Leaflet 的锚点
bindSlider("s-dotsize", "dotsize", v => v + "%", () => {
  document.documentElement.style.setProperty("--mks", (Number(PREF.dotsize) || 100) / 100);
  _rc.tag.clear();      // 信息条锚点随标记大小重算
});
bindSlider("s-fontsize", "fontsize", v => v + "%", () => {
  document.documentElement.style.setProperty("--fs", (Number(PREF.fontsize) || 100) / 100);
  _rc.tag.clear();
});
// 状态字体粗细（头顶信息条的状态/名字/距离等一起变粗变细）
bindSlider("s-tagw", "tagweight", v => String(v), () => {
  document.documentElement.style.setProperty("--tagw", Number(PREF.tagweight) || 600);
  _rc.tag.clear();
});
bindSlider("s-alert", "alert", v => Number(v) === 0 ? "关" : v + "m", () => {
  _rc.roster = ""; _rc.icon.clear(); if (lastSnap) draw(lastSnap);
});
$("pick-loot").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-g]"); if (!b) return;
  setPref("lootmin", Number(b.dataset.g));
  document.querySelectorAll("#pick-loot button").forEach(x => x.classList.toggle("on", x === b));
  purge(); if (lastSnap) draw(lastSnap);
  if (R3 && lastSnap && is3d()) R3.update(loot3dSnapshot(lastSnap));
});
document.querySelectorAll("#pick-loot button").forEach(x =>
  x.classList.toggle("on", Number(x.dataset.g) === Number(PREF.lootmin)));

/* ------------------------------------------------------------ 3D 白模视图
   同源引擎：/m3d/r3d.js（three.js + 极简 GLB 读取），与 2D 共用同一份快照。
   模型 16~37MB，首次进图下载一次后走 ETag 长缓存；没有模型的图直接提示。 */
let R3 = null, r3busy = false, r3man = null;
const M3D = "/m3d/";
const is3d = () => !!R3 && document.body.classList.contains("mode3d");

function r3CamSync() {
  const c = R3 ? R3.camMode : (PREF.cam3d || "chase");
  document.querySelectorAll("#camseg button").forEach(b =>
    b.classList.toggle("on", b.dataset.cam === c));
}
async function r3Manifest() {
  if (r3man) return r3man;
  try { r3man = await (await fetch(M3D + "manifest.json", { cache: "no-cache" })).json(); }
  catch (e) { r3man = { maps: {} }; }
  return r3man;
}
function r3Loading(on, pct, txt) {
  const el = $("r3-load");
  el.classList.toggle("on", !!on);
  if (txt) $("r3-load-t").textContent = txt;
  $("r3-bar").firstElementChild.style.width = Math.max(0, Math.min(100, pct || 0)) + "%";
}
async function r3Model() {
  const k = (MAP_INFO && MAP_INFO.key) || DEF_MAP;
  const mf = await r3Manifest();
  const rec = mf.maps && mf.maps[k];
  if (!rec) return null;
  // GLB 长缓存必须带内容版本；只换同名文件时，force-cache 会在
  // max-age 内直接命中旧模型，ETag 根本没有机会参与重验证。
  const rev = rec.rev || rec.bytes || 1;
  return { url: M3D + rec.file + "?v=" + encodeURIComponent(rev), rec: rec, key: k };
}
// 干员台点选 -> 3D 镜头对准（2D 那边同时在做 setView，两边行为一致）
function r3Focus(key) {
  if (!is3d() || !R3) return;
  R3.focus(key, lastSnap);          // 定位会从「跟随」切到「自由」，相机条要跟着变
  setPref("cam3d", R3.camMode);
  r3CamSync();
}
// 换图：3D 开着就把新图白模换上；该图没有白模则退回 2D，不留一张错图在屏幕上
async function r3SwitchMap() {
  if (!R3 || !is3d()) return;
  const m = await r3Model();
  if (!m) { toast("该地图暂无 3D 白模，已切回 2D"); set3d(false, true); return; }
  const mb = (m.rec.bytes / 1048576).toFixed(0);
  r3Loading(true, 0, "载入 " + (m.rec.name || m.key) + " 白模 " + mb + "MB…");
  await R3.setMap(MAP_INFO, m.url, (got, tot) => r3Loading(true, tot ? got / tot * 100 : 0,
    "载入 " + (m.rec.name || m.key) + " " + (got / 1048576).toFixed(1) + "/" + mb + "MB"), m.rec);
  r3Loading(false);
  if (lastSnap) R3.update(loot3dSnapshot(lastSnap));
}

// 2D / 3D 分段与快捷条 3D 键的选中态统一在这里同步
function sync3dUI(on) {
  const d3 = $("s-3d"), d2 = $("s-2d"), qk = $("qk-3d");
  if (d3) d3.classList.toggle("on", on);
  if (d2) d2.classList.toggle("on", !on);
  if (qk) qk.classList.toggle("on", on);
}
async function set3d(on, quiet) {
  if (NO3D) return;
  if (r3busy) return;
  if (on === is3d()) return;
  if (!on) {
    document.body.classList.remove("mode3d");
    if (R3 && R3.setActive) R3.setActive(false);
    $("r3").classList.remove("on");
    $("camseg").classList.remove("on");
    $("map").classList.remove("mini");
    // 小地图窗口被拖过/缩过会留下内联样式，退出时清掉，否则 2D 主图不铺满
    ["left", "top", "right", "bottom", "width", "height"].forEach(k2 => { $("map").style[k2] = ""; });
    syncMiniHint();
    setTimeout(() => map.invalidateSize(), 60);
    r3Loading(false);
    sync3dUI(false);
    setPref("v3d", 0);
    setTimeout(() => map.invalidateSize(), 340);
    return;
  }
  r3busy = true;
  try {
    const m = await r3Model();
    if (!m) { toast("该地图暂无 3D 白模"); return; }
    if (!R3) {
      r3Loading(true, 2, "载入 3D 引擎…");
      const mod = await import(M3D + "korr-adapter.js?v=1.1.1");
      R3 = mod.create({
        canvas: $("r3-cv"), labels: $("r3-lbl"), warn: $("warn-layer"),
        mapInfo: MAP_INFO, model: m.rec, pref: PREF,
        onPick: (e) => { if (e && e.key && e.key !== "__self") focusEntity(e.key); },
        onToast: (msg) => toast(msg),
        bagLv: (typeof BAG_LV !== "undefined" ? BAG_LV : null),
        bagCap: (typeof BAG_CAP !== "undefined" ? BAG_CAP : null),
      });
      // 3D 内部因拖动平移而脱离跟随时，同步相机条高亮（不写偏好，下次进 3D 仍按原设置）
      R3.onCam = (mode) => { r3CamSync(); };
    }
    document.body.classList.add("mode3d");
    if (R3 && R3.setActive) R3.setActive(true);
    $("r3").classList.add("on");
    setTimeout(() => { miniRestore(); syncMiniHint(); }, 80);
    $("camseg").classList.add("on");
    if (PREF.pip !== 0) $("map").classList.add("mini");

    sync3dUI(true);
    setPref("v3d", 1);
    R3.resize();
    const mb = (m.rec.bytes / 1048576).toFixed(0);
    r3Loading(true, 3, "载入 " + (m.rec.name || m.key) + " 白模 " + mb + "MB…");
    await R3.setMap(MAP_INFO, m.url, (got, tot) => {
      r3Loading(true, tot ? got / tot * 100 : 0,
        "载入 " + (m.rec.name || m.key) + " " + (got / 1048576).toFixed(1) + "/" + mb + "MB");
    }, m.rec);
    r3Loading(false);
    const st = R3.stat();
    if (st.err) {
      // 同上：底图载入失败不再自动恢复，打断手机上的重载死循环
      setPref("v3d", 0);
      toast("3D 底图载入失败：" + st.err);
    }
    else if (!quiet) toast("3D 视图 · " + (st.tris / 1e6).toFixed(1) + "M 面"
      + (st.fps ? " · " + st.fps + " FPS" : ""));
    R3.setPrefs(PREF);
    R3.setQuality(PREF.q3d || "auto");     // 重进 3D 时按当前档位重设渲染分辨率
    R3.setCam(PREF.cam3d || "chase");
    // 3D 跟随时同步打开 2D 跟随，并立刻把小地图中心对到自己
    if ((PREF.cam3d || "chase") === "chase") {
      setFollow(true);
      if (lastSnap && lastSnap.self)
        setTimeout(() => map.panTo(pointLatLng(lastSnap.self), { animate: false }), 360);
    }
    r3CamSync();
    if (lastSnap) R3.update(loot3dSnapshot(lastSnap));
    setTimeout(() => map.invalidateSize(), 340);
  } catch (e) {
    r3Loading(false);
    // 载入失败就清 v3d，下次启动不再自动拉白模——否则手机上会变成
    // 「启动→载 daba→标签被杀→重载→再载」的死循环，只能手动再开 3D。
    setPref("v3d", 0);
    toast("3D 初始化失败：" + (e && e.message || e));
  } finally { r3busy = false; }
}
const NO3D=__NO3D__;   // 服务器无 web/m3d/manifest.json 时置 1：隐藏 3D 入口（8.136 定版）
if (NO3D) {
  ["qk-3d","s-3d"].forEach(i => { const el = $(i); if (el) el.style.display = "none"; });
  const pip = $("s-pip"); if (pip) pip.style.display = "none";
}
$("s-2d").addEventListener("click", () => set3d(false));
$("s-3d").addEventListener("click", () => set3d(true));
$("qk-3d").addEventListener("click", () => set3d(!is3d()));
$("camseg").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-cam]"); if (!b || !R3) return;
  document.querySelectorAll("#camseg button").forEach(x => x.classList.toggle("on", x === b));
  setPref("cam3d", b.dataset.cam);
  R3.setCam(b.dataset.cam);
});
$("s-pip").addEventListener("click", () => {
  setPref("pip", PREF.pip ? 0 : 1);
  $("s-pip").classList.toggle("on", !!PREF.pip);
  $("map").classList.toggle("mini", is3d() && PREF.pip !== 0);
  setTimeout(() => map.invalidateSize(), 340);
});
$("s-pip").classList.toggle("on", !!PREF.pip);
window.addEventListener("resize", () => { if (R3) R3.resize(); });
// 上次停在 3D 就恢复（模型走浏览器缓存，不会重复下载）
if (Number(PREF.v3d) === 1) setTimeout(() => set3d(true, true), 600);

/* --- 3D 小地图：拖动移动 / 滚轮缩放 --- */
{
  const el = $("map");
  let drag = null;
  const isMini = () => document.body.classList.contains("mode3d") && el.classList.contains("mini");
  el.addEventListener("pointerdown", e => {
    if (!isMini() || !e.shiftKey || e.button !== 0) return;   // Shift+拖动 = 移动这个框
    const r = el.getBoundingClientRect();
    drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    try { el.setPointerCapture(e.pointerId); } catch (err) {}
    e.preventDefault(); e.stopPropagation();
  }, true);
  el.addEventListener("pointermove", e => {
    if (!drag) return;
    const x = Math.max(0, Math.min(window.innerWidth - 60, e.clientX - drag.dx));
    const y = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - drag.dy));
    el.style.left = x + "px"; el.style.top = y + "px";
    el.style.right = "auto"; el.style.bottom = "auto";
    map.invalidateSize(); syncMiniHint();
    e.preventDefault(); e.stopPropagation();
  }, true);
  const endDrag = () => { drag = null; syncMiniHint(); miniSave(); };
  window.syncMiniHint = () => {
    const el = $("map"), hint = $("mini-hint");
    if (!el || !hint) return;
    const show = document.body.classList.contains("mode3d") && el.classList.contains("mini");
    hint.classList.toggle("on", show);
    if (!show) return;
    const r2 = el.getBoundingClientRect();
    hint.style.left = Math.round(r2.left) + "px";
    hint.style.top = Math.max(4, Math.round(r2.top - 22)) + "px";
    hint.style.maxWidth = Math.round(r2.width) + "px";
  };
  const MINI_KEY = "nr2_mini";
  window.miniSave = () => {
    const el = $("map");
    if (!el.classList.contains("mini")) return;
    const r2 = el.getBoundingClientRect();
    try {
      localStorage.setItem(MINI_KEY, JSON.stringify({
        l: Math.round(r2.left), t: Math.round(r2.top),
        w: Math.round(r2.width), h: Math.round(r2.height) }));
    } catch (e) {}
  };
  window.miniRestore = () => {
    const el = $("map");
    let v = null;
    try { v = JSON.parse(localStorage.getItem(MINI_KEY) || "null"); } catch (e) {}
    if (!v || !v.w) return;                       // 没记录过就用默认位置
    const w = Math.max(150, Math.min(window.innerWidth * 0.8, v.w));
    const h = Math.max(110, Math.min(window.innerHeight * 0.8, v.h || w * 0.74));
    const l = Math.max(0, Math.min(window.innerWidth - 60, v.l || 0));
    const t = Math.max(0, Math.min(window.innerHeight - 40, v.t || 0));
    el.style.left = l + "px"; el.style.top = t + "px";
    el.style.right = "auto"; el.style.bottom = "auto";
    el.style.width = Math.round(w) + "px"; el.style.height = Math.round(h) + "px";
    map.invalidateSize(); syncMiniHint();
  };
  el.addEventListener("pointerup", endDrag, true);
  el.addEventListener("pointercancel", endDrag, true);
  el.addEventListener("wheel", e => {
    if (!isMini() || !e.shiftKey) return;   // 无 Shift 时交回 Leaflet 原有缩放
    e.preventDefault(); e.stopPropagation();
    const r = el.getBoundingClientRect();
    const w = Math.max(150, Math.min(window.innerWidth * 0.7, r.width * (e.deltaY < 0 ? 1.12 : 0.89)));
    el.style.width = Math.round(w) + "px";
    el.style.height = Math.round(w * 0.74) + "px";
    map.invalidateSize(); syncMiniHint(); miniSave();
  }, { passive: false, capture: true });
}

/* --- 回放控制 --- */
const IC_PAUSE = '<path d="M6 4h4v16H6zM14 4h4v16h-4z"/>';
const IC_PLAY  = '<path d="M7 4l13 8-13 8z"/>';
let playing = true;
$("btn-play").addEventListener("click", () => {
  playing = !playing;
  $("ic-play").innerHTML = playing ? IC_PAUSE : IC_PLAY;
  fetch("/api/ctrl?pause=" + (playing ? 0 : 1));
});
$("spd").addEventListener("click", ev => {
  const b = ev.target.closest("button[data-spd]"); if (!b) return;
  document.querySelectorAll("#spd button").forEach(x => x.classList.toggle("on", x === b));
  fetch("/api/ctrl?speed=" + b.dataset.spd);
});
$("seek").addEventListener("pointerdown", () => { seeking = true; });
$("seek").addEventListener("pointerup", () => { seeking = false; fetch("/api/ctrl?seek=" + $("seek").value); });
$("seek").addEventListener("change", () => { seeking = false; fetch("/api/ctrl?seek=" + $("seek").value); });

/* --- 战况岛展开 --- */
$("island").addEventListener("click", () => $("island").classList.toggle("open"));

/* --- 窗口尺寸切换：桌面/移动端布局语义不同，重置一次开合状态 --- */
let _wasMobile = MOBILE();
window.addEventListener("resize", () => {
  const m = MOBILE();
  if (m !== _wasMobile) {
    _wasMobile = m;
    closeSheet();
    if (m) { $("roster").classList.remove("hide"); closeRoster(); } else openRoster();
  }
  if (map) map.invalidateSize();
});
</script>
</body>
 </html>"""

# 3D 能力开关：部署目录没有 web/m3d/manifest.json（如 8.136 老服务器）时把
# NO3D 置 1，前端隐藏 3D 入口、set3d 直接返回——同一份代码，行为由资产
# 是否存在决定，不做每台服务器一套 PAGE。
PAGE = PAGE.replace(
    "const NO3D=__NO3D__;",
    "const NO3D=%d;" % (0 if (M3D_DIR / "manifest.json").is_file() else 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=None, help="固定某个 run 目录名（缺省自动跟最新）")
    ap.add_argument("--flow", default=None, help="固定某个 flow 目录名（回放防轮转换图）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址（服务器部署用 0.0.0.0）")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--live", action="store_true",
                    help="实时模式：增量解析 light_relay 抓包，tail-follow 最新位置")
    ap.add_argument("--ui", choices=("korr", "er"), default=None,
                    help="首页用哪套前端：korr=Korr 雷达页（缺省），er=内置老页面"
                         "（老页面始终可从 /er 打开）")
    args = ap.parse_args()
    if args.ui:
        global KORR_UI
        KORR_UI = args.ui == "korr"
    app = RadarApp(fixed_run=args.run, live=args.live)
    if args.flow:
        app.pin_lock = (args.run, args.flow)
    Handler.app = app
    Handler.map_info = load_map_info()
    if args.live:
        threading.Thread(target=app.worker_live_flow, daemon=True).start()
    else:
        threading.Thread(target=app.worker_prelock, daemon=True).start()
        threading.Thread(target=app.worker_indexer, daemon=True).start()
        threading.Thread(target=app.worker_live, daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("[er radar] http://%s:%d (live=%s)" % (args.host, args.port, args.live), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

