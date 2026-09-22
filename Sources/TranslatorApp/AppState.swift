import AppKit
import Foundation
import SwiftUI
import TranslatorCore
import UniformTypeIdentifiers

enum WorkspacePage: String, CaseIterable, Identifiable {
    case text = "文字翻译", documents = "文档与图片", audio = "录音翻译", settings = "本机资源"
    var id: String { rawValue }
    var symbol: String {
        switch self {
        case .text: return "character.bubble"
        case .documents: return "doc.text.image"
        case .audio: return "waveform"
        case .settings: return "externaldrive.badge.checkmark"
        }
    }
}

@MainActor final class AppState: ObservableObject {
    @Published var page: WorkspacePage = .text
    @Published var model = "hy-mt2:1.8b-q8"
    @Published var direction = "en-zh" { didSet { if oldValue != direction { translation = nil; documentTranslations = [:] } } }
    @Published var source = "" { didSet { if oldValue != source { translation = nil } } }
    @Published var translation: TranslationRecord?
    @Published var activity: String?
    @Published var error: String?
    @Published var notice = "准备就绪"
    @Published var serviceAvailable = false
    @Published var serviceChecked = false
    @Published var installedModels: [String] = []
    @Published var checkingService = false
    @Published var document: ImportedDocument?
    @Published var documentURL: URL?
    @Published var selectedBlock: String?
    @Published var documentTranslations: [String: TranslationRecord] = [:]
    @Published var extractMode = "auto"
    @Published var pageLimit = 10
    @Published var audioURL: URL?
    @Published var speechModel = "turbo"
    @Published var segments: [SpeechSegment] = []
    @Published var speechRun: SpeechRun?
    @Published var lastWorkFolder: URL?
    @Published var resourceRoot: String
    let streaming = StreamingSessionController()
    private var operation: Task<Void, Never>?
    private var serverProcess: Process?
    private var serverLog: FileHandle?

    init() {
        resourceRoot = UserDefaults.standard.string(forKey: "speechResourceRoot")
            ?? Bundle.main.object(forInfoDictionaryKey: "M0ResourceRoot") as? String ?? ""
        if let index = CommandLine.arguments.firstIndex(of: "--open-recording-session"), index + 1 < CommandLine.arguments.count {
            page = .audio
            streaming.loadSession(URL(fileURLWithPath: CommandLine.arguments[index + 1]))
        }
    }
    var busy: Bool { activity != nil }
    var chosenBlock: TextBlock? { document?.blocks.first { $0.id == selectedBlock } }
    var selectedTranslation: TranslationRecord? { selectedBlock.flatMap { documentTranslations[$0] } }
    func sourceLabel(for block: TextBlock) -> String {
        let index = (document?.blocks.firstIndex { $0.id == block.id } ?? 0) + 1
        let location = block.source.page.map { "第 \($0) 页 · 第 \(index) 段" } ?? "第 \(index) 段"
        return block.kind == "table-paragraph" ? location + " · 表格文字" : location
    }
    var models: [String] {
        Array(Set(["hy-mt2:1.8b-q8", "hy-mt2:7b-q8"] + installedModels)).sorted()
    }
    var modelLabel: String { model.contains("1.8b") ? "HY-MT2 · 1.8B" : model.contains("7b") ? "HY-MT2 · 7B" : model }
    var modelDirectory: URL {
        URL(fileURLWithPath: resourceRoot).appendingPathComponent("whisper-coreml")
            .appendingPathComponent(speechModel == "turbo" ? "openai_whisper-large-v3-v20240930_turbo" : "openai_whisper-small.en")
    }
    var tokenizerDirectory: URL {
        URL(fileURLWithPath: resourceRoot).appendingPathComponent(speechModel == "turbo" ? "whisper-tokenizer" : "whisper-tokenizer-small.en")
    }
    var speechFilesPresent: Bool {
        !resourceRoot.isEmpty && FileManager.default.fileExists(atPath: modelDirectory.appendingPathComponent("AudioEncoder.mlmodelc").path)
            && FileManager.default.fileExists(atPath: tokenizerDirectory.appendingPathComponent("tokenizer.json").path)
    }

    func checkService() async {
        guard !checkingService else { return }
        checkingService = true
        defer { checkingService = false; serviceChecked = true }
        do {
            let models = try await OllamaEngine().models()
            installedModels = models.filter { $0.remote_host == nil && $0.remote_model == nil && !$0.name.lowercased().contains("cloud") }.map(\.name)
            serviceAvailable = true
        } catch { serviceAvailable = false; installedModels = [] }
    }

    func startLocalService() {
        guard !checkingService, !serviceAvailable else { return }
        Task {
            // Check again before launching: never replace an existing server.
            await checkService()
            guard !serviceAvailable else { return }
            do {
                let paths = ["/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"]
                guard let path = paths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
                    throw M0Error.unavailable("未找到 Ollama。请先安装或手动启动本机 Ollama。")
                }
                let folder = try makeWorkFolder()
                let log = folder.appendingPathComponent("ollama.log")
                FileManager.default.createFile(atPath: log.path, contents: nil)
                let handle = try FileHandle(forWritingTo: log)
                let child = Process()
                child.executableURL = URL(fileURLWithPath: path)
                child.arguments = ["serve"]
                var environment = ProcessInfo.processInfo.environment
                environment["OLLAMA_HOST"] = "127.0.0.1:11434"
                environment["OLLAMA_NO_CLOUD"] = "1"
                environment["OLLAMA_NOPRUNE"] = "1"
                environment["OLLAMA_MAX_LOADED_MODELS"] = "1"
                child.environment = environment
                child.standardOutput = handle; child.standardError = handle
                try child.run()
                serverProcess = child; serverLog = handle
                checkingService = true
                for _ in 0..<20 {
                    try await Task.sleep(nanoseconds: 250_000_000)
                    if let models = try? await OllamaEngine().models() {
                        installedModels = models.filter { $0.remote_host == nil && $0.remote_model == nil && !$0.name.lowercased().contains("cloud") }.map(\.name)
                        serviceAvailable = true; break
                    }
                    if !child.isRunning { break }
                }
                checkingService = false; serviceChecked = true
                if !serviceAvailable { throw M0Error.unavailable("本地服务启动失败。请打开本次测试目录查看 ollama.log。") }
                notice = "本地服务已启动，云功能已关闭"
            } catch { checkingService = false; self.error = error.localizedDescription }
        }
    }

    func shutdown() {
        streaming.stop()
        streaming.pausePlayback()
        operation?.cancel()
        if let process = serverProcess, process.isRunning { process.terminate() }
        try? serverLog?.close()
    }
    func cancel() { operation?.cancel(); notice = "正在停止，已完成的录音片段仍可导出" }

    private func makeWorkFolder() throws -> URL {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("LocalTranslator-M0", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        lastWorkFolder = folder
        return folder
    }
    private func perform(_ title: String, operation body: @escaping @MainActor (URL) async throws -> Void) {
        guard !busy else { return }
        activity = title; error = nil; notice = title
        operation = Task {
            defer { self.activity = nil; self.operation = nil }
            do {
                let folder = try makeWorkFolder()
                try await body(folder)
                try Task.checkCancellation()
                notice = "处理完成"
            } catch is CancellationError { notice = "已停止；可保留已显示的结果" }
            catch { self.error = error.localizedDescription; notice = "处理未完成" }
        }
    }

    func translateText() {
        let input = source, selectedModel = model, selectedDirection = direction
        translation = nil
        perform("正在翻译…") { folder in
            self.translation = try await self.translate(input, model: selectedModel, direction: selectedDirection, folder: folder)
        }
    }
    private func translate(_ source: String, model: String, direction: String, folder: URL) async throws -> TranslationRecord {
        let input = folder.appendingPathComponent("source.txt"), output = folder.appendingPathComponent("translation.json")
        try source.write(to: input, atomically: true, encoding: .utf8)
        try await CommandRunner().run(arguments: ["translate", "--input", input.path, "--model", model,
            "--direction", direction, "--output", output.path], log: folder.appendingPathComponent("translation.log"))
        try Task.checkCancellation()
        return try JSONDecoder().decode(TranslationRecord.self, from: Data(contentsOf: output))
    }

    func chooseDocument() {
        guard !busy else { return }
        let panel = NSOpenPanel()
        panel.title = "选择文档或图片"; panel.allowsMultipleSelection = false
        panel.allowedContentTypes = ["pdf", "docx", "pptx", "txt", "md", "png", "jpg", "jpeg", "heic", "tif", "tiff"].compactMap { UTType(filenameExtension: $0) }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        documentURL = url
        extractDocument()
    }
    func extractDocument() {
        guard let url = documentURL else { return }
        let mode = extractMode, limit = pageLimit
        document = nil; selectedBlock = nil; documentTranslations = [:]
        perform("正在提取文字…") { folder in
            let output = folder.appendingPathComponent("document.json")
            try await CommandRunner().run(arguments: ["extract", "--input", url.path, "--mode", mode,
                "--pages", String(limit), "--output", output.path], log: folder.appendingPathComponent("document.log"))
            try Task.checkCancellation()
            let result = try JSONDecoder().decode(ImportedDocument.self, from: Data(contentsOf: output))
            self.document = result; self.selectedBlock = result.blocks.first?.id
        }
    }
    func translateBlock() {
        guard let block = chosenBlock else { return }
        let selectedModel = model, selectedDirection = direction
        perform("正在翻译所选段落…") { folder in
            let result = try await self.translate(block.text, model: selectedModel, direction: selectedDirection, folder: folder)
            self.documentTranslations[block.id] = result
        }
    }
    func chooseAudio() {
        guard !busy else { return }
        let panel = NSOpenPanel(); panel.title = "选择英语录音"
        panel.allowedContentTypes = [.audio]; panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        audioURL = url; segments = []; speechRun = nil
    }
    func processAudio() {
        guard let audioURL else { return }
        let modelFolder = modelDirectory, tokenizerFolder = tokenizerDirectory, selectedModel = model
        segments = []; speechRun = nil
        perform("正在校验语音资源…") { folder in
            let manifest = folder.appendingPathComponent("resources.json")
            try await CommandRunner().run(arguments: ["seal-speech", "--model-folder", modelFolder.path,
                "--tokenizer-folder", tokenizerFolder.path, "--output", manifest.path], log: folder.appendingPathComponent("resources.log"))
            try Task.checkCancellation()
            self.activity = "正在加载模型并翻译录音…"
            let output = folder.appendingPathComponent("speech.json")
            let partial = output.appendingPathExtension("partial.json")
            let watcher = Task { @MainActor in
                while !Task.isCancelled {
                    if let data = try? Data(contentsOf: partial),
                       let items = try? JSONDecoder().decode([SpeechSegment].self, from: data) {
                        self.segments = items
                        self.activity = "已完成 \(items.count) 个片段，正在继续…"
                    }
                    do { try await Task.sleep(nanoseconds: 500_000_000) } catch { break }
                }
            }
            defer { watcher.cancel() }
            try await CommandRunner().run(arguments: ["transcribe", "--input", audioURL.path,
                "--resources", manifest.path, "--model", selectedModel, "--output", output.path], log: folder.appendingPathComponent("speech.log"))
            try Task.checkCancellation()
            let result = try JSONDecoder().decode(SpeechRun.self, from: Data(contentsOf: output))
            self.speechRun = result; self.segments = result.segments
        }
    }
    func chooseResourceRoot() {
        guard !busy else { return }
        let panel = NSOpenPanel(); panel.title = "选择包含 whisper-coreml 的 models 文件夹"
        panel.canChooseDirectories = true; panel.canChooseFiles = false; panel.allowsMultipleSelection = false
        if !resourceRoot.isEmpty { panel.directoryURL = URL(fileURLWithPath: resourceRoot) }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        resourceRoot = url.path
        UserDefaults.standard.set(resourceRoot, forKey: "speechResourceRoot")
    }
    func copy(_ text: String) { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string) }
    func paste() { if let text = NSPasteboard.general.string(forType: .string) { source = text } }
    func saveText(_ text: String, name: String) {
        let panel = NSSavePanel(); panel.nameFieldStringValue = name; panel.allowedContentTypes = [.plainText]
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do { try text.write(to: url, atomically: true, encoding: .utf8); notice = "已保存：\(url.lastPathComponent)" }
        catch { self.error = error.localizedDescription }
    }
    func exportAudio() {
        let title = speechRun == nil ? "未完成的录音片段" : "录音双语结果"
        let text = "\(title)\n来源：\(audioURL?.lastPathComponent ?? "")\nM0 测试结果；独立窗口可能切断句子，请核对原录音。\n\n"
            + segments.map { "[\(Self.time($0.start)) → \(Self.time($0.end))]\n\($0.english)\n\($0.chinese ?? "")" }.joined(separator: "\n\n")
        saveText(text, name: "录音双语结果.txt")
    }
    static func time(_ seconds: Double) -> String {
        let seconds = max(0, Int(seconds)); return String(format: "%02d:%02d", seconds / 60, seconds % 60)
    }
}
