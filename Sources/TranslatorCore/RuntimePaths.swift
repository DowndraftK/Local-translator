import Foundation
import CryptoKit
import Darwin

public struct ComponentCatalog: Codable, Sendable {
    public var schema: Int
    public var components: [RuntimeComponent]
}

public struct RuntimeComponent: Codable, Sendable, Equatable {
    public var id: String
    public var version: String
    public var platform: String
    public var archiveSHA256: String
    public var archiveBytes: UInt64
    public var installedBytes: UInt64
    public var manifestSHA256: String
    public var downloadURL: String?
    public var allowedDownloadHosts: [String]
    public var archiveRoot: String
    public var executable: String
    public var selfcheck: String?
    public var licenseStatus: String
}

public struct RuntimeActivation: Codable, Sendable {
    public var schema: Int
    public var components: [String: String]
}

public enum RuntimePaths {
    public static var environmentRoot: URL {
        let args = ProcessInfo.processInfo.arguments
        if let index = args.firstIndex(of: "--environment-root"), index + 1 < args.count {
            return URL(fileURLWithPath: args[index + 1], isDirectory: true)
        }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("LocalTranslator/Environments", isDirectory: true)
    }

    public static func digest(_ url: URL) throws -> String {
        let file = try FileHandle(forReadingFrom: url)
        defer { try? file.close() }
        var hash = SHA256()
        // Foundation's read bridges through autoreleased NSData. A detached
        // verification task has no run-loop pool between files; without this
        // scope an entire runtime's worth of blocks can remain resident.
        while try autoreleasepool(invoking: { () throws -> Bool in
            try Task.checkCancellation()
            guard let block = try file.read(upToCount: 1024 * 1024), !block.isEmpty else { return false }
            hash.update(data: block)
            return true
        }) {}
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }

    public static func archivedAudio(in session: URL, recordedPath: String, expectedSHA256: String?) throws -> URL {
        // A copied task keeps its historical snapshot unchanged. Playback reads
        // the archive beside the selected database, never the old absolute path.
        let audio = session.appendingPathComponent("audio.wav")
        guard try contained(audio, in: session), FileManager.default.isReadableFile(atPath: audio.path) else {
            throw M0Error.unavailable("此任务目录缺少可读的归档录音；已保存字幕仍可查看和导出。")
        }
        if URL(fileURLWithPath: recordedPath).standardizedFileURL != audio.standardizedFileURL,
           let expectedSHA256 {
            guard try digest(audio) == expectedSHA256 else {
                throw M0Error.invalid("搬动后的归档录音与已保存校验值不符；保留字幕并拒绝回放。")
            }
        }
        // Older/in-progress snapshots can have no full-file hash. Reading their
        // local archive does not authorize ASR resume or bypass its checkpoints.
        return audio
    }

    public static func child(_ relative: String, in root: URL) throws -> URL {
        guard !relative.isEmpty, !relative.hasPrefix("/"), !relative.contains("\\"),
              !relative.split(separator: "/", omittingEmptySubsequences: false).contains(where: { $0 == ".." || $0 == "." || $0.isEmpty }) else {
            throw M0Error.invalid("安装包包含不安全路径。")
        }
        let target = root.appendingPathComponent(relative).standardizedFileURL
        guard try contained(target, in: root) else { throw M0Error.invalid("安装路径越出本应用目录。") }
        return target
    }

    private static func canonical(_ url: URL) throws -> String {
        var current = url; var tail: [String] = []
        while true {
            if let pointer = realpath(current.path, nil) {
                let value = String(cString: pointer); free(pointer)
                return value + (tail.isEmpty ? "" : "/" + tail.reversed().joined(separator: "/"))
            }
            if (try? current.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true {
                throw M0Error.invalid("安装路径含无法解析的链接。")
            }
            guard current.path != "/", !current.lastPathComponent.isEmpty else { throw M0Error.invalid("无法解析安装目录。") }
            tail.append(current.lastPathComponent); current.deleteLastPathComponent()
        }
    }
    public static func contained(_ target: URL, in root: URL) throws -> Bool {
        try canonical(target).hasPrefix(canonical(root) + "/")
    }

    public static func active(_ component: RuntimeComponent, root: URL = environmentRoot) throws -> URL {
        let activation = try JSONDecoder().decode(RuntimeActivation.self, from: Data(contentsOf: root.appendingPathComponent("active.json")))
        guard activation.schema == 1, let relative = activation.components[component.id], relative.hasPrefix("versions/\(component.id)/") else {
            throw M0Error.unavailable("尚未准备\(component.id == "speech-runtime" ? "语音运行环境" : "翻译服务")。")
        }
        let folder = try child(relative, in: root)
        let manifest = component.id == "ollama-engine" ? "Contents/Info.plist" : "component.json"
        guard try digest(folder.appendingPathComponent(manifest)) == component.manifestSHA256,
              FileManager.default.isExecutableFile(atPath: try child(component.executable, in: folder).path) else {
            throw M0Error.unavailable("运行环境缺失或已损坏，请在本机资源中修复。")
        }
        return folder
    }

    public static func bundledCatalog() throws -> ComponentCatalog {
        guard let path = Bundle.main.resourceURL?.appendingPathComponent("component-catalog.json") else {
            throw M0Error.unavailable("未找到本版本可信组件清单。")
        }
        let catalog = try JSONDecoder().decode(ComponentCatalog.self, from: Data(contentsOf: path))
        guard catalog.schema == 1 else { throw M0Error.invalid("组件清单版本不兼容。") }
        return catalog
    }

    public static func speech() throws -> URL {
        guard let component = try bundledCatalog().components.first(where: { $0.id == "speech-runtime" }) else {
            throw M0Error.unavailable("本候选尚未提供受信任语音包，请使用最终整合版。")
        }
        return try active(component)
    }
}
