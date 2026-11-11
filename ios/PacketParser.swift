import Foundation

/// 一个解析好的 UDP 包。
struct ParsedPacket {
    let up: Bool          // true=上行(客户端→服务器), false=下行(服务器→客户端)
    let local: String     // 本机地址 "ip:port"
    let remote: String    // 远端地址 "ip:port"
    let payload: Data     // UDP 负载（不含 IP/UDP 头）
}

/// 从 NEPacketTunnelProvider 抓到的原始 IP 包中解析出 UDP 五元组 + 负载。
enum PacketParser {
    /// 解析原始 IP 包（IPv4 / IPv6）。
    /// - Parameters:
    ///   - packet: 完整 IP 包（含 IP 头 + UDP 头 + 负载）
    ///   - isOutbound: 是否出站（TUN 读到的 = 出站；从网络 socket 读到的 = 入站）
    static func parse(_ packet: Data, isOutbound: Bool) -> ParsedPacket? {
        guard packet.count >= 20 else { return nil }
        let version = packet[0] >> 4
        switch version {
        case 4: return parseIPv4(packet, isOutbound: isOutbound)
        case 6: return parseIPv6(packet, isOutbound: isOutbound)
        default: return nil
        }
    }

    // MARK: IPv4

    private static func parseIPv4(_ p: Data, isOutbound: Bool) -> ParsedPacket? {
        guard p.count >= 20 else { return nil }
        let ihl = Int(p[0] & 0x0F) * 4
        guard ihl >= 20, p.count >= ihl + 8 else { return nil }
        let proto = p[9]
        guard proto == 17 else { return nil } // 只要 UDP

        let src = p.subdata(in: 12..<16)
        let dst = p.subdata(in: 16..<20)
        let srcPort = Int(readUInt16(p, ihl))
        let dstPort = Int(readUInt16(p, ihl + 2))
        let payload = p.subdata(in: (ihl + 8)..<p.count)

        let srcStr = "\(ip4(src)):\(srcPort)"
        let dstStr = "\(ip4(dst)):\(dstPort)"

        if isOutbound {
            // 出站：客户端 → 服务器。local=src, remote=dst, up=true
            return ParsedPacket(up: true, local: srcStr, remote: dstStr, payload: payload)
        } else {
            // 入站：服务器 → 客户端。local=dst, remote=src, up=false
            return ParsedPacket(up: false, local: dstStr, remote: srcStr, payload: payload)
        }
    }

    // MARK: IPv6

    private static func parseIPv6(_ p: Data, isOutbound: Bool) -> ParsedPacket? {
        guard p.count >= 48 else { return nil }
        let nextHeader = p[6]
        guard nextHeader == 17 else { return nil } // 只要 UDP（无扩展头）

        let src = p.subdata(in: 8..<24)
        let dst = p.subdata(in: 24..<40)
        let srcPort = Int(readUInt16(p, 40))
        let dstPort = Int(readUInt16(p, 42))
        let payload = p.subdata(in: 48..<p.count)

        let srcStr = "[\(ip6(src))]:\(srcPort)"
        let dstStr = "[\(ip6(dst))]:\(dstPort)"

        if isOutbound {
            return ParsedPacket(up: true, local: srcStr, remote: dstStr, payload: payload)
        } else {
            return ParsedPacket(up: false, local: dstStr, remote: srcStr, payload: payload)
        }
    }

    // MARK: helpers

    private static func readUInt16(_ data: Data, _ offset: Int) -> UInt16 {
        data[offset..<(offset + 2)].withUnsafeBytes { $0.loadUnaligned(as: UInt16.self).bigEndian }
    }

    private static func ip4(_ b: Data) -> String {
        b.map { String($0) }.joined(separator: ".")
    }

    private static func ip6(_ b: Data) -> String {
        // 每 2 字节一组，压缩连续的 0 段
        var groups = [UInt16]()
        for i in stride(from: 0, to: 16, by: 2) {
            groups.append(readUInt16(b, i))
        }
        // 简化：直接十六进制拼接（生产可再优化压缩）
        return groups.map { String(format: "%x", $0) }.joined(separator: ":")
    }
}
