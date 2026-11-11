import Foundation
import NetworkExtension

/// VPN 管理器（主 App 用）：登录后把账号/token 写入共享，再启动隧道。
final class TunnelManager {
    static let shared = TunnelManager()

    /// 扩展的 bundle id（Xcode 里 Network Extension target 的 bundle id）
    private let providerBundleId = "com.mx.radar.app.PacketTunnel"

    func start(account: String, feedToken: String,
               completion: @escaping (Error?) -> Void) {
        let ud = UserDefaults(suiteName: AppGroup.id)
        ud?.set(account, forKey: AppGroup.kAccount)
        ud?.set(feedToken, forKey: AppGroup.kFeedToken)

        NETunnelProviderManager.loadAllFromPreferences { managers, _ in
            let m = managers?.first ?? NETunnelProviderManager()
            let proto = NETunnelProviderProtocol()
            proto.providerBundleIdentifier = self.providerBundleId
            proto.serverAddress = ServerConfig.host
            m.protocolConfiguration = proto
            m.localizedDescription = "MX 雷达"
            m.isEnabled = true

            m.saveToPreferences { err in
                if let err { completion(err); return }
                m.loadFromPreferences { err in
                    if let err { completion(err); return }
                    do {
                        try m.connection.startVPNTunnel()
                        completion(nil)
                    } catch {
                        completion(error)
                    }
                }
            }
        }
    }

    func stop() {
        NETunnelProviderManager.loadAllFromPreferences { managers, _ in
            managers?.forEach { $0.connection.stopVPNTunnel() }
        }
    }

    func isRunning(_ completion: @escaping (Bool) -> Void) {
        NETunnelProviderManager.loadAllFromPreferences { managers, _ in
            completion(managers?.first?.connection.status == .connected)
        }
    }
}
