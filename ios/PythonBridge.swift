import Foundation

/// CPython 嵌入桥接层。
///
/// 职责：初始化 Python、导入并实例化 `LocalParserBridge` + `SeasonRelay`，
/// 提供两个入口给 Swift 调用：
///   - `handle(...)`  把抓到的原始 UDP 包喂给 `SeasonRelay.handle`
///   - `latestState()` 取最新 state.json 字节
///
/// 对应 Android 端 `radar_server/parser_bridge.py` 里做的事，只是把
/// Java 侧的桥接换成了 Swift 侧。
final class PythonBridge {
    static let shared = PythonBridge()

    private var ready = false

    private init() {}

    /// 初始化 Python + 导入解析器。失败返回 false。
    /// - Parameters:
    ///   - assetsDir: `assets/py` 目录（解析器文件所在，含 radar_server / parser_src）
    ///   - outputDir: 输出目录（写 state.json / states.jsonl）
    func start(assetsDir: String, outputDir: String) -> Bool {
        guard !ready else { return true }

        Py_Initialize()

        // 转义单引号，防止注入破坏 Python 脚本字符串
        let esc: (String) -> String = { $0.replacingOccurrences(of: "'", with: "\\'") }

        let script = """
        import sys, os
        _assets = '\(esc(assetsDir))'
        _out = '\(esc(outputDir))'
        os.makedirs(_out, exist_ok=True)
        sys.path[:0] = [
            _assets,
            _assets + '/parser_src',
            _assets + '/parser_src/ErCore/src',
            _assets + '/parser_src/ErCore/src/core',
        ]
        from radar_server.parser_bridge import LocalParserBridge
        from radar_server.seasonrelay import SeasonRelay
        _bridge = LocalParserBridge(_out, _assets, 17891)
        _bridge.start()
        _relay = SeasonRelay(_bridge.emit)
        """

        let ret = script.withCString {
            PyRun_SimpleString(UnsafeMutablePointer(mutating: $0))
        }
        ready = (ret == 0)
        return ready
    }

    /// 喂一个原始 UDP 包给 SeasonRelay。
    /// - Parameters:
    ///   - up: true = 上行（本机 -> 服务器），false = 下行（服务器 -> 本机）
    ///   - local: 本机地址，如 "127.0.0.1:48805"
    ///   - remote: 远端地址，如 "153.3.220.95:24498"
    ///   - payload: UDP 负载（不含 IP/UDP 头）
    ///   - account: 账号（默认 ""）
    func handle(up: Bool, local: String, remote: String, payload: Data, account: String = "") {
        guard ready else { return }
        guard let main = PyImport_AddModule("__main__") else { return }
        guard let relay = PyObject_GetAttrString(main, "_relay") else {
            Py_DECREF(main)
            return
        }

        let upObj = up ? Py_True : Py_False
        let localObj = PyUnicode_FromString(local)
        let remoteObj = PyUnicode_FromString(remote)
        let payloadObj = payload.withUnsafeBytes { (buf: UnsafeRawBufferPointer) -> UnsafeMutablePointer<PyObject>? in
            guard let base = buf.baseAddress else { return nil }
            return PyBytes_FromStringAndSize(base.assumingMemoryBound(to: CChar.self), payload.count)
        }
        let accountObj = PyUnicode_FromString(account)

        guard let pObj = payloadObj,
              let lObj = localObj,
              let rObj = remoteObj,
              let aObj = accountObj else {
            Py_DECREF(relay); Py_DECREF(main)
            return
        }

        let args = PyTuple_Pack(5, upObj, lObj, rObj, pObj, aObj)
        if let args {
            let result = PyObject_Call(relay, args, nil)
            if let result { Py_DECREF(result) }
            Py_DECREF(args)
        }

        Py_DECREF(upObj)
        Py_DECREF(lObj); Py_DECREF(rObj); Py_DECREF(pObj); Py_DECREF(aObj)
        Py_DECREF(relay); Py_DECREF(main)
    }

    /// 取最新 state.json 字节（nil 表示还没有 / 未就绪）。
    /// 对应 `LocalParserBridge.latest_state()`。
    func latestState() -> Data? {
        guard ready else { return nil }
        guard let main = PyImport_AddModule("__main__") else { return nil }
        guard let bridge = PyObject_GetAttrString(main, "_bridge") else {
            Py_DECREF(main)
            return nil
        }

        var data: Data? = nil
        if let state = PyObject_CallMethod(bridge, "latest_state", nil) {
            // state 是 bytes（PyBytes）或 None
            if state != Py_None {
                var ptr: UnsafePointer<CChar>? = nil
                var len: Py_ssize_t = 0
                if PyBytes_AsStringAndSize(state, &ptr, &len) == 0, let p = ptr {
                    data = Data(bytes: p, count: Int(len))
                }
            }
            Py_DECREF(state)
        }
        Py_DECREF(bridge); Py_DECREF(main)
        return data
    }

    /// 停止：可选，进程退出时由 CPython 自己清理。
    func stop() {
        guard ready else { return }
        // LocalParserBridge.stop() 关闭线程 + 事件队列
        if let main = PyImport_AddModule("__main__"),
           let bridge = PyObject_GetAttrString(main, "_bridge") {
            _ = PyObject_CallMethod(bridge, "stop", nil)
            Py_DECREF(bridge); Py_DECREF(main)
        }
        ready = false
    }
}
