#!/bin/bash
# 在 macOS 上构建 IPA（需要 Xcode + XcodeGen + Homebrew）。
# 用法：./scripts/build_ipa.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# 0. 检查/安装 xcodegen
if ! command -v xcodegen &>/dev/null; then
  echo "安装 xcodegen..."
  brew install xcodegen
fi

# 1. 生成 Xcode 工程
xcodegen generate

# 2. 构建（先不做签名，后续 zsign 重签）
echo "==> xcodebuild build (unsigned)"
xcodebuild -project MXRadar.xcodeproj -scheme MXRadar \
  -configuration Release \
  -destination 'generic/platform=iOS' \
  CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO build

# 3. 定位产物 .app（DerivedData）
APP=$(find "$HOME/Library/Developer/Xcode/DerivedData" -name 'MXRadar.app' -path '*/Release-iphoneos/*' | head -1)
if [ -z "$APP" ]; then
  echo "!! 没找到 MXRadar.app"; exit 1
fi
echo "APP: $APP"

# 4. 打包成未签名 IPA
rm -rf build/Payload build/MXRadar_unsigned.ipa
mkdir -p build/Payload
cp -R "$APP" build/Payload/
(cd build && zip -qr MXRadar_unsigned.ipa Payload)
rm -rf build/Payload

echo "==> 未签名 IPA: build/MXRadar_unsigned.ipa"
echo "下一步：./scripts/sign_ipa.sh"
