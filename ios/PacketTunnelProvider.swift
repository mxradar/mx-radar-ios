import Foundation
import NetworkExtension
import Network

/// 网络扩展：VPN 抓包 + 喂给 Python 解析器 + UDP 转发 + 上传雷达数据。
final class PacketTunnelProvider: NEPacketTunnelProvider {

    // 每条 UDP 流一个 NWUDPSession（key = "local|remote"）
    private var udpSessions: [String: NWUDPSession] = [:]
    private var uploader: WebSocketUploader?

    private var account = ""
    private var feedToken = ""

    // 解析器输出目录（App Group 共享，让主 App 能读）
    private var assetsDir: String {
        // 解析器文件打包进 extension bundle 的 Resources/py
        return (Bundle.main.resourcePath ?? "") + "/py"
    }
    private var outputDir: String {
        let c = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: AppGroup.id)
        return (c?.path ?? NSTemporaryDirectory()) + "/parser_out"
    }

    override func startTunnel(options: [String : NSObject]?,
                              completionHandler: @escaping (Error?) -> Void) {
        // 读取账号 + feed_token（主 App 通过 UserDefaults 共享传入）
        let ud = UserDefaults(suiteName: AppGroup.id)
        account = ud?.string(forKey: "account") ?? ""
        feedToken = ud?.string(forKey: "feed_token") ?? ""

        // 1. 隧道网络设置：全流量走 VPN（简化；生产可按需加路由）
        let settings = NEPacketTunnelNetworkSettings(tunnelRemoteAddress: "10.0.0.1")
        settings.mtu = 1400
        let ipv4 = NEIPv4Settings(addresses: ["10.0.0.2"], subnetMasks: ["255.255.255.0"])
        ipv4.includedRoutes = [NEIPv4Route.default()]
        settings.ipv4Settings = ipv4
        settings.dnsSettings = NEDNSSettings(servers: ["223.5.5.5", "8.8.8.8"])

        setTunnelNetworkSettings(settings) { [weak self] err in
            guard let self else { completionHandler(err); return }
            if let err { completionHandler(err); return }

            // 2. 启动 Python 解析器
            let ok = PythonBridge.shared.start(assetsDir: self.assetsDir, outputDir: self.outputDir)
            if !ok {
                completionHandler(NSError(domain: "PythonBridge", code: -1,
                    userInfo: [NSLocalizedDescriptionKey: "Python 初始化失败"]))
                return
            }

            // 3. 上传器
            self.uploader = WebSocketUploader(account: self.account, token: self.feedToken)

            // 4. 抓包循环 + 上传循环
            self.startReadLoop()
            self.startUploadLoop()

            completionHandler(nil)
        }
    }

    override func stopTunnel(with reason: NEProviderStopReason, completionHandler: @escaping () -> Void) {
        PythonBridge.shared.stop()
        uploader?.stop()
        for (_, s) in udpSessions { s.cancel() }
        udpSessions.removeAll()
        completionHandler()
    }

    // MARK: 抓包循环（出站）

    private func startReadLoop() {
        packetFlow.readPackets { [weak self] packets, _ in
            guard let self else { return }
            for packet in packets {
                self.processOutbound(packet)
            }
            self.startReadLoop() // 继续读下一批
        }
    }

    private func processOutbound(_ packet: Data) {
        guard let parsed = PacketParser.parse(packet, isOutbound: true) else {
            // 非 UDP 或解析失败：原样转发（TCP 等直接丢弃由系统处理，这里只关心 UDP）
            return
        }
        // 喂给 Python（SeasonRelay.handle）
        PythonBridge.shared.handle(up: parsed.up, local: parsed.local,
                                   remote: parsed.remote, payload: parsed.payload,
                                   account: account)

        // 转发：找到/创建该流的 UDP 会话，把负载发到远端
        let key = parsed.local + "|" + parsed.remote
        let session = udpSessions[key] ?? makeSession(local: parsed.local, remote: parsed.remote, key: key)
        if let session, session.state == .ready {
            session.writeDatagram(parsed.payload) { _ in }
        }
    }

    private func makeSession(local: String, remote: String, key: String) -> NWUDPSession? {
        let host = remote.components(separatedBy: ":").dropLast().joined(separator: ":")
        let portStr = remote.components(separatedBy: ":").last ?? ""
        guard NWEndpoint.Port(portStr) != nil else { return nil }
        let ep = NWHostEndpoint(hostname: host, port: portStr)
        let session = createUDPSession(to: ep, from: nil)
        session.setReadHandler { [weak self] (datagrams: [Data]?, err: Error?) in
            guard let self, err == nil, let datagrams else { return }
            for d in datagrams {
                self.processInbound(d, local: local, remote: remote)
            }
        }
        udpSessions[key] = session
        return session
    }

    private func processInbound(_ payload: Data, local: String, remote: String) {
        // 入站：服务器 → 客户端。local=客户端, remote=服务器, up=false
        PythonBridge.shared.handle(up: false, local: local, remote: remote,
                                   payload: payload, account: account)
        // 写回给 App（把原始 UDP 负载包回 IP 头，简化：这里只写负载，生产需重组 IP/UDP 头）
        // TODO: 重组完整 IP/UDP 包再 packetFlow.writePackets
    }

    // MARK: 上传循环

    private func startUploadLoop() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            while let self, !self.isStopping() {
                if let state = PythonBridge.shared.latestState(), !state.isEmpty {
                    self.uploader?.send(state)
                }
                Thread.sleep(forTimeInterval: 0.1)
            }
        }
    }

    private func isStopping() -> Bool { false }
}
