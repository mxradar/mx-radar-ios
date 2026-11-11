import Foundation

/// 雷达状态上传器（占位版：先发原始数据，gzip 后续补）。
final class WebSocketUploader {
    private let account: String
    private let token: String
    private var task: URLSessionWebSocketTask?

    init(account: String, token: String) {
        self.account = account
        self.token = token
        connect()
    }

    private func connect() {
        var comps = URLComponents(string: "ws://\(ServerConfig.host):\(ServerConfig.port)/ws/ingest")!
        comps.queryItems = [
            URLQueryItem(name: "account", value: account),
            URLQueryItem(name: "token", value: token),
        ]
        guard let url = comps.url else { return }
        let t = URLSession.shared.webSocketTask(with: url)
        t.resume()
        task = t
    }

    func send(_ state: Data) {
        // TODO: gzip 压缩后再发（先用 Compression 框架或后续补 zlib）
        task?.send(.data(state)) { _ in }
    }

    func stop() {
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }
}
