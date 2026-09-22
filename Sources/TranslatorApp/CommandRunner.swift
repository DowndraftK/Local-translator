import Foundation
import TranslatorCore

/// Each UI operation owns one worker process. Cancelling it never stops Ollama.
final class CommandRunner: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false

    func cancel() {
        lock.lock(); defer { lock.unlock() }
        cancelled = true
        if let process, process.isRunning { process.terminate() }
    }

    func run(arguments: [String], log: URL) async throws {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                lock.lock(); defer { lock.unlock() }
                guard !cancelled else { continuation.resume(throwing: CancellationError()); return }
                do {
                    let executable = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/translator-m0")
                    guard FileManager.default.isExecutableFile(atPath: executable.path) else {
                        throw M0Error.unavailable("未找到随应用打包的处理程序。请运行 scripts/package-app.sh 重新打包。")
                    }
                    FileManager.default.createFile(atPath: log.path, contents: nil)
                    let handle = try FileHandle(forWritingTo: log)
                    let child = Process()
                    child.executableURL = executable
                    child.arguments = arguments
                    child.standardOutput = handle
                    child.standardError = handle
                    child.terminationHandler = { [weak self] process in
                        try? handle.close()
                        self?.lock.lock()
                        let cancelled = self?.cancelled ?? false
                        self?.process = nil
                        self?.lock.unlock()
                        if cancelled { continuation.resume(throwing: CancellationError()) }
                        else if process.terminationStatus == 0 { continuation.resume() }
                        else {
                            let message = (try? String(contentsOf: log, encoding: .utf8)) ?? "处理程序未正常结束。"
                            continuation.resume(throwing: M0Error.unavailable(String(message.suffix(2400))))
                        }
                    }
                    process = child
                    do { try child.run() }
                    catch { process = nil; try? handle.close(); throw error }
                } catch { continuation.resume(throwing: error) }
            }
        } onCancel: { self.cancel() }
    }
}
