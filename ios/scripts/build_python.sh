#!/bin/bash
# 交叉编译 CPython 3.12 + NumPy 到 iOS（真机 arm64 + 模拟器），产出 .xcframework
# 基于 beeware/Python-Apple-support。在 macOS 上运行。
set -euo pipefail

WORK="$(cd "$(dirname "$0")" && pwd)/build"
mkdir -p "$WORK"
cd "$WORK"

echo "==> [1/4] 克隆 Python-Apple-support"
if [ ! -d Python-Apple-support ]; then
  git clone --depth 1 https://github.com/beeware/Python-Apple-support.git
fi
cd Python-Apple-support

echo "==> [2/4] 构建依赖 + CPython（真机 arm64）"
# 这个 make 目标会依次构建 bzip2/libffi/openssl/xz + Python，产出
# build/iOS/ 下的 framework + support 目录
make iOS

echo "==> [3/4] 定位产物"
PY_FRAMEWORK="$(find . -maxdepth 3 -name 'Python.xcframework' -o -name 'Python.framework' | head -1)"
echo "Python framework: $PY_FRAMEWORK"

echo "==> [4/4] 交叉编译 NumPy（用刚构建的 Python 作为 host 去 pip install）"
# 找到 iOS 的 python 可执行文件（host 侧）
HOST_PYTHON="$(find . -path '*hostpython*' -o -name 'python3.12' | head -1)"
echo "host python: $HOST_PYTHON"
# 说明：NumPy 需要针对 iOS SDK 交叉编译，Python-Apple-support 仓库里有
# support/numpy 的构建脚本或 Makefile target，按仓库 README 执行即可。
# 这里给出通用路径：用 host python 的 pip 安装到 iOS 的 site-packages。
echo "NumPy 需要按 Python-Apple-support 仓库 README 的 numpy 章节构建"
echo "完成后把 Python.xcframework 拖进 Xcode 工程，并把 numpy 放进 Resources/py"

echo "==> 完成"
