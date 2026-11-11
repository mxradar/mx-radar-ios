import SwiftUI

/// 主界面：登录 / 连接 / 设置（对应 Android 的 Compose UI，简化版）。
struct ContentView: View {
    @State private var username = ""
    @State private var password = ""
    @State private var server = ServerConfig.baseURL
    @State private var busy = false
    @State private var message = ""
    @State private var connected = false
    @State private var feedToken = ""

    var body: some View {
        NavigationView {
            VStack(spacing: 16) {
                Text("MX").font(.largeTitle).bold()

                if !connected {
                    loginForm
                } else {
                    homeView
                }

                if !message.isEmpty {
                    Text(message).font(.footnote).foregroundColor(.orange)
                }
                Spacer()
            }
            .padding()
            .navigationTitle("MX 雷达")
        }
    }

    private var loginForm: some View {
        VStack(spacing: 12) {
            TextField("账号", text: $username)
                .textFieldStyle(.roundedBorder)
                .autocapitalization(.none)
            SecureField("密码", text: $password)
                .textFieldStyle(.roundedBorder)
            TextField("服务器地址", text: $server)
                .textFieldStyle(.roundedBorder)
                .autocapitalization(.none)
            Button(busy ? "登录中…" : "登录并连接") {
                doLogin()
            }
            .buttonStyle(.borderedProminent)
            .disabled(busy || username.isEmpty || password.isEmpty)
        }
    }

    private var homeView: some View {
        VStack(spacing: 12) {
            Text("已登录：\(username)").bold()
            Text("服务器：\(server)").foregroundColor(.secondary)
            Button("断开连接") {
                TunnelManager.shared.stop()
                connected = false
            }
            .buttonStyle(.bordered)
        }
    }

    private func doLogin() {
        busy = true
        message = ""
        let ud = UserDefaults(suiteName: AppGroup.id)
        ud?.set(server, forKey: AppGroup.kServer)

        ApiClient.login(username: username, password: password) { result in
            DispatchQueue.main.async {
                switch result {
                case .success(let token):
                    feedToken = token
                    startTunnel(token)
                case .failure(let err):
                    busy = false
                    message = "登录失败：\(err.localizedDescription)"
                }
            }
        }
    }

    private func startTunnel(_ token: String) {
        TunnelManager.shared.start(account: username, feedToken: token) { err in
            DispatchQueue.main.async {
                busy = false
                if let err {
                    message = "连接失败：\(err.localizedDescription)"
                } else {
                    connected = true
                    message = "已连接"
                }
            }
        }
    }
}
