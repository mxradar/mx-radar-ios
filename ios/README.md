# iOS 移植 — 阶段 1 代码说明

本目录是 iOS 版（三角洲行动雷达）的 Swift 源码骨架，对应 Android 端的：
- VpnService → NEPacketTunnelProvider
- StateUploader → WebSocketUploader
- 登录/连接/设置 Compose UI → SwiftUI ContentView

## 文件清单

| 文件 | 作用 |
|---|---|
| `AppGroup.swift` | App Group ID + 服务器配置 |
| `ApiClient.swift` | 登录 `POST /api/login`，拿 feed_token |
| `TunnelManager.swift` | 主 App 启动/停止 VPN |
| `ContentView.swift` | SwiftUI 登录/连接/设置界面 |
| `PacketTunnelProvider.swift` | 网络扩展：抓包 + 喂 Python + UDP 转发 + 上传 |
| `PacketParser.swift` | 解析 IPv4/IPv6 + UDP 头，拆出 local/remote/payload |
| `PythonBridge.swift` | CPython 嵌入：初始化 + SeasonRelay.handle + latest_state |
| `WebSocketUploader.swift` | gzip + WebSocket 上传到 /ws/ingest |

## Xcode 工程需要 2 个 target

1. **主 App**（SwiftUI）—— `TunnelManager` + `ContentView` + `ApiClient`
2. **Network Extension**（`NEPacketTunnelProvider`）—— `PacketTunnelProvider` + `PacketParser` + `PythonBridge` + `WebSocketUploader`

两者都要勾选同一个 **App Group**：`group.com.mx.radar`（`AppGroup.id` 里改）。

### 关键 entitlement

- Network Extension 的 **Personal VPN**：`com.apple.developer.networking.vpn.api`
- App Group：`com.apple.security.application-groups`

这两个 entitlement 都要写进描述文件（全能签证书要支持 VPN，已确认「能」）。

## Python 嵌入（阶段 2，需 Linux/云 Mac）

需要交叉编译到 iOS arm64 并打包进 extension：

1. **CPython 3.12**（静态库）
2. **NumPy**（静态库）
3. 解析器源码（`assets/py` 整个目录，含 `radar_server` + `parser_src`）

打进 extension bundle 的 `Resources/py`，`PythonBridge.start` 里用 `sys.path` 指向它。

`PythonBridge` 调用的 Python 入口（对应 Android 端已跑通的逻辑）：

```python
from radar_server.parser_bridge import LocalParserBridge
from radar_server.seasonrelay import SeasonRelay
_bridge = LocalParserBridge(output_dir, assets_dir, 17891)
_bridge.start()
_relay = SeasonRelay(_bridge.emit)
# 每包调用：_relay.handle(up, local, remote, payload, account)
# 取状态：_bridge.latest_state()
```

## 编译 + 签名（阶段 3/4，云 Mac）

```bash
# 云 Mac 上
xcodebuild -scheme MXRadar -configuration Release \
  -archivePath build/MXRadar.xcarchive archive

# 导出 IPA（用全能签的证书 + 描述文件重签）
```

然后用「全能签」工具（或 `zsign`）把 IPA 重签成企业证书版本。

## 待办（TODO）

1. `PacketTunnelProvider.processInbound` 里「重组 IP/UDP 头再 writePackets」——目前只写了负载，需要补全回包给 App 的逻辑（否则游戏收不到服务器响应）。
2. UDP 转发目前用 `NWUDPSession`，需要验证与游戏服务器的连通性。
3. `PythonBridge` 里的 CPython C API 调用需要接上真实的 `Python.h`（阶段 2 交叉编译后）。
4. 确认 `SeasonRelay.handle` 的 `local`/`remote` 方向与 iOS 抓包的方向一致（出站/入站的 up 标志）。

## 需要用户提供

- 全能签证书：`.p12` + 密码 + `.mobileprovision` 描述文件
- 云 Mac（MacinCloud / AWS Mac）
