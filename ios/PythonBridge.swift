import Foundation

/// CPython 嵌入桥接层（占位版）。
/// 阶段 2 交叉编译好 CPython+NumPy 后，这里换成真实的 Python C API 调用。
final class PythonBridge {
    static let shared = PythonBridge()
    private init() {}

    func start(assetsDir: String, outputDir: String) -> Bool {
        // TODO: Py_Initialize + 导入 SeasonRelay / LocalParserBridge
        return false
    }

    func handle(up: Bool, local: String, remote: String, payload: Data, account: String = "") {
        // TODO: SeasonRelay.handle(up, local, remote, payload, account)
    }

    func latestState() -> Data? {
        // TODO: LocalParserBridge.latest_state()
        return nil
    }

    func stop() {
        // TODO: LocalParserBridge.stop()
    }
}
