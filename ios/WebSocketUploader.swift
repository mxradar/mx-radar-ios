import Foundation

/// 雷达状态上传器：gzip 后通过 WebSocket 二进制帧发到服务器 /ws/ingest。
/// 对应 Android 端 Java 的 StateUploader。
final class WebSocketUploader {
    private let account: String
    private let token: String
    private var task: URLSessionWebSocketTask?
    private let queue = DispatchQueue(label: "ws.upload")

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

    /// 发送一帧 state（自动 gzip）。
    func send(_ state: Data) {
        guard let gz = gzip(state) else { return }
        task?.send(.data(gz)) { [weak self] err in
            if err != nil {
                // 连接断了：重连（简单重试）
                self?.connect()
            }
        }
    }

    func stop() {
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    /// 用 zlib 做 gzip 压缩（Foundation 没有内置，用 zlib）。
    private func gzip(_ data: Data) -> Data? {
        data.withUnsafeBytes { (buf: UnsafeRawBufferPointer) -> Data? in
            guard let base = buf.baseAddress?.assumingMemoryBound(to: UInt8.self) else { return nil }
            var stream = z_stream()
            stream.next_in = UnsafeMutablePointer(mutating: base)
            stream.avail_in = uInt(data.count)

            if deflateInit2_(&stream, Z_DEFAULT_COMPRESSION, Z_DEFLATED, 15 + 16, 8,
                             Z_DEFAULT_STRATEGY, ZLIB_VERSION, Int32(MemoryLayout<z_stream>.size)) != Z_OK {
                return nil
            }
            defer { deflateEnd(&stream) }

            var out = Data()
            let chunk = 64 * 1024
            var buffer = [UInt8](repeating: 0, count: chunk)
            repeat {
                stream.next_out = &buffer
                stream.avail_out = uInt(chunk)
                let r = deflate(&stream, Z_FINISH)
                let written = chunk - Int(stream.avail_out)
                out.append(buffer, count: written)
                if r == Z_STREAM_END { break }
                if r != Z_OK { return nil }
            } while stream.avail_out == 0

            return out
        }
    }
}
