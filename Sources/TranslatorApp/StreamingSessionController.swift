import AppKit
@preconcurrency import AVFoundation
import Foundation
import SwiftUI
import TranslatorCore
import UniformTypeIdentifiers

struct StreamingSegment: Decodable, Identifiable {
    let id: Int
    let revision: Int
    let start: Double
    let end: Double
    let english: String
    let chinese: String?
    let boundary: String
    let translation_state: String
    let translation_error: String?
}

struct StreamingSnapshot: Decodable {
    let session_id: String?
    let state: String?
    let audio_path: String?
    let source_path: String?
    let audio_seconds: Double?
    let received_audio_seconds: Double?
    let processed_audio_seconds: Double?
    let asr_queue_seconds: Double?
    let asr_backlog_seconds: Double?
    let input_kind: String?
    let parent_session_path: String?
    let parent_asr_complete: Bool?
    let pending_english: String?
    let asr_error: String?
    let error: String?
    let asr_complete: Bool?
    let translation_counts: [String: Int]?
    let segment_count: Int?
    let segment_offset: Int?
    let segments: [StreamingSegment]
}

@MainActor final class StreamingSessionController: ObservableObject {
    @Published var snapshot: StreamingSnapshot?
    @Published var folder: URL?
    @Published var busy = false
    @Published var recording = false
    @Published var paused = false
    @Published var pauseInProgress = false
    @Published var recordOnly = false
    @Published var stopping = false
    @Published var status = "导入英语录音，或开始麦克风录音"
    @Published var error: String?
    @Published var useCPU = false
    @Published var paced = true
    @Published var translationEnabled = true
    @Published var playbackRate: Float = 1 { didSet { player?.rate = playbackRate } }
    @Published var isPlaying = false
    private var process: Process?
    private var inputPipe: Pipe?
    private var watcher: Task<Void, Never>?
    private var microphone: MicrophoneCapture?
    private var player: AVAudioPlayer?
    private var playbackWatcher: Task<Void, Never>?
    private var userStopped = false
    private var operationID = UUID()
    private var microphoneSession = false
    private var sleepObservers: [NSObjectProtocol] = []

    init() {
        let center = NSWorkspace.shared.notificationCenter
        sleepObservers.append(center.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.pauseRecording(interrupted: true, reason: "电脑即将休眠，录音已暂停；唤醒后请点击继续录音。") }
        })
        sleepObservers.append(center.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.paused, self.busy else { return }
                self.status = "已唤醒 · 检查麦克风后点击继续录音"
            }
        })
    }

    var library: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("LocalTranslator/Recordings", isDirectory: true)
    }
    var countDescription: String {
        let counts = snapshot?.translation_counts ?? [:]
        return "英文 \(snapshot?.segment_count ?? snapshot?.segments.count ?? 0) 段 · 中文 \(counts["completed"] ?? 0) 段 · 待译 \((counts["pending"] ?? 0) + (counts["running"] ?? 0)) · 失败 \(counts["failed"] ?? 0)"
    }

    func start(input: URL?, resourceRoot: String, model: String, microphone: Bool = false) {
        guard !busy else { return }
        pausePlayback()
        let requestID = UUID(); operationID = requestID
        error = nil; busy = true; userStopped = false; stopping = false; paused = false; pauseInProgress = false
        microphoneSession = microphone
        status = microphone ? "正在请求麦克风权限…" : "正在创建录音任务…"
        Task {
            do {
                if microphone {
                    let granted = await AVCaptureDevice.requestAccess(for: .audio)
                    guard granted else { throw failure("麦克风权限未开启。可在系统设置 → 隐私与安全性 → 麦克风中允许本应用。") }
                }
                guard operationID == requestID, !userStopped else { return }
                try FileManager.default.createDirectory(at: library, withIntermediateDirectories: true)
                let destination = library.appendingPathComponent(UUID().uuidString, isDirectory: true)
                try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: false)
                folder = destination; snapshot = nil; userStopped = false
                let project = URL(fileURLWithPath: resourceRoot).deletingLastPathComponent()
                let config: [String: Any] = [
                    "source": project.appendingPathComponent("artifacts/whisperlivekit-mps-20260915/source").path,
                    "source_manifest": project.appendingPathComponent("artifacts/whisperlivekit-mps-20260915/patched-source.json").path,
                    "model": project.appendingPathComponent("models/whisper-mps-experiment/large-v3-turbo").path,
                    "model_manifest": project.appendingPathComponent("experiments/whisperlivekit/mps-model-manifest.json").path,
                    "device": useCPU ? "cpu" : "mps", "dtype": "float32", "max_context_tokens": 128]
                let configURL = destination.appendingPathComponent("runtime.json")
                try JSONSerialization.data(withJSONObject: config, options: [.prettyPrinted, .sortedKeys]).write(to: configURL, options: .atomic)
                let onlyRecord = microphone && recordOnly
                var args = [onlyRecord ? "record" : "run", "--config", configURL.path, "--session", destination.path,
                            "--translation-model", model]
                if !translationEnabled { args.append("--no-translation") }
                if microphone { if !onlyRecord { args.append("--stdin-pcm") } }
                else if let input { args += ["--input", input.path] }
                else { throw failure("请先选择录音。") }
                if paced && !microphone { args.append("--paced") }
                try launch(args, project: project, microphoneRequested: microphone)
            } catch {
                guard operationID == requestID else { return }
                self.error = error.localizedDescription; busy = false; stopping = false
            }
        }
    }

    private func launch(_ arguments: [String], project: URL, microphoneRequested: Bool = false) throws {
        guard let folder else { return }
        let python = project.appendingPathComponent("artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python")
        let bundledRuntime = Bundle.main.resourceURL?.appendingPathComponent("StreamingRuntime")
        let runtime = bundledRuntime.flatMap { FileManager.default.fileExists(atPath: $0.appendingPathComponent("streaming_translator/__main__.py").path) ? $0 : nil }
            ?? project.appendingPathComponent("runtime")
        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: runtime.appendingPathComponent("streaming_translator/__main__.py").path) else {
            throw failure("未找到流式运行环境。请按项目 runtime/README.md 准备 Python 环境并重新打包应用。")
        }
        let child = Process()
        let launchID = UUID(); operationID = launchID
        child.executableURL = python
        child.arguments = ["-m", "streaming_translator"] + arguments
        child.currentDirectoryURL = project
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = runtime.path
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["NUMBA_CACHE_DIR"] = folder.appendingPathComponent("numba-cache").path
        child.environment = env
        let logURL = folder.appendingPathComponent("worker-\(UUID().uuidString).log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let log = try FileHandle(forWritingTo: logURL)
        child.standardError = log; child.standardOutput = log
        if microphoneRequested { let pipe = Pipe(); child.standardInput = pipe; inputPipe = pipe }
        child.terminationHandler = { [weak self] child in
            try? log.close()
            Task { @MainActor in
                guard let self, self.process === child else { return }
                self.microphone?.stop(); self.microphone = nil; self.recording = false
                self.paused = false; self.pauseInProgress = false; self.microphoneSession = false
                self.inputPipe = nil; self.process = nil; self.busy = false; self.stopping = false
                self.watcher?.cancel(); self.watcher = nil; self.refresh()
                self.status = self.statusDescription()
                if child.terminationStatus != 0 && !self.userStopped {
                    self.error = self.snapshot?.error ?? self.snapshot?.asr_error
                        ?? String(((try? String(contentsOf: logURL, encoding: .utf8)) ?? "工作进程未正常结束。").suffix(1800))
                }
            }
        }
        process = child; busy = true; stopping = false; status = "正在校验资源并加载模型…"
        do { try child.run() }
        catch {
            process = nil; busy = false; try? log.close()
            try? inputPipe?.fileHandleForWriting.close(); inputPipe = nil
            self.error = error.localizedDescription
            throw error
        }
        watcher = Task { @MainActor [self] in
            while !Task.isCancelled && self.operationID == launchID {
                self.refresh()
                self.status = self.statusDescription()
                if microphoneRequested && ["recognizing", "recording"].contains(self.snapshot?.state ?? "") && self.microphone == nil && !self.userStopped && !self.paused {
                    do {
                        guard let handle = self.inputPipe?.fileHandleForWriting else { break }
                        let capture = MicrophoneCapture(handle: handle, journal: folder.appendingPathComponent("capture-events.jsonl"),
                            onError: { [weak self] message in
                                Task { @MainActor in
                                    guard let self, self.operationID == launchID else { return }
                                    self.error = message; self.stop()
                                }
                            }, onInterruption: { [weak self] in
                                Task { @MainActor in
                                    guard let self, self.operationID == launchID else { return }
                                    self.pauseRecording(interrupted: true, reason: "麦克风设备发生变化，录音已暂停。请检查设备并点击继续录音。")
                                }
                            })
                        try capture.start()
                        self.microphone = capture; self.recording = true
                    } catch { self.error = error.localizedDescription; self.stop() }
                }
                do { try await Task.sleep(nanoseconds: 500_000_000) } catch { break }
            }
        }
    }

    func refresh() {
        guard let folder, let data = try? Data(contentsOf: folder.appendingPathComponent("snapshot.json")),
              let snapshot = try? JSONDecoder().decode(StreamingSnapshot.self, from: data) else { return }
        self.snapshot = snapshot
    }
    private func statusDescription() -> String {
        if stopping { return snapshot?.state == "refining" ? "正在停止 · 等待当前校对保存后退出…" : "正在停止输入并保存尾句…" }
        if paused { return pauseInProgress ? "正在保存暂停前的录音…" : "录音已暂停 · 点击继续录音恢复" }
        if !busy, ["preparing", "loading", "recognizing", "recording", "finishing_asr", "translating", "refining"].contains(snapshot?.state ?? "") {
            return "上次任务中断；可继续识别已保存录音或补译已有英文"
        }
        switch snapshot?.state {
        case "preparing": return "正在保存录音并准备资源…"
        case "loading": return "正在加载语音模型…"
        case "refining": return "正在根据完整录音重新校对英文 · 原版字幕保留"
        case "recognizing": return recording ? "正在录音 · 英文先保存，中文随后显示" : "正在识别 · 英文先保存，中文随后显示"
        case "recording": return "仅录音 · 正在保存，稍后可继续识别"
        case "recorded": return "录音已保存 · 点击继续识别生成字幕"
        case "finishing_asr": return "正在提交录音末尾…"
        case "translating": return "英文已保存，正在补齐中文…"
        case "completed": return "本次处理完成"
        case "needs_translation": return "英文已保存；部分中文失败，可继续补译"
        case "incomplete_asr": return "识别尚未完成 · 点击继续识别处理已保存录音"
        case "failed": return "任务未完成；已保存内容仍可查看"
        case "stopped": return "已停止；可继续补译已保存英文"
        default: return status
        }
    }
    func stop() {
        guard busy, !stopping else { return }
        userStopped = true; stopping = true; status = "正在停止输入并保存尾句…"
        if process == nil {
            operationID = UUID(); busy = false; stopping = false; paused = false
            microphoneSession = false; status = "已取消"
            return
        }
        if let microphone {
            // EOF follows the capture queue's final copied buffer. Do not interrupt
            // the reader before those samples have been archived by the worker.
            microphone.stop(); self.microphone = nil; recording = false
            return
        } else if let inputPipe { try? inputPipe.fileHandleForWriting.close() }
        if let process, process.isRunning { process.terminate() }
    }

    func pauseRecording(interrupted: Bool = false, reason: String? = nil) {
        guard busy, microphoneSession, !paused, !stopping else { return }
        paused = true; recording = false
        if let reason { error = reason }
        guard let microphone else { return }
        pauseInProgress = true
        let current = operationID
        microphone.pause(interrupted: interrupted) { [weak self] in
            Task { @MainActor in
                guard let self, self.operationID == current else { return }
                self.pauseInProgress = false
            }
        }
    }

    func resumeRecording() {
        guard busy, paused, !pauseInProgress, !stopping else { return }
        guard AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            error = "麦克风权限已关闭，请在系统设置 → 隐私与安全性 → 麦克风中允许后继续。"; return
        }
        do {
            try microphone?.start()
            paused = false; recording = microphone != nil; error = nil
        } catch {
            self.error = error.localizedDescription
            // Drain failed start state before permitting another resume attempt.
            paused = false
            pauseRecording(interrupted: true)
        }
    }

    func resumeASR(resourceRoot: String) {
        guard !busy, let folder, snapshot?.asr_complete != true else { return }
        error = nil; userStopped = false
        do { try launch(["resume", "--session", folder.path],
                        project: URL(fileURLWithPath: resourceRoot).deletingLastPathComponent()) }
        catch { self.error = error.localizedDescription }
    }

    func showPage(offset: Int?, resourceRoot: String) {
        guard let folder else { return }
        do {
            let request: [String: Any] = ["offset": offset.map { $0 as Any } ?? NSNull()]
            try JSONSerialization.data(withJSONObject: request).write(to: folder.appendingPathComponent("view.json"), options: .atomic)
            if !busy {
                try launch(["page", "--session", folder.path],
                           project: URL(fileURLWithPath: resourceRoot).deletingLastPathComponent())
            }
        } catch { self.error = error.localizedDescription }
    }
    func openSession() {
        guard !busy else { return }
        let panel = NSOpenPanel(); panel.title = "打开录音任务目录"
        panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false
        panel.directoryURL = library
        guard panel.runModal() == .OK, let chosen = panel.url else { return }
        loadSession(chosen)
    }
    func loadSession(_ chosen: URL) {
        guard !busy else { return }
        guard FileManager.default.fileExists(atPath: chosen.appendingPathComponent("session.sqlite").path) else {
            error = "所选目录没有录音任务数据库。"; return
        }
        pausePlayback(); error = nil
        folder = chosen; snapshot = nil; refresh(); status = statusDescription()
    }
    func refine(resourceRoot: String) {
        guard !busy, let original = folder, snapshot?.audio_path != nil else { return }
        let project = URL(fileURLWithPath: resourceRoot).deletingLastPathComponent()
        let configURL = original.appendingPathComponent("runtime.json")
        guard FileManager.default.fileExists(atPath: configURL.path) else {
            error = "此任务缺少运行配置，请使用新版重新处理原录音后再校对。"; return
        }
        do {
            try FileManager.default.createDirectory(at: library, withIntermediateDirectories: true)
            let destination = library.appendingPathComponent(UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: false)
            pausePlayback(); folder = destination; snapshot = nil; error = nil; userStopped = false
            try launch(["refine", "--from-session", original.path, "--session", destination.path,
                        "--config", configURL.path], project: project)
        } catch {
            folder = original; refresh(); self.error = error.localizedDescription
        }
    }
    func retry(resourceRoot: String) {
        guard !busy, let folder else { return }
        userStopped = false; error = nil
        do { try launch(["retry", "--session", folder.path, "--retry-failed"],
                        project: URL(fileURLWithPath: resourceRoot).deletingLastPathComponent()) }
        catch { self.error = error.localizedDescription }
    }
    func revise(_ segment: StreamingSegment, text: String, resourceRoot: String) {
        guard !busy, let folder else { return }
        do {
            let path = folder.appendingPathComponent("revision-\(UUID().uuidString).txt")
            try text.write(to: path, atomically: true, encoding: .utf8)
            try launch(["revise", "--session", folder.path, "--segment", String(segment.id), "--text-file", path.path],
                       project: URL(fileURLWithPath: resourceRoot).deletingLastPathComponent())
        } catch { self.error = error.localizedDescription }
    }
    func play(from seconds: Double) {
        guard let path = snapshot?.audio_path else { return }
        do {
            player = try AVAudioPlayer(contentsOf: URL(fileURLWithPath: path))
            player?.enableRate = true; player?.rate = playbackRate; player?.currentTime = seconds
            player?.play(); isPlaying = true
            playbackWatcher?.cancel()
            playbackWatcher = Task { @MainActor [weak self] in
                while self?.player?.isPlaying == true {
                    do { try await Task.sleep(nanoseconds: 250_000_000) } catch { return }
                }
                self?.isPlaying = false
            }
        } catch { self.error = error.localizedDescription }
    }
    func pausePlayback() { playbackWatcher?.cancel(); playbackWatcher = nil; player?.pause(); isPlaying = false }
    func export(_ kind: String, resourceRoot: String) {
        guard !busy, let folder else { return }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [UTType(filenameExtension: kind) ?? .plainText]
        panel.nameFieldStringValue = "双语字幕"
        guard panel.runModal() == .OK, let output = panel.url else { return }
        do { try launch(["export", "--session", folder.path, "--format", kind, "--output", output.path],
                        project: URL(fileURLWithPath: resourceRoot).deletingLastPathComponent()) }
        catch { self.error = error.localizedDescription }
    }
    private func failure(_ message: String) -> NSError {
        NSError(domain: "StreamingTranslator", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
