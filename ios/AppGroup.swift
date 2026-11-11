import Foundation

/// App Group + 服务器配置常量。
/// 主 App 和 Network Extension 共享同一个 App Group，用于传账号/token 和读解析器输出。
enum AppGroup {
    /// App Group ID（Xcode 里主 App 和 Extension 都要勾选同一个）
    static let id = "group.com.mx.radar"

    /// 共享的 UserDefaults 键
    static let kAccount = "account"
    static let kFeedToken = "feed_token"
    static let kToken = "token"
    static let kServer = "server"
}

/// 服务器配置。
enum ServerConfig {
    /// 默认服务器地址（可被主 App 设置覆盖）
    static let host = "116.62.123.130"
    static let port = 8000
    static var baseURL: String {
        let ud = UserDefaults(suiteName: AppGroup.id)
        if let s = ud?.string(forKey: AppGroup.kServer), !s.isEmpty { return s }
        return "http://\(host):\(port)"
    }
}
