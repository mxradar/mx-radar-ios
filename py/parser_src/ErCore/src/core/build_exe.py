# -*- coding: utf-8 -*-
"""build_exe.py - 用 Nuitka 把本地雷达编译成原生 ER.exe。

为什么用 Nuitka 而不是 PyInstaller：
    PyInstaller 只是把 .pyc 字节码封进包，能用 uncompyle6 / pycdc 之类反编译
    回可读源码；Nuitka 先把 Python 转成 C、再编译成机器码，产物里没有 .pyc，
    静态反编译还原源码不成立（只留下机器码与字符串常量）。本脚本再叠一层
    --python-flag=no_docstrings，去掉文档字符串常量，减少可读信息泄漏。

产物：release/ER-local/
    ER.exe                原生可执行（x64 控制台程序）
    python3xx.dll 等       运行时依赖
    web/                  3D 白模 / 干员头像 / 地图资源
    src/core/             JSON / NPZ / PS1 数据文件
    item_names_cn.json    物品名表
    TUTORIAL.md           新手教程

用法：
    python src/core/build_exe.py
    python src/core/build_exe.py --clean     # 构建前清掉旧产物与中间目录
    python src/core/build_exe.py --dry-run   # 只打印将要执行的 Nuitka 命令
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "src" / "core" / "er_app.py"
BUILD_DIR = ROOT / "build_exe"
RELEASE_DIR = ROOT / "release"
FINAL_DIR = RELEASE_DIR / "ER-local"
STAGE = BUILD_DIR / "_assets"
VERSION = "1.0.0"

sys.path.insert(0, str(ROOT / "src"))
import asset_crypto   # noqa: E402  数据保护：打包时把敏感资产加密进产物

# 运行时产物：程序跑起来才会生成，**绝不能进交付包**。
#   captures/                      抓包数据（用户的游戏流量）
#   data/                          运行日志 / 桌面设置 / DHCP 租约
#   src/core/flowcache/            恢复出的 S2C 表 + 去混淆后的载荷解析缓存
#   src/core/radar_detect_cache.json  战斗流检测结果
#   dhcp_leases.json               FALLBACK 模式下的 DHCP 租约
RUNTIME_DIRS = ("captures", "data", "src/core/flowcache")
RUNTIME_FILES = ("src/core/radar_detect_cache.json", "dhcp_leases.json")

# ── 只随包分发「数据」，绝不包含 .py / .pyc（源码不进产物）───────────────────
# src/core 的数据先复制进暂存目录，其中 asset_crypto.PROTECTED 里的敏感资产在
# 这一步被加密成密文 —— 产物里拿不到明文协议表/参考分布，拷走文件也只能得密文。
DATA_DIRS = (
    (ROOT / "web", "web"),
)


def stage_assets() -> Path:
    """把 src/core 数据 + 物品名表复制到暂存目录（敏感资产加密），返回暂存根。"""
    if STAGE.exists():
        shutil.rmtree(STAGE)
    core = STAGE / "src" / "core"
    core.mkdir(parents=True, exist_ok=True)
    protected = 0
    for p in sorted((ROOT / "src" / "core").iterdir()):
        if not p.is_file() or p.suffix == ".py":
            continue
        rel = p.relative_to(ROOT).as_posix()
        blob = p.read_bytes()
        if rel in asset_crypto.PROTECTED:
            blob = asset_crypto.encrypt(blob)
            protected += 1
        (core / p.name).write_bytes(blob)
    shutil.copy2(ROOT / "item_names_cn.json", STAGE / "item_names_cn.json")
    print("[i] 暂存数据到 %s（%d 个敏感资产已加密）" % (STAGE, protected))
    return STAGE


def collect_modules() -> list[str]:
    """er_app 的 --check 用 importlib 动态导入，Nuitka 静态分析看不到，必须显式列出。

    目录顺序 = 运行期 sys.path 优先级（er_app 把 src 与 src/client 插到最前），
    先到先得：src/s2c_dec.py 覆盖 src/core/s2c_dec.py，与实跑行为一致。
    """
    found: dict[str, Path] = {}
    skip = {ENTRY, Path(__file__).resolve()}          # 入口与构建脚本自身不进包
    for base in (ROOT / "src", ROOT / "src" / "core", ROOT / "src" / "client"):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.py")):
            if path.name == "__init__.py" or path in skip:
                continue
            found.setdefault(path.stem, path)
    return sorted(found)


def build_command(modules: list[str], jobs: int | None = None) -> list[str]:
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--msvc=latest",                     # 已装 VS 18 Community（含 VC 工具集）
        "--assume-yes-for-downloads",
        "--enable-plugin=numpy",
        "--windows-console-mode=force",      # 保留控制台：--check 输出 / Npcap 引导需要
        "--python-flag=no_docstrings",       # 去掉文档字符串常量，减少可读信息泄漏
        "--nofollow-import-to=webview",      # 可选桌面窗口依赖，er_app 已 try/except
        "--company-name=ER",
        "--product-name=ER Local Radar",
        f"--file-version={VERSION}.0",
        f"--product-version={VERSION}",
        f"--jobs={jobs or (os.cpu_count() or 4)}",

        f"--output-dir={BUILD_DIR}",
        "--output-filename=ER.exe",
        "--include-package=codec",
    ]
    for src, dest in DATA_DIRS:
        cmd.append(f"--include-data-dir={src.as_posix()}={dest}")
    cmd.append(f"--include-data-dir={(STAGE / 'src' / 'core').as_posix()}=src/core")
    cmd.append(f"--include-data-file={(STAGE / 'item_names_cn.json').as_posix()}=item_names_cn.json")
    for name in modules:
        cmd.append(f"--include-module={name}")
    cmd.append(str(ENTRY))
    return cmd


def verify_dist(dist: Path) -> bool:
    """产物自检：不得出现任何 .py / .pyc，且关键数据文件在位。"""
    ok = True
    leaks = [p for p in dist.rglob("*") if p.suffix.lower() in (".py", ".pyc", ".pyo")]
    if leaks:
        ok = False
        print("[X] 产物内发现源码/字节码残留（应清零）：")
        for p in leaks[:20]:
            print("    ", p.relative_to(dist))
    else:
        print("[OK] 产物内无 .py/.pyc/.pyo —— 静态反编译拿不到源码")
    required = ("ER.exe", "item_names_cn.json", "web/m3d/manifest.json",
                "web/map_catalog.json", "src/core/fix_hotspot.ps1",
                "src/core/replayouts_R31.json", "src/core/s2c_reference.npz",
                "web/korr/radar.html", "web/korr/static/js/er-live.js")
    for rel in required:
        if not (dist / rel).is_file():
            ok = False
            print("[X] 缺少数据文件:", rel)
    stale = [rel for rel in RUNTIME_DIRS + RUNTIME_FILES if (dist / rel).exists()]
    if stale:
        ok = False
        print("[X] 产物内含运行时产物，禁止交付:", ", ".join(stale))
    else:
        print("[OK] 产物内无运行时产物（抓包/日志/flowcache/检测缓存/租约）")
    plain = [rel for rel in asset_crypto.PROTECTED
             if not (dist / rel).is_file() or not asset_crypto.is_encrypted(dist / rel)]
    if plain:
        ok = False
        print("[X] 以下敏感资产缺失或仍为明文:", ", ".join(plain))
    else:
        print("[OK] %d 个敏感资产在产物内均为密文（拷走文件只得密文）" % len(asset_crypto.PROTECTED))
    files = [p for p in dist.rglob("*") if p.is_file()]
    size = sum(p.stat().st_size for p in files)
    print("[i] 产物 %d 个文件，共 %.1f MB" % (len(files), size / 1024 / 1024))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Nuitka 打包本地雷达")
    ap.add_argument("--clean", action="store_true", help="构建前清掉 build_exe/ 与产物目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    ap.add_argument("--out", default=None,
                    help="产物目录（缺省 release/ER-local）。要覆盖正在运行的实例时，"
                         "先打到旁边目录、关掉旧实例再换过去。")
    ap.add_argument("--no-s2c-cache", action="store_true",
                    help="打成「不写缓存」变体：往产物里注入 src/core/s2c_cache.json "
                         "（disk_cache=false）。运行期不读不写 flowcache，S2C 表每进程"
                         "现拟合；表是每连接独立的，不影响解码正确性。")
    ap.add_argument("--jobs", type=int, default=None,
                    help="Nuitka 并行编译任务数（缺省=CPU 核数）。雷达正在实时跑时调小"
                         "（如 --jobs 4），免得把抓包/解析进程挤卡。")
    args = ap.parse_args()

    # 产物目录可换：旧实例还在从 release/ER-local 跑时，直接覆盖会被下面的守卫拦下。
    global FINAL_DIR
    if args.out:
        FINAL_DIR = (ROOT / args.out).resolve()

    if not ENTRY.is_file():
        print("[X] 找不到入口:", ENTRY)
        return 1

    if args.clean:
        for d in (BUILD_DIR, FINAL_DIR):
            if d.exists():
                print("[clean]", d)
                shutil.rmtree(d, ignore_errors=True)

    modules = collect_modules()
    stage_assets()
    if args.no_s2c_cache:
        # 只写进暂存目录（产物会带上），不动仓库里的 src/core
        flag = STAGE / "src" / "core" / "s2c_cache.json"
        flag.write_text('{\n  "disk_cache": false\n}\n', encoding="utf-8")
        print("[i] 不写缓存变体：已注入 %s" % flag.relative_to(ROOT))
    cmd = build_command(modules, args.jobs)
    print("[i] 显式包含模块 %d 个：%s" % (len(modules), ", ".join(modules)))
    if args.dry_run:
        print(" ".join(cmd))
        return 0

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT / "src" / "core"), str(ROOT / "src" / "client"),
         env.get("PYTHONPATH", "")]).strip(os.pathsep)

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    print("[i] 开始 Nuitka 构建（首次较慢，请耐心）...")
    rc = subprocess.call(cmd, cwd=str(ROOT), env=env)
    if rc != 0:
        print("[X] Nuitka 构建失败，退出码", rc)
        return rc

    dists = sorted(BUILD_DIR.glob("*.dist"))
    if not dists:
        print("[X] 未找到 .dist 产物目录")
        return 1
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    if FINAL_DIR.exists():
        shutil.rmtree(FINAL_DIR, ignore_errors=True)
    # rmtree 失败基本只有一个原因：产物目录里的 ER.exe 还在跑（文件被占用）。此时
    # shutil.move 会把新产物「塞进」旧目录（变成 ER-local/er_app.dist/），留下一个
    # 四不像的交付目录 —— 宁可在这里停下，也不要静默产出坏包。
    if FINAL_DIR.exists():
        print("[X] 旧产物目录没能清空（%s 里可能还有 ER.exe 在运行）。"
              "请先关掉它再重新构建。" % FINAL_DIR)
        return 1
    shutil.move(str(dists[0]), str(FINAL_DIR))
    print("[OK] 产物目录:", FINAL_DIR)

    # 防御性清理：就算源目录里混进了运行期产物，也不让它进交付包
    for rel in RUNTIME_DIRS:
        target = FINAL_DIR / rel
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            print("[clean] 已从产物移除运行时目录:", rel)
    for rel in RUNTIME_FILES:
        target = FINAL_DIR / rel
        if target.exists():
            target.unlink()
            print("[clean] 已从产物移除运行时文件:", rel)

    tutorial = ROOT / "docs" / "TUTORIAL.md"
    if tutorial.is_file():
        shutil.copy2(tutorial, FINAL_DIR / "TUTORIAL.md")

    # Npcap 安装器必须随包：装机的人大多没外网、npcap.com 也常打不开，缺了它
    # exe 只能弹下载页干等（er_app._ensure_npcap 里的 bundled 分支）。
    installers = sorted(ROOT.glob("npcap-*.exe"))
    if installers:
        shutil.copy2(installers[-1], FINAL_DIR / installers[-1].name)
        print("[i] 已打包 Npcap 安装器:", installers[-1].name)
    else:
        print("[!] 未找到 npcap-*.exe —— 交付包不带抓包驱动安装器，"
              "对方无外网时装不上（放一个到仓库根目录即可）")

    return 0 if verify_dist(FINAL_DIR) else 1


if __name__ == "__main__":
    sys.exit(main())
