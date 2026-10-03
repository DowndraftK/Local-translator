import Foundation
import Darwin
import ZIPFoundation

public struct RuntimeFile: Codable, Sendable {
    public var size: UInt64?
    public var sha256: String?
    public var link: String?
}
public struct RuntimeManifest: Codable, Sendable {
    public var schema: Int
    public var id: String
    public var version: String
    public var platform: String
    public var files: [String: RuntimeFile]
}

private final class ComponentDownload: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    let hosts: Set<String>
    let byteLimit: UInt64
    let report: @Sendable (Double, String) -> Void
    private let progressLock = NSLock()
    private var lastReport: TimeInterval = 0
    init(hosts: [String], byteLimit: UInt64, report: @escaping @Sendable (Double, String) -> Void) {
        self.hosts = Set(hosts); self.byteLimit = byteLimit; self.report = report
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        let url = request.url
        completionHandler(url?.scheme == "https" && hosts.contains(url?.host ?? "") && url?.user == nil && url?.password == nil ? request : nil)
    }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
                    totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        if totalBytesWritten < 0 || UInt64(totalBytesWritten) > byteLimit { downloadTask.cancel(); return }
        progressLock.lock(); defer { progressLock.unlock() }
        let now = Date().timeIntervalSinceReferenceDate
        guard now - lastReport >= 0.25 || totalBytesWritten == totalBytesExpectedToWrite else { return }; lastReport = now
        report(totalBytesExpectedToWrite > 0 ? Double(totalBytesWritten) / Double(totalBytesExpectedToWrite) * 0.4 : 0,
               "正在下载：\(ByteCountFormatter.string(fromByteCount: totalBytesWritten, countStyle: .file))")
    }
    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {}
}

public enum RuntimeInstaller {
    public static func rollback(_ catalog: ComponentCatalog, root: URL = RuntimePaths.environmentRoot,
                                busy: @escaping @MainActor @Sendable () -> Bool) async throws {
        guard !(await busy()) else { throw M0Error.unavailable("任务正在处理，暂不回退环境。") }
        let lock = open(root.appendingPathComponent(".install.lock").path, O_RDWR | O_NOFOLLOW)
        guard lock >= 0 else { throw M0Error.unavailable("没有可回退的已安装环境。") }
        defer { flock(lock, LOCK_UN); close(lock) }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else { throw M0Error.unavailable("运行环境正在准备，请稍后回退。") }
        let previousURL = root.appendingPathComponent("previous.json")
        let previous = try Data(contentsOf: previousURL)
        let activation = try JSONDecoder().decode(RuntimeActivation.self, from: previous)
        guard activation.schema == 1, !activation.components.isEmpty else { throw M0Error.invalid("上一份环境索引不兼容。") }
        let verification = Task.detached {
            for (id, relative) in activation.components {
                guard let component = catalog.components.first(where: { $0.id == id }), relative.hasPrefix("versions/\(id)/") else { throw M0Error.invalid("上一环境不属于当前可信清单，保留原索引。") }
                let folder = try RuntimePaths.child(relative, in: root)
                try validate(component, at: folder)
            }
        }
        try await withTaskCancellationHandler(operation: { try await verification.value }, onCancel: { verification.cancel() })
        try Task.checkCancellation()
        try await MainActor.run {
            guard !busy(), !Task.isCancelled else { throw M0Error.unavailable("任务已开始，暂不回退环境。") }
            let usage = open(root.appendingPathComponent(".usage.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
            guard usage >= 0 else { throw M0Error.unavailable("无法取得环境使用锁。") }
            defer { flock(usage, LOCK_UN); close(usage) }
            guard flock(usage, LOCK_EX | LOCK_NB) == 0 else { throw M0Error.unavailable("另一实例正在使用环境，暂不回退。") }
            let activeURL = root.appendingPathComponent("active.json")
            let current = try Data(contentsOf: activeURL)
            try previous.write(to: activeURL, options: .atomic)
            try current.write(to: previousURL, options: .atomic)
        }
    }
    public static func validate(_ component: RuntimeComponent, at root: URL) throws {
        guard let physical = realpath(root.path, nil) else { throw M0Error.invalid("组件目录不存在。") }
        let physicalRoot = String(cString: physical); free(physical)
        if component.id == "ollama-engine" {
            guard component.version == "0.35.0", component.licenseStatus == "reviewed",
                  try RuntimePaths.digest(root.appendingPathComponent("Contents/Info.plist")) == component.manifestSHA256 else { throw M0Error.invalid("官方引擎版本不配。") }
            let process = Process(); process.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
            process.arguments = ["--verify", "--deep", "--strict", root.path]
            process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
            try process.run(); process.waitUntilExit()
            guard process.terminationStatus == 0, FileManager.default.isExecutableFile(atPath: try RuntimePaths.child(component.executable, in: root).path) else {
                throw M0Error.invalid("官方引擎签名或可执行文件校验失败。")
            }
            return
        }
        guard component.platform == "macos-arm64", ["reviewed", "reviewed-personal-use-only"].contains(component.licenseStatus),
              try RuntimePaths.digest(root.appendingPathComponent("component.json")) == component.manifestSHA256 else {
            throw M0Error.invalid("组件身份、架构或清单校验失败。")
        }
        let manifest = try JSONDecoder().decode(RuntimeManifest.self, from: Data(contentsOf: root.appendingPathComponent("component.json")))
        guard manifest.schema == 1, manifest.id == component.id, manifest.version == component.version,
              manifest.platform == component.platform, !manifest.files.isEmpty else { throw M0Error.invalid("组件内容清单不配。") }
        var observed = Set<String>(); var bytes: UInt64 = 0
        guard let iterator = FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey]) else {
            throw M0Error.invalid("无法读取组件文件。")
        }
        for case let url as URL in iterator {
            try Task.checkCancellation()
            guard let parent = realpath(url.deletingLastPathComponent().path, nil) else { throw M0Error.invalid("组件文件目录无法读取。") }
            let physicalFile = String(cString: parent) + "/" + url.lastPathComponent; free(parent)
            guard physicalFile.hasPrefix(physicalRoot + "/") else { throw M0Error.invalid("组件路径逃逸。") }
            let relative = String(physicalFile.dropFirst(physicalRoot.count + 1))
            let values = try url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
            if values.isDirectory == true && values.isSymbolicLink != true { continue }
            if relative == "component.json" { continue }
            guard let expected = manifest.files[relative], observed.insert(relative).inserted else {
                throw M0Error.invalid("组件含未登记文件：\(relative)")
            }
            _ = try RuntimePaths.child(relative, in: root)
            if let link = expected.link {
                guard values.isSymbolicLink == true, !link.hasPrefix("/"),
                      try FileManager.default.destinationOfSymbolicLink(atPath: url.path) == link else { throw M0Error.invalid("组件链接不配。") }
            } else {
                guard values.isSymbolicLink != true, let size = expected.size, let hash = expected.sha256,
                      (try url.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(UInt64.init) == size,
                      try RuntimePaths.digest(url) == hash else { throw M0Error.invalid("组件文件校验失败：\(relative)") }
                guard bytes <= UInt64.max - size else { throw M0Error.invalid("组件大小溢出。") }; bytes += size
            }
        }
        guard observed == Set(manifest.files.keys), bytes <= component.installedBytes,
              FileManager.default.isExecutableFile(atPath: try RuntimePaths.child(component.executable, in: root).path) else {
            throw M0Error.invalid("组件缺少文件或大小不配。")
        }
    }

    public static func unpack(_ archiveURL: URL, component: RuntimeComponent, to staging: URL,
                              report: @Sendable (Double, String) -> Void = { _, _ in }) throws -> URL {
        guard component.archiveBytes == UInt64(try archiveURL.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0),
              try RuntimePaths.digest(archiveURL) == component.archiveSHA256 else { throw M0Error.invalid("安装包大小或 SHA-256 不配，未安装。") }
        let archive = try Archive(url: archiveURL, accessMode: .read)
        let entries = Array(archive)
        var names = Set<String>(); var sum: UInt64 = 0
        var links = Set<String>()
        for entry in entries {
            try Task.checkCancellation()
            let name = entry.path.hasSuffix("/") ? String(entry.path.dropLast()) : entry.path
            _ = try RuntimePaths.child(name, in: staging)
            guard name == component.archiveRoot || name.hasPrefix(component.archiveRoot + "/"), names.insert(name).inserted,
                  UInt64(entry.uncompressedSize) <= component.installedBytes,
                  sum <= component.installedBytes - UInt64(entry.uncompressedSize) else {
                throw M0Error.invalid("安装包路径、重复项或解压大小不合法。")
            }
            sum += UInt64(entry.uncompressedSize)
            if entry.type == .symlink { links.insert(name) }
        }
        // No archive member may be below a link, regardless of archive order.
        for name in names { for link in links where name.hasPrefix(link + "/") { throw M0Error.invalid("安装包通过链接写入其他目录。") } }
        var lastReport: TimeInterval = 0
        for (index, entry) in entries.enumerated() where entry.type != .symlink {
            try Task.checkCancellation()
            let name = entry.path.hasSuffix("/") ? String(entry.path.dropLast()) : entry.path
            let progress = Progress(totalUnitCount: Int64(entry.uncompressedSize))
            try withTaskCancellationHandlerSync(progress) {
                let crc = try archive.extract(entry, to: RuntimePaths.child(name, in: staging), progress: progress)
                guard crc == entry.checksum else { throw M0Error.invalid("安装包解压校验失败。") }
            }
            let now = Date().timeIntervalSinceReferenceDate
            if now - lastReport >= 0.25 { lastReport = now; report(0.45 + Double(index + 1) / Double(max(1, entries.count)) * 0.25, "正在解压语音运行环境…") }
        }
        for entry in entries where entry.type == .symlink {
            var data = Data()
            guard entry.uncompressedSize < 4096 else { throw M0Error.invalid("安装包链接过长。") }
            let crc = try archive.extract(entry) { data.append($0) }
            let destination = try RuntimePaths.child(entry.path, in: staging)
            guard crc == entry.checksum, let link = String(data: data, encoding: .utf8), !link.isEmpty, !link.hasPrefix("/"), !link.contains("\\") else {
                throw M0Error.invalid("安装包链接不合法。")
            }
            let resolved = destination.deletingLastPathComponent().appendingPathComponent(link).standardizedFileURL.resolvingSymlinksInPath()
            let runtime = staging.appendingPathComponent(component.archiveRoot)
            guard try RuntimePaths.contained(resolved, in: runtime) else { throw M0Error.invalid("安装包链接逃逸。") }
            try FileManager.default.createSymbolicLink(atPath: destination.path, withDestinationPath: link)
        }
        let runtime = try RuntimePaths.child(component.archiveRoot, in: staging)
        try validate(component, at: runtime)
        return runtime
    }

    private static func withTaskCancellationHandlerSync(_ progress: Progress, body: () throws -> Void) throws {
        // ZIPFoundation reads bounded chunks. Between entries and in file hashing
        // the detached installation task observes cancellation; no activation follows it.
        try Task.checkCancellation(); try body(); try Task.checkCancellation()
    }

    private static func selfcheck(_ component: RuntimeComponent, root: URL) throws {
        guard component.selfcheck != nil || component.id == "ollama-engine" else { return }
        let process = Process(); let pipe = Pipe()
        process.executableURL = try RuntimePaths.child(component.executable, in: root)
        process.arguments = component.id == "ollama-engine" ? ["--version"] : ["-I", "-B", try RuntimePaths.child(component.selfcheck!, in: root).path]
        process.currentDirectoryURL = root
        process.environment = ["PATH":"/usr/bin:/bin:/usr/sbin:/sbin", "HOME":NSHomeDirectory(), "HF_HUB_OFFLINE":"1",
                               "TRANSFORMERS_OFFLINE":"1", "PYTHONDONTWRITEBYTECODE":"1", "PYTORCH_ENABLE_MPS_FALLBACK":"0"]
        process.standardOutput = pipe; process.standardError = pipe
        var output = Data(); let lock = NSLock()
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let chunk = handle.availableData; lock.lock(); if output.count < 64_000 { output.append(chunk.prefix(64_000 - output.count)) }; lock.unlock()
        }
        defer { pipe.fileHandleForReading.readabilityHandler = nil; try? pipe.fileHandleForReading.close() }
        try process.run()
        let deadline = Date().addingTimeInterval(120)
        while process.isRunning {
            if Task.isCancelled || Date() >= deadline {
                process.terminate()
                let stopDeadline = Date().addingTimeInterval(2)
                while process.isRunning && Date() < stopDeadline { Thread.sleep(forTimeInterval: 0.05) }
                if process.isRunning { kill(process.processIdentifier, SIGKILL) }
                process.waitUntilExit()
                if Task.isCancelled { throw CancellationError() }
                throw M0Error.unavailable("固定环境启动自检超时，旧环境保持。")
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        guard process.terminationStatus == 0 else { throw M0Error.unavailable("固定环境自检失败，旧环境保持。\n" + String(decoding: output.suffix(2000), as: UTF8.self)) }
    }

    public static func install(_ component: RuntimeComponent, from local: URL? = nil, root: URL = RuntimePaths.environmentRoot,
                               busy: @escaping @MainActor @Sendable () -> Bool,
                               report: @escaping @Sendable (Double, String) -> Void = { _, _ in }) async throws {
        #if !arch(arm64)
        throw M0Error.unavailable("此固定环境仅支持 Apple Silicon。")
        #endif
        try Task.checkCancellation()
        guard component.archiveBytes < 8 * 1024 * 1024 * 1024, component.installedBytes < 16 * 1024 * 1024 * 1024,
              !(await busy()), component.licenseStatus == "reviewed" || (local != nil && component.licenseStatus == "reviewed-personal-use-only") else { throw M0Error.unavailable("任务正在处理或组件未获准分发，稍后重试。") }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let descriptor = open(root.appendingPathComponent(".install.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
        guard descriptor >= 0 else { throw M0Error.unavailable("无法写入环境目录。") }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else { close(descriptor); throw M0Error.unavailable("另一个窗口正在准备运行环境。") }
        defer { flock(descriptor, LOCK_UN); close(descriptor) }
        let operation = UUID().uuidString
        // The process-held lock proves no installer is using a recognized old
        // staging folder. An abrupt exit never marks it active.
        let stagingRoot = root.appendingPathComponent("staging")
        for old in (try? FileManager.default.contentsOfDirectory(at: stagingRoot, includingPropertiesForKeys: [.isSymbolicLinkKey])) ?? [] {
            if (try? old.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true { continue }
            if UUID(uuidString: old.lastPathComponent) != nil,
               let marker = try? String(contentsOf: old.appendingPathComponent("operation-owner.txt"), encoding: .utf8), marker == "LocalTranslator-install-v1\n" {
                try FileManager.default.removeItem(at: old)
            }
        }
        let temporary = root.appendingPathComponent("staging/" + operation, isDirectory: true)
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
        try Data("LocalTranslator-install-v1\n".utf8).write(to: temporary.appendingPathComponent("operation-owner.txt"), options: .atomic)
        defer { try? FileManager.default.removeItem(at: temporary) }
        let needed = component.archiveBytes + component.installedBytes + 64 * 1024 * 1024
        let capacity = try root.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey, .volumeAvailableCapacityKey])
        let fallback = try FileManager.default.attributesOfFileSystem(forPath: root.path)[.systemFreeSize] as? NSNumber
        let available = fallback?.int64Value ?? capacity.volumeAvailableCapacity.map(Int64.init) ?? capacity.volumeAvailableCapacityForImportantUsage ?? 0
        guard available >= 0, UInt64(available) >= needed else { throw M0Error.unavailable("准备所需空间不足；旧环境和任务保持。") }
        let archiveURL = temporary.appendingPathComponent("download.zip")
        if let local {
            guard UInt64(try local.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0) == component.archiveBytes else { throw M0Error.invalid("所选运行包大小不配，未复制或激活。") }
            try FileManager.default.copyItem(at: local, to: archiveURL)
        }
        else {
            guard let string = component.downloadURL, let url = URL(string: string), url.scheme == "https",
                  component.allowedDownloadHosts.contains(url.host ?? ""), url.user == nil, url.password == nil else {
                throw M0Error.unavailable("本组件尚未公开托管；可以导入本版本已校验运行包。联网入口未发布，未开始下载。")
            }
            let delegate = ComponentDownload(hosts: component.allowedDownloadHosts, byteLimit: component.archiveBytes, report: report)
            let config = URLSessionConfiguration.ephemeral
            config.timeoutIntervalForRequest = 30; config.timeoutIntervalForResource = 1800; config.urlCache = nil
            let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
            defer { session.invalidateAndCancel() }
            var failure: Error?
            for attempt in 0..<2 {
                do {
                    try Task.checkCancellation()
                    let (download, response) = try await session.download(from: url)
                    guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                          response.url?.scheme == "https", component.allowedDownloadHosts.contains(response.url?.host ?? "") else { throw M0Error.invalid("下载来源或状态不可信。") }
                    try FileManager.default.moveItem(at: download, to: archiveURL); failure = nil; break
                } catch is CancellationError { throw CancellationError() }
                catch { failure = error; if Task.isCancelled { throw CancellationError() }; if attempt == 0 { try await Task.sleep(nanoseconds: 1_000_000_000) } }
            }
            if let failure { throw failure }
        }
        let preparation = Task.detached(priority: .utility) { () throws -> URL in
            report(0.4, "正在校验完整安装包…")
            let payload = try unpack(archiveURL, component: component, to: temporary.appendingPathComponent("unpack"), report: report)
            report(0.75, "正在检查固定版本、GPU 与独立启动…")
            try selfcheck(component, root: payload); try Task.checkCancellation()
            try validate(component, at: payload)
            return payload
        }
        let payload = try await withTaskCancellationHandler(operation: { try await preparation.value }, onCancel: { preparation.cancel() })
        try Task.checkCancellation()
        // The final busy check and atomic switch run on the same actor as task starts.
        try await MainActor.run {
            guard !busy(), !Task.isCancelled else { throw M0Error.unavailable("任务已开始，暂不切换环境；稍后重新准备。") }
            let usage = open(root.appendingPathComponent(".usage.lock").path, O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
            guard usage >= 0 else { throw M0Error.unavailable("无法取得环境使用锁。") }
            defer { flock(usage, LOCK_UN); close(usage) }
            guard flock(usage, LOCK_EX | LOCK_NB) == 0 else { throw M0Error.unavailable("另一应用实例正在使用环境，暂不切换。") }
            let relative = "versions/\(component.id)/\(component.version)-\(operation)"
            let destination = try RuntimePaths.child(relative, in: root)
            try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
            var activation = (try? JSONDecoder().decode(RuntimeActivation.self, from: Data(contentsOf: root.appendingPathComponent("active.json")))) ?? RuntimeActivation(schema: 1, components: [:])
            guard activation.schema == 1 else { throw M0Error.invalid("环境索引格式不兼容，保留原文件。") }
            let previous = root.appendingPathComponent("previous.json")
            if FileManager.default.fileExists(atPath: root.appendingPathComponent("active.json").path) {
                try Data(contentsOf: root.appendingPathComponent("active.json")).write(to: previous, options: .atomic)
            }
            try FileManager.default.moveItem(at: payload, to: destination)
            activation.components[component.id] = relative
            do { try JSONEncoder().encode(activation).write(to: root.appendingPathComponent("active.json"), options: .atomic) }
            catch { try? FileManager.default.removeItem(at: destination); throw error }
            report(1, "运行环境已准备，可开始处理。")
        }
    }
}
