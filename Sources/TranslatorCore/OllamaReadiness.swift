import Foundation
import Darwin

public enum OllamaReadiness: Sendable, Equatable {
    case ready(String), notRunning, notInstalled, incompatible(String), portOccupied
    public var message: String {
        switch self {
        case .ready(let version): return "已连接本机翻译服务 \(version)"
        case .notRunning: return "翻译程序已安装，服务尚未运行"
        case .notInstalled: return "未找到兼容的翻译程序，可按需准备官方引擎"
        case .incompatible(let reason): return "现有服务不兼容：\(reason)；请保留原服务，手动处理后重试"
        case .portOccupied: return "11434 端口已被占用，未确认是兼容 Ollama；不会停止占用程序"
        }
    }
    public static func portAvailable(_ port: UInt16 = 11434) -> Bool {
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { return false }; defer { close(descriptor) }
        var address = sockaddr_in(); address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET); address.sin_port = port.bigEndian
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        return withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0 }
        }
    }
    public static func assess(version: String?, tagsValid: Bool, portAvailable: Bool, executablePresent: Bool) -> Self {
        if let version { return version == "0.35.0" && tagsValid ? .ready(version) : .incompatible("固定组合需要 0.35.0 及本地模型 API，检测到 \(version)") }
        if !portAvailable { return .portOccupied }
        return executablePresent ? .notRunning : .notInstalled
    }
}

extension OllamaEngine {
    public func version() async throws -> String {
        // A separate ephemeral no-proxy, no-redirect session with a short timeout.
        let config = URLSessionConfiguration.ephemeral; config.connectionProxyDictionary = [:]
        config.timeoutIntervalForRequest = 3; config.timeoutIntervalForResource = 3; config.urlCache = nil
        let delegate = OllamaVersionNoRedirects()
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let (data, response) = try await session.data(from: baseURL.appendingPathComponent("api/version"))
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw M0Error.unavailable("无法确认服务版本。") }
        struct Version: Decodable { let version: String }
        return try JSONDecoder().decode(Version.self, from: data).version
    }
}
private final class OllamaVersionNoRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
