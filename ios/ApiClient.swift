import Foundation

/// 登录响应（只取需要的字段）。
struct LoginResponse: Decodable {
    let ok: Bool
    let feed_token: String
    let error: String?
}

/// 服务器 API 客户端：登录。
enum ApiClient {
    /// POST /api/login，body = {"username":..., "password":...}
    /// 返回 feed_token（用于 WebSocket 上传）。
    static func login(username: String, password: String,
                      completion: @escaping (Result<String, Error>) -> Void) {
        let url = URL(string: "\(ServerConfig.baseURL)/api/login")!
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: String] = ["username": username, "password": password]
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)

        URLSession.shared.dataTask(with: req) { data, _, err in
            if let err { completion(.failure(err)); return }
            guard let data else {
                completion(.failure(NSError(domain: "api", code: -1, userInfo: [NSLocalizedDescriptionKey: "无响应"])))
                return
            }
            do {
                let r = try JSONDecoder().decode(LoginResponse.self, from: data)
                if r.ok {
                    completion(.success(r.feed_token))
                } else {
                    completion(.failure(NSError(domain: "api", code: -1,
                        userInfo: [NSLocalizedDescriptionKey: r.error ?? "登录失败"])))
                }
            } catch {
                completion(.failure(error))
            }
        }.resume()
    }
}
