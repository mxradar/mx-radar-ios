#!/bin/bash
# 用 zsign + 全能签企业证书重签 IPA。
# 用法：./scripts/sign_ipa.sh <证书.p12> <描述文件.mobileprovision> [证书密码]
# 证书文件放 ios/ 目录下（或传绝对路径）。
set -euo pipefail

cd "$(dirname "$0")/.."

P12="${1:-cert.p12}"
PROFILE="${2:-profile.mobileprovision}"
PASS="${3:-}"

# 0. 检查/下载 zsign（一次性）
if ! command -v zsign &>/dev/null; then
  echo "下载 zsign..."
  brew install zsign || true
  # 若 brew 没有，从 GitHub 拉源码编译：
  if ! command -v zsign &>/dev/null; then
    git clone --depth 1 https://github.com/zhlynn/zsign /tmp/zsign
    (cd /tmp/zsign && ./bootstrap.sh && ./configure && make && sudo make install)
  fi
fi

IN="build/MXRadar_unsigned.ipa"
OUT="build/MXRadar_signed.ipa"
[ -f "$IN" ] || { echo "!! 先运行 build_ipa.sh 生成未签名 IPA"; exit 1; }
[ -f "$P12" ] || { echo "!! 找不到证书 $P12"; exit 1; }
[ -f "$PROFILE" ] || { echo "!! 找不到描述文件 $PROFILE"; exit 1; }

echo "==> zsign 重签"
if [ -n "$PASS" ]; then
  zsign -k "$P12" -p "$PASS" -m "$PROFILE" -o "$OUT" "$IN"
else
  zsign -k "$P12" -m "$PROFILE" -o "$OUT" "$IN"
fi

echo "==> 已签名 IPA: $OUT"
echo "安装：用爱思助手/全能签工具/Apple Configurator 装到 iPhone"
