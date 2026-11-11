"""ER 本地版一键入口 —— PyInstaller 打成 ER.exe，双击即用。

进程模型（与 start_local_radar.bat 三窗口等价，收进一个控制台）：
  父进程  提权 → Npcap 体检 → fix_hotspot.ps1（自适应 ICS 探测）
          → 按 ICS/FALLBACK 模式 spawn worker 子进程 → 开浏览器 → 看护
  worker  --worker radar    er_radar --live --host 0.0.0.0
          --worker capture  nic_capture（Npcap 抓热点网卡 → captures/）
          --worker dhcp     dhcp_server（仅 FALLBACK 模式）

数据根：ER_ROOT = exe 所在目录（web/m3d、src/core 数据、captures/、
dhcp_leases.json 都在旁边）；源码方式跑（python src/core/er_app.py）则
自动落到仓库根，同一入口两种形态共用。

  --check     纯自检：import 全链 + 数据文件在位，不动网络、不提权、
              不杀进程（打包后包内自检用）
  --worker X  内部用：以 worker 形态重入
  --elevate   仅在需要自动改网络配置（配热点 IP / 加防火墙规则）时才用，
              会弹 UAC；默认不提权运行
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

RADAR_URL = "http://127.0.0.1:" + os.environ.get("ER_APP_PORT", "8888")
PHONE_URL = "http://192.168.137.1:8888"
FIX_LOG = r"C:\Temp\er_fix_hotspot.log"


def _is_packaged() -> bool:
    """是否打包形态（PyInstaller / Nuitka）。

    PyInstaller 会设 sys.frozen；Nuitka 不设 sys.frozen，但它定义 __compiled__，
    并把 __file__ 伪造成一个不存在的路径。三种判据任一成立即算打包——否则产物
    会误走源码分支，按伪造 __file__ 回推出错误的仓库根（曾导致 exe 找 C:\\src\\core）。
    """
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return True
    try:
        return not Path(__file__).is_file()
    except OSError:
        return False


_PACKAGED = _is_packaged()


def _self_exe() -> str:
    """打包形态下真正的可执行文件路径。

    Nuitka standalone 里 sys.executable 指向包内的 python.exe —— standalone 目录
    并不含这个文件，拿它建子进程会 FileNotFoundError；真路径在 sys.argv[0]。
    PyInstaller 下两者都指向 exe，取哪一个都安全。
    """
    if not _PACKAGED:
        return sys.executable
    argv0 = Path(sys.argv[0] or "")
    if argv0.is_file():
        return str(argv0.resolve())
    return sys.executable


def _base_dir() -> Path:
    """打包形态 = exe 所在目录；源码跑 = 仓库根。"""
    if _PACKAGED:
        return Path(_self_exe()).resolve().parent
    return Path(__file__).resolve().parents[2]


BASE = _base_dir()
os.environ.setdefault("ER_ROOT", str(BASE))
os.environ.setdefault("ER_DATA", str(BASE))   # dhcp 租约落 exe 旁，跨重启稳定
(BASE / "src/core").mkdir(exist_ok=True)                 # radar_detect_cache.json 落点
if str(BASE / "src") not in sys.path:                       # 根级模块（src/codec/s2c_auto/…）
    sys.path.insert(0, str(BASE / "src"))
sys.path.insert(0, str(BASE / "src/client"))

APP_LOG = BASE / "data" / "er_app.log"
_DEBUG = os.environ.get("ER_DEBUG") == "1"   # 调试日志总开关（默认关，正式包静默）


def _hide_own_console() -> bool:
    """双击启动时会独占一个控制台窗口，把它藏掉。

    从已有终端启动（与别的进程共用控制台）时保持可见，方便实时看输出。
    返回是否隐藏了窗口。
    """
    if os.name != "nt":
        return False
    try:
        k = ctypes.windll.kernel32
        buf = (ctypes.c_uint * 8)()
        attached = k.GetConsoleProcessList(buf, 8)
        if attached > 1:                        # 共用控制台，别动
            return False
        ctypes.windll.user32.ShowWindow(k.GetConsoleWindow(), 0)   # SW_HIDE
        return True
    except Exception:
        return False


def _log_output(path: Path) -> None:
    """把 stdout/stderr 落到日志文件：窗口藏起来之后，报错唯一还看得见的地方。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return
    sys.stdout = handle
    sys.stderr = handle
    print("\n==== %s ====" % time.strftime("%Y-%m-%d %H:%M:%S"))


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _elevate() -> None:
    """runas 拉起自己（新控制台，工作目录 = exe 目录），本进程退出。"""
    if _PACKAGED:
        exe, params = _self_exe(), " ".join(a for a in sys.argv[1:] if a != "--no-elevate")
    else:
        exe = sys.executable
        rest = " ".join(a for a in sys.argv[1:] if a != "--no-elevate")
        params = ('"%s" %s' % (Path(__file__).resolve(), rest)).strip()
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, str(BASE), 1)
    if rc <= 32:
        print("[X] 提权被取消：Npcap 抓包需要管理员权限，右键「以管理员身份运行」重试。")
        sys.exit(1)
    sys.exit(0)


def _npcap_ready() -> bool:
    win = os.environ.get("WINDIR", r"C:\Windows")
    for d in (os.path.join(win, "System32"),
              os.path.join(win, "System32", "Npcap")):
        try:
            ctypes.WinDLL(os.path.join(d, "wpcap.dll"))
            return True
        except OSError:
            continue
    return False


def _ensure_npcap() -> bool:
    if _npcap_ready():
        return True
    print("[X] 未检测到 Npcap（抓包驱动，约 1MB，一次性安装）。")
    # 这句两个分支都要说：安装向导默认就勾着「仅管理员可抓包」，而本程序默认不提权，
    # 勾着装完 = 驱动到位但抓不到包，最难排查。
    print("    注意：安装向导里请**取消勾选**「Restrict Npcap driver's access to")
    print("    Administrators only（仅管理员可抓包）」—— 本程序默认不提权，")
    print("    保留默认勾选会导致抓包失败。")
    bundled = sorted(BASE.glob("npcap-*.exe"))
    if bundled:
        print("    启动包内安装程序：%s —— 一路「下一步」装完。" % bundled[-1].name)
        os.startfile(str(bundled[-1]))
    else:
        print("    打开下载页 https://npcap.com/#download ，装完回到本窗口按回车。")
        os.startfile("https://npcap.com/#download")
    for _ in range(3):
        try:
            input("    装好后按回车继续（Ctrl+C 退出）...")
        except EOFError:
            return False
        if _npcap_ready():
            return True
        print("    仍未检测到 wpcap.dll —— 再确认安装完成（默认下一步到底）？")
    return False


def _run_hotspot_fix() -> str:
    """跑 fix_hotspot.ps1（自适应 ICS 探测），返回 'ICS' / 'FALLBACK' / ''（未完成）。"""
    ps1 = BASE / "src/core" / "fix_hotspot.ps1"
    if not ps1.is_file():
        print("[!] 缺 src/core/fix_hotspot.ps1，跳过热点准备。")
        return ""
    mode_file = Path(os.environ.get("TEMP", str(BASE))) / "er_mode.txt"
    mode_file.unlink(missing_ok=True)
    print("[*] 热点准备：自动开热点 → ICS 体检 →（仅坏 ICS）自建 DHCP+NAT，最长约 60s ...")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps1), "-ModeFile", str(mode_file),
             "-KeepPid", str(os.getpid()),   # 杀旧实例时豁免自己（0906 闪退根因）
             "-NoDhcp"],                     # dhcp 由本进程统一 spawn（exe 机未必有 python）
            cwd=str(BASE), timeout=180, capture_output=True, text=True, errors="replace")
    except subprocess.TimeoutExpired:
        print("[!] 热点准备超时（180s），继续启动（雷达照常，入网可能不完整）。")
    mode = ""
    if mode_file.is_file():
        mode = mode_file.read_text(encoding="ascii", errors="ignore").strip()
    if mode == "ICS":
        print("[OK] 原生 ICS 热点栈健康，原样使用（未改动任何网络配置）。")
    elif mode == "FALLBACK":
        print("[OK] ICS 不可用（坏引擎机器），已自动启用内置 DHCP+NAT 替代栈。")
    else:
        print("[!] 热点准备未完成 —— 移动热点没开起来？手动开热点后重跑。日志：" + FIX_LOG)
    return mode


# ─────────────────── 子进程生命周期：Windows Job Object ───────────────────
# 直接关控制台窗口 / 任务管理器结束父进程时，父进程不会执行 finally，worker 会变成
# 孤儿继续占着 8888 / 10808 / 67，下次启动直接"端口被占用"。把每个 worker 挂进一个
# KILL_ON_JOB_CLOSE 的 Job Object 后，父进程无论怎么死（强杀、崩溃、关窗口），
# 操作系统都会连带收掉整棵树。
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_HANDLE = None
_JOB_TRIED = False


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),          # ULONG_PTR
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _k32():
    """设置 argtypes/restype —— 不设的话 64 位下返回的 HANDLE 会被截成 32 位。"""
    k = ctypes.windll.kernel32
    k.CreateJobObjectW.restype = ctypes.c_void_p
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k.SetInformationJobObject.restype = ctypes.c_int
    k.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_void_p, ctypes.c_uint32]
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.AssignProcessToJobObject.restype = ctypes.c_int
    k.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    k.CloseHandle.restype = ctypes.c_int
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


def _job_handle():
    """惰性创建 KILL_ON_JOB_CLOSE 的 job；任何失败都只返回 None，不影响主流程。"""
    global _JOB_HANDLE, _JOB_TRIED
    if _JOB_TRIED:
        return _JOB_HANDLE
    _JOB_TRIED = True
    if os.name != "nt":
        return None
    try:
        k = _k32()
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                         ctypes.byref(info), ctypes.sizeof(info)):
            return None
        _JOB_HANDLE = handle
    except Exception:
        _JOB_HANDLE = None
    return _JOB_HANDLE


def _assign_to_job(proc) -> None:
    """把 worker 挂进 job。失败只记录，不影响启动。"""
    job = _job_handle()
    if not job or proc is None:
        return
    try:
        k = _k32()
        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001
        h = k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
        if not h:
            print("[!] 无法为 worker 打开进程句柄，孤儿保护未生效（pid=%s）" % proc.pid,
                  flush=True)
            return
        try:
            if not k.AssignProcessToJobObject(job, h):
                print("[!] worker 加入 job 失败，孤儿保护未生效（pid=%s）" % proc.pid,
                      flush=True)
        finally:
            k.CloseHandle(h)
    except Exception as exc:
        print("[!] job 挂载异常：%r" % (exc,), flush=True)


_WORKER_LOGS: list = []       # 保住子进程日志句柄，别被 GC 提前关掉


def _spawn(worker: str, settings=None) -> subprocess.Popen:
    if _PACKAGED:
        cmd = [_self_exe(), "--worker", worker]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--worker", worker]
    env = dict(os.environ)
    if _DEBUG:
        # 仅 ER_DEBUG=1 时：子进程日志实时落盘 + 无缓冲，否则块缓冲会把输出吞掉。
        # 默认出厂不写任何调试日志（正式包只保留 data/er_app.log 这一个运行日志）。
        env["PYTHONUNBUFFERED"] = "1"
    if settings:
        if settings.get("mode") == "direct":
            # 「软路由直采」：把监听端口传给 capture worker（与 ER_PROXY_* 同一套做法）
            env["ER_CAP_MODE"] = "direct"
            env["ER_CAP_LISTEN_PORT"] = str(settings.get("listen_port", 9099))
        elif settings.get("mode") == "mirror":
            # 「软路由抓包」：只把「在哪张网卡上抓」传给 capture worker（与 ER_PROXY_*
            # 同一套做法）。顺手修掉一个隐患：mirror 的 settings 里没有
            # proxy_host/proxy_port，原来无条件取键会 KeyError。
            if settings.get("device"):
                env["ER_CAP_DEVICE"] = settings["device"]
            env["ER_CAP_MODE"] = "mirror"
        else:
            env.update(ER_PROXY_HOST=settings.get("proxy_host", "127.0.0.1"),
                       ER_PROXY_PORT=str(settings.get("proxy_port", 10808)))
    out = None
    if _DEBUG:
        # worker 走 CREATE_NO_WINDOW，有自己独立的控制台 —— 它的 print/异常默认完全
        # 看不见（排查时吃过亏）。仅调试模式下把子进程输出落到 data/er_workers.log。
        try:
            log = BASE / "data" / "er_workers.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            out = open(log, "ab", buffering=0)
            out.write(("\n==== %s  worker=%s ====\n"
                       % (time.strftime("%Y-%m-%d %H:%M:%S"), worker)).encode("utf-8"))
            _WORKER_LOGS.append(out)
        except OSError:
            out = None
    proc = subprocess.Popen(cmd, cwd=str(BASE), env=env, stdout=out, stderr=out,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _assign_to_job(proc)          # 父进程一旦消失，OS 连带收掉它
    return proc


def _run_worker(worker: str) -> int:
    if worker == "radar":
        sys.argv = ["er_radar", "--live", "--host", "0.0.0.0"]
        port = os.environ.get("ER_APP_PORT")     # 测试/多开用
        if port:
            sys.argv += ["--port", port]
        import er_radar
        from client_layout import adapt_page
        er_radar.PAGE = adapt_page(er_radar.PAGE)
        er_radar.main()
    elif worker == "capture":
        sys.argv = ["nic_capture"]
        # 镜像模式：由编排进程通过环境变量把选定的网卡传进来（worker 的 argv 是重置过
        # 的，拿不到门户提交的参数）。
        dev = os.environ.get("ER_CAP_DEVICE")
        if dev:
            sys.argv += ["--device", dev]
        lport = os.environ.get("ER_CAP_LISTEN_PORT")
        if lport:
            sys.argv += ["--listen-port", lport]
        import nic_capture
        return nic_capture.main()
    elif worker == "dhcp":
        sys.argv = ["dhcp_server", "--wait", "0"]
        import dhcp_server
        return dhcp_server.main()
    elif worker == "proxy":
        from desktop_modes import run_proxy
        return run_proxy(BASE)
    return 0


def _wait_radar(timeout: float = 25.0) -> bool:
    import urllib.request
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(RADAR_URL + "/api/state", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _check() -> int:
    print("[check] base =", BASE)
    ok = True
    import importlib
    mods = ("codec", "s2c_auto", "s2c_dec", "light_relay", "handshake",
            "flowload", "flowlive", "liveview", "repdec4", "repdec6",
            "repmove3", "repmove4", "sna", "blocks", "field13", "state_track",
            "ai_name_table", "nic_capture", "dhcp_server", "er_radar",
            "client_release", "client_portal", "client_layout", "desktop_modes")
    for m in mods:
        try:
            importlib.import_module(m)
            print("  import  ok  :", m)
        except Exception as e:
            ok = False
            print("  import  FAIL:", m, "->", repr(e))
    try:
        import liveview
        name_files = list(liveview._name_table_paths())
        name_files.append(BASE / "item_names_cn.json")
        if not any(p.is_file() for p in name_files):
            raise FileNotFoundError(str(name_files))
        print("  data    ok  : item_names_cn.json")
        counts = (len(liveview.HERO_NAMES), len(liveview.LOOT_NAMES),
                  len(liveview.GEAR_NAMES), len(liveview.WEAPON_NAMES))
        if not all(counts):
            raise RuntimeError("名表未解析出条目 干员/物资/装备/武器=%r" % (counts,))
        print("  names   ok  : 干员 %d / 物资 %d / 装备 %d / 武器 %d" % counts)
    except Exception as e:
        ok = False
        print("  data    FAIL: item_names_cn.json ->", repr(e))
    for rel in ("src/core/replayouts_R31.json", "src/core/subobj_replayouts_R32.json",
                "src/core/prop_meta_R36.json", "src/core/s2c_reference.npz",
                "src/core/fix_hotspot.ps1", "src/core/er-direct.sh", "web/m3d/manifest.json",
                "web/map_catalog.json"):
        p = BASE / rel
        if p.is_file():
            print("  data    ok  :", rel)
        else:
            ok = False
            print("  data    FAIL:", rel, "(缺)")
    # S2C 表磁盘缓存的**运行期实际取值**。这一项必须打出来：变体只差产物里一个
    # src/core/s2c_cache.json，而 s2c_auto 取不到它时会静默回退成「开」——
    # 光看产物有没有那个文件证明不了它在冻结 exe 里被读到了（ROOT 走 ER_ROOT）。
    # 两种取值都是正常交付形态，所以只报事实、不下"警告"结论。
    try:
        import s2c_auto
        print("  s2c     info: disk_cache=%s（ROOT=%s）"
              % ("开 → 写 src/core/flowcache" if s2c_auto.DISK_CACHE
                 else "关 → 不读不写 flowcache，表每进程现拟合",
                 s2c_auto.ROOT))
    except Exception as e:
        ok = False
        print("  s2c     FAIL:", repr(e))
    try:
        import asset_crypto
        _probe = BASE / "src/core/s2c_reference.npz"
        _blob = asset_crypto.read_bytes(_probe)
        if len(_blob) < 16:
            raise ValueError("资产为空")
        print("  assets  ok  : s2c_reference.npz（%s，解密后 %d 字节）"
              % ("密文" if asset_crypto.is_encrypted(_probe) else "明文", len(_blob)))
    except Exception as e:
        ok = False
        print("  assets  FAIL:", repr(e))
    self_exe = Path(_self_exe())
    if self_exe.is_file():
        print("  selfexe ok  :", self_exe)
    else:
        ok = False
        print("  selfexe FAIL: worker 子进程要用的可执行文件不存在 ->", self_exe)
    m3d = BASE / "web" / "m3d"
    n_glb = len(list(m3d.glob("*.glb"))) + len(list(m3d.glob("*.glb.gz")))
    print("  m3d 白模 %d 个 + manifest → 3D %s" % (n_glb, "可用" if (m3d / "manifest.json").is_file() else "关闭"))
    n_runs = 0
    try:
        for d in (BASE / "captures").iterdir():
            if d.is_dir() and d.name.startswith("run_"):
                n_runs += 1
    except OSError:
        pass
    _keep = _keep_runs()
    print("  runs   现存 %d 个（启动时%s；ER_KEEP_RUNS=N 保留最新 N 个，all=全留）"
          % (n_runs,
             "全清历史" if _keep == 0 else
             "全留、不清理" if _keep < 0 else
             "只留最新 %d 个" % _keep))
    print("  npcap  %s" % ("ok" if _npcap_ready() else "未装（首次运行 exe 会引导安装）"))
    print("[check] %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    # 藏窗口 + 输出转日志：只在"双击启动的父进程"这一形态做。
    # --check 的结果要打在终端、--worker 由父进程统一管理、--version 要回显，都不动。
    if not ({"--check", "--worker", "--version"} & set(args)):
        if _hide_own_console():
            _log_output(APP_LOG)
            print("[home] 控制台已隐藏，运行输出见 %s" % APP_LOG)
    if "--version" in args:
        from client_release import VERSION
        print(VERSION)
        return 0
    if "--worker" in args:
        return _run_worker(args[args.index("--worker") + 1])
    if "--check" in args:
        return _check()
    # 双击运行的顶层兜底：任何异常都停住展示，绝不无提示闪退（0906 教训——
    # 闪退窗口连报错都看不到）。Ctrl+C 退出时顺手终止 worker 子进程。
    try:
        return _run_parent(args)
    except KeyboardInterrupt:
        print("[bye]")
        return 0
    except Exception:
        import traceback
        print("\n[X] 启动出错：")
        traceback.print_exc()
        print("\n把上面的报错截图反馈即可。按回车退出...")
        try:
            input()
        except EOFError:
            pass
        return 1
    finally:
        # 只收 worker：captures/ 的清理放在【下次启动】做（见 _run_parent），
        # 那里没人写盘、也不受强杀影响；退出时再清一遍是多余的，而且会让
        # "关掉 ER 之后翻上一局的回放"不可能。
        _stop_kids()


_KIDS: dict = {}


def _stop_kids():
    for p in list(_KIDS.values()):
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
    _KIDS.clear()


_RADAR_OPENER = None          # GUI 模式注册：启动成功后把窗口导航到雷达

# ─────────────────── 抓包 run 保留策略（启动时清历史）───────────────────
# 热点抓包的磁盘巡检默认是关的（nic_capture --max-gb 默认 0：宁可留着也不
# 误删历史对局），于是 captures/ 只涨不消。这里在启动时统一收口：
#   · 启动清历史（起 worker 之前、无人写盘）→ 每次打开 ER 都是干净的 captures/
#   · 不做退出清理：强杀 / 关窗口 / 断电时 finally 根本不执行，本来就靠不住；
#     留着它还会让"关掉 ER 之后翻上一局的回放"变成不可能。
# 一次会话里产生的 run 累积到下次启动才清（最多一个会话的量）。
# 想永久留住的局：把目录挪进 captures/_archive/（不在清理范围内）。
KEEP_RUNS_ENV = "ER_KEEP_RUNS"


def _keep_runs() -> int:
    """启动时保留多少个历史 run。

    未设置 → 0：全清（默认，每次启动都是干净的 captures/）
    ER_KEEP_RUNS=N（N>=1）→ 只保留最新 N 个
    ER_KEEP_RUNS=all / -1 → 不清理，全留（排障时用）
    """
    raw = (os.environ.get(KEEP_RUNS_ENV) or "").strip().lower()
    if raw in ("all", "-1"):
        return -1
    if not raw:
        return 0
    try:
        return max(-1, int(raw))
    except ValueError:
        return 0


def _other_instance_alive() -> bool:
    """门户端口有人应 = 另一个 ER 正在跑（或端口被别的程序占了）。

    双开时，后启动的那个会先走到这里把 captures/ 清了、再去抢门户端口失败——
    前一个实例正在抓的局就这么没了。所以动手前先问一句：端口有人应就不清。
    """
    import socket
    port = int(os.environ.get("ER_HOME_PORT", "6677"))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def _wipe_old_runs(keep: int | None = None) -> int:
    """清理 captures/run_*，最多保留最新 keep 个（keep=0 即全清），返回删除数。

    清理是顺手做的事，任何异常都吞掉——绝不能因为删不掉就挡住启动。
    """
    if keep is None:
        keep = _keep_runs()
    if keep < 0:                         # 显式要求全留
        return 0
    if _other_instance_alive():
        print("[prune] 已有实例在运行（门户端口被占），跳过历史清理", flush=True)
        return 0
    root = BASE / "captures"
    try:
        runs = []
        for d in root.iterdir():
            if not (d.is_dir() and d.name.startswith("run_")):
                continue
            try:
                runs.append((d.stat().st_mtime, d))
            except OSError:
                continue
        runs.sort()                      # mtime 升序 = 最旧在前
        # keep=0 时不能写 runs[:-keep]：Python 里 -0 == 0，切片结果是空列表
        victims = runs if keep <= 0 else runs[:-keep]
        if not victims:
            return 0
        # 释放量按磁盘剩余空间差估算：逐目录 rglob 统计要走上千次 stat
        # （实测 86 个 run 约 1s），而这是启动路径，不值得。
        try:
            free_before = shutil.disk_usage(root).free
        except OSError:
            free_before = 0
        n = 0
        for _mt, d in victims:
            try:
                shutil.rmtree(d)
                n += 1
            except OSError as exc:
                print("[prune] 删除失败 %s：%s" % (d.name, exc), flush=True)
        if n:
            msg = ("[prune] 启动清理：清空 %d 个历史 run" % n if keep <= 0 else
                   "[prune] 启动清理：删除 %d 个旧 run，保留最新 %d 个" % (n, keep))
            if free_before:
                try:
                    freed = shutil.disk_usage(root).free - free_before
                    if freed > 0:
                        msg += ("，释放约 %.1f GB" % (freed / 1e9) if freed >= 1e9 else
                                "，释放约 %.0f MB" % (freed / 1e6) if freed >= 1e6 else
                                "，释放约 %.0f KB" % (freed / 1e3))
                except OSError:
                    pass
            print(msg, flush=True)
        return n
    except OSError:
        return 0
    except Exception as exc:             # noqa: BLE001 —— 启动路径不许抛
        print("[prune] 跳过：%r" % (exc,), flush=True)
        return 0


def _open_radar(args):
    """启动成功后的雷达入口：桌面窗口模式在窗口内跳转，否则开浏览器。"""
    if _RADAR_OPENER is not None:
        try:
            _RADAR_OPENER(RADAR_URL)
            return
        except Exception:
            pass
    if "--no-browser" not in args:
        webbrowser.open(RADAR_URL)


def _runtime_loop(args, session, server):
    """worker 生命周期：等授权与模式选择 → 起 worker → 看护。"""
    if "--start-hotspot" in args:
        session.requested = True
    # prepared_mode：按模式记「已经准备过」。原来是个布尔量，加了第三种模式后
    # 切换模式（hotspot -> mirror -> hotspot）各自需要不同准备，布尔量表达不了。
    prepared_mode, mode = "", ""
    while True:
        if "--portal-only" in args:
            time.sleep(0.5)
            continue
        if not session.active.wait(0.5) or not session.requested:
            time.sleep(0.1)
            continue
        with session.lock:
            settings = dict(session.settings)
            generation = session.generation
        if settings["mode"] != prepared_mode and settings["mode"] == "hotspot":
            # 默认不提权：热点、SoftAP IP、防火墙规则按"需已就绪"处理，缺什么由
            # 下面这行提示引导。要程序自动改网络配置才显式加 --elevate。
            if "--elevate" in args and not _is_admin():
                server.shutdown()
                server.server_close()
                if "--start-hotspot" not in sys.argv:
                    sys.argv.append("--start-hotspot")
                _elevate()
                os._exit(0)          # 提权在子线程里发起：必须整进程退出
            if not _is_admin():
                print("[i] 未提权运行。手机打不开雷达页时，用管理员执行一次："
                      "netsh advfirewall firewall add rule name=er-radar-8888 "
                      "dir=in action=allow protocol=TCP localport=8888", flush=True)
            session.runtime = "准备热点"
            if not _ensure_npcap():
                session.runtime = "Npcap 未就绪"
                session.requested = False
                continue
            mode = _run_hotspot_fix()
        elif settings["mode"] == "direct" and settings["mode"] != prepared_mode:
            # 「软路由直采」：本机不抓网卡、**不需要 Npcap**，唯一要做的就是等软路由
            # 把 pcap 流推过来。所以这里只把该在软路由上执行的命令打出来。
            session.runtime = "等待软路由接入"
            adv = settings.get("advertise_host") or "127.0.0.1"
            print("[*] 软路由直采：本机监听 %s:%s。在软路由上执行："
                  % ("0.0.0.0", settings.get("listen_port", 9099)), flush=True)
            print("    tcpdump -i <手机所在口> -U -w - udp | nc %s %s"
                  % (adv, settings.get("listen_port", 9099)), flush=True)
        elif settings["mode"] == "mirror" and settings["mode"] != prepared_mode:
            # 「软路由抓包」：不碰网络配置（不开热点、不配 NAT/转发、不发 DHCP），
            # 只确认 Npcap 在。网卡存在性已在 validate_settings 里校验；拿不到网卡
            # 列表时留给 capture worker 报明确错误。
            session.runtime = "准备抓包"
            if not _ensure_npcap():
                session.runtime = "Npcap 未就绪"
                session.requested = False
                continue
            if not _is_admin():
                print("[i] 未提权运行。手机打不开雷达页时，用管理员执行一次："
                      "netsh advfirewall firewall add rule name=er-radar-8888 "
                      "dir=in action=allow protocol=TCP localport=8888", flush=True)
        prepared_mode = settings["mode"]
        if not session.active.is_set() or not session.requested or generation != session.generation:
            continue
        session.runtime = "启动中"
        from desktop_modes import check_ports
        try:
            check_ports(settings)
        except OSError:
            session.runtime = "端口被占用，请修改端口或关闭旧实例"
            session.requested = False
            continue
        if settings["mode"] == "hotspot":
            if mode == "FALLBACK":
                _KIDS["dhcp"] = _spawn("dhcp")
            _KIDS["capture"] = _spawn("capture")
        elif settings["mode"] == "mirror":
            _KIDS["capture"] = _spawn("capture", settings)   # 只收包：不起 dhcp / proxy
        elif settings["mode"] == "direct":
            _KIDS["capture"] = _spawn("capture", settings)   # 只收 pcap 流：不起 dhcp / proxy
        else:
            _KIDS["proxy"] = _spawn("proxy", settings)
        _KIDS["radar"] = _spawn("radar")
        if _wait_radar() and all(p.poll() is None for p in _KIDS.values()):
            session.runtime = "运行中"
            _open_radar(args)
        else:
            session.runtime = "启动失败，请检查端口或网卡"
            session.requested = False
        while session.active.is_set() and session.requested and generation == session.generation:
            time.sleep(1)
            for name, p in list(_KIDS.items()):
                if p.poll() is not None:
                    print("[worker] %s exited: %s" % (name, p.returncode))
                    session.runtime = "%s 已退出，请检查端口或网卡后重启" % name
                    session.requested = False
                    break
        _stop_kids()
        if session.runtime in ("运行中", "启动中", "准备热点"):
            session.runtime = "未启动"


def _run_parent(args: list[str]) -> int:
    """启动门户服务；默认用桌面窗口承载（不自动开浏览器）。"""
    from client_portal import RuntimeSession, start_portal
    from client_release import VERSION
    import threading
    print("=" * 64)
    print(" ER %s 本地雷达（热点网卡 / SOCKS5 代理）" % VERSION)
    print("=" * 64)
    session = RuntimeSession(BASE / "data")
    _wipe_old_runs()     # 启动清历史：起 worker 之前、无人写盘，删得干净
    server = start_portal(session, int(os.environ.get("ER_HOME_PORT", "6677")))
    url = "http://127.0.0.1:%d/" % server.server_port
    print("[home]", url)
    loop = threading.Thread(target=_runtime_loop, args=(args, session, server), daemon=True)
    loop.start()
    try:
        if "--gui" in args and "--portal-only" not in args:   # 默认走网页；需要桌面窗口时加 --gui
            try:
                import webview
            except Exception:
                webview = None
            if webview is not None:
                window = webview.create_window("ER 本地雷达", url, width=940, height=820)
                globals()["_RADAR_OPENER"] = lambda u: window.load_url(u)
                print("[gui] 桌面窗口已打开（--gui 模式）")
                webview.start()
                return 0
        if "--no-browser" not in args:
            webbrowser.open(url)
        while loop.is_alive():
            time.sleep(0.5)
        return 0
    finally:
        _stop_kids()
        session.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
