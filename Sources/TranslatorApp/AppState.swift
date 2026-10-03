import AppKit
import Foundation
import Combine
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
    @Published var model = TranslationConfiguration.defaultModel { didSet { if oldValue != model { invalidateTextResult(); invalidateDocumentResult() } } }
    @Published var direction = "en-zh" { didSet { if oldValue != direction { invalidateTextResult(); invalidateDocumentResult(); documentTranslations = [:] } } }
    @Published var source = "" { didSet { if oldValue != source { invalidateTextResult() } } }
    let recoveryStore: TaskRecoveryStore
    @Published var savedTasks: [RecoveryTask] = []
    @Published var showSavedTasks = false
    @Published var savingStatus = "暂无未保存内容"
    @Published var recoveryErrors: [String] = []
    var textRecoveryID = UUID()
    var textConfiguration = TranslationConfiguration(model: TranslationConfiguration.defaultModel, direction: "en-zh")
    var documentConfiguration: TranslationConfiguration?
    var recoveryMetadata: [UUID: RecoveryTask] = [:]
    var recoveryRevision: [UUID: UInt64] = [:]
    var restoringRecovery = false
    var autosave: Task<Void, Never>?
    var dirtyGeneration: UInt64 = 0
    var persistenceFailure: String?
    let textTask = TextTranslationController()
    let documentTask = DocumentTranslationController()
    private var documentObservation: AnyCancellable?
    @Published var wholeDocument = true
    @Published var documentMode: DocumentExtractionMode = .text { didSet { if oldValue != documentMode { invalidateDocumentResult() } } }
    @Published var allDocumentPages = true
    @Published var rangeFirst = "1"
    @Published var rangeLast = "1"
    private var textObservation: AnyCancellable?
    @Published var activity: String?
    @Published var error: String?
    @Published var notice = "准备就绪"
    @Published var serviceStatus = "正在检查本机翻译服务"
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
    @Published var components: [RuntimeComponent] = []
    @Published var runtimeStatus = "尚未检查语音运行环境"
    @Published var preparingEnvironment = false
    @Published var setupProgress = 0.0
    @Published var setupMessage = "只准备明确选择的组件，不下载模型。"
    var setupTask: Task<Void, Never>?
    var setupOperation = UUID()
    var environmentCheckOperation = UUID()
    var environmentCheckTask: Task<Void, Never>?
    private var operation: Task<Void, Never>?
    private var serverProcess: Process?
    private var serverLog: FileHandle?
    private var startingService = false

    init() {
        let arguments = CommandLine.arguments
        let root: URL
        if let index = arguments.firstIndex(of: "--recovery-root"), index + 1 < arguments.count {
            root = URL(fileURLWithPath: arguments[index + 1], isDirectory: true)
        } else {
            root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("LocalTranslator/TextDocuments", isDirectory: true)
        }
        recoveryStore = TaskRecoveryStore(root: root)
        if let index = arguments.firstIndex(of: "--resource-root"), index + 1 < arguments.count {
            resourceRoot = arguments[index + 1]
        } else {
            resourceRoot = UserDefaults.standard.string(forKey: "speechResourceRoot")
                ?? Bundle.main.object(forInfoDictionaryKey: "M0ResourceRoot") as? String ?? ""
        }
        documentObservation = documentTask.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
        textObservation = textTask.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
        textTask.changed = { [weak self] in self?.scheduleRecoverySave() }
        documentTask.changed = { [weak self] in self?.scheduleRecoverySave() }
        documentTask.translator.changed = { [weak self] in self?.scheduleRecoverySave() }
        textTask.checkpoint = { [weak self] job in try await self?.saveTextCheckpoint(job) }
        documentTask.checkpoint = { [weak self] in try await self?.saveDocumentCheckpoint() }
        documentTask.translator.checkpoint = { [weak self] job in try await self?.saveDocumentCheckpoint(job: job) }
        documentTask.beforeFork = { [weak self] in self?.preserveCurrentRecovery() }
        Task { await refreshSavedTasks() }
        refreshEnvironment()
        if let index = CommandLine.arguments.firstIndex(of: "--open-recording-session"), index + 1 < CommandLine.arguments.count {
            page = .audio
            streaming.loadSession(URL(fileURLWithPath: CommandLine.arguments[index + 1]))
        }
    }
    var busy: Bool { activity != nil || textTask.busy || documentTask.busy }
    private func invalidateDocumentResult() {
        guard !restoringRecovery else { return }
        if !documentTask.busy, !documentTask.hasDrafts, let url = documentURL, wholeDocument {
            preserveCurrentRecovery(); documentTask.load(url); documentConfiguration = nil
        }
    }
    private func invalidateTextResult() {
        guard !restoringRecovery, !textTask.busy else { return }
        if textTask.job != nil {
            preserveCurrentRecovery(); textTask.clear(); textRecoveryID = UUID()
        }
        textConfiguration = TranslationConfiguration(model: model, direction: direction)
        scheduleRecoverySave()
    }
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
    var streamingResourcesPresent: Bool {
        let root = URL(fileURLWithPath: resourceRoot)
        return !resourceRoot.isEmpty && FileManager.default.fileExists(atPath: root.appendingPathComponent("whisper-mps-experiment/large-v3-turbo/weights.safetensors").path)
            && FileManager.default.fileExists(atPath: root.appendingPathComponent("speech-runtime-data-v1/assets-manifest.json").path)
    }
    var speechFilesPresent: Bool {
        !resourceRoot.isEmpty && FileManager.default.fileExists(atPath: modelDirectory.appendingPathComponent("AudioEncoder.mlmodelc").path)
            && FileManager.default.fileExists(atPath: tokenizerDirectory.appendingPathComponent("tokenizer.json").path)
    }

    var ollamaExecutable: String? {
        let paths = ["/Applications/Ollama.app/Contents/Resources/ollama", NSHomeDirectory() + "/Applications/Ollama.app/Contents/Resources/ollama", "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama"]
        if let path = paths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) { return path }
        if let component = components.first(where: { $0.id == "ollama-engine" }), let root = try? RuntimePaths.active(component) {
            return root.appendingPathComponent(component.executable).path
        }
        return nil
    }
    func checkService() async {
        guard !checkingService else { return }
        checkingService = true
        defer { checkingService = false; serviceChecked = true }
        do {
            let engine = try OllamaEngine()
            let version = try await engine.version()
            let models = try await engine.models()
            let readiness = OllamaReadiness.assess(version: version, tagsValid: true, portAvailable: false, executablePresent: ollamaExecutable != nil)
            serviceStatus = readiness.message
            guard case .ready = readiness else { serviceAvailable = false; installedModels = []; return }
            installedModels = models.filter { $0.remote_host == nil && $0.remote_model == nil && !$0.name.lowercased().contains("cloud") }.map(\.name)
            serviceAvailable = true
        } catch {
            serviceAvailable = false; installedModels = []
            serviceStatus = OllamaReadiness.assess(version: nil, tagsValid: false, portAvailable: OllamaReadiness.portAvailable(), executablePresent: ollamaExecutable != nil).message
        }
    }

    func startLocalService() {
        guard !startingService, !checkingService, !serviceAvailable else { return }
        startingService = true
        Task {
            defer { startingService = false }
            // Check again before launching: never replace an existing server.
            await checkService()
            guard !serviceAvailable else { return }
            do {
                guard OllamaReadiness.portAvailable() else { throw M0Error.unavailable(serviceStatus) }
                guard let path = ollamaExecutable else {
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
                let versionCheck = Process(); let versionPipe = Pipe()
                versionCheck.executableURL = URL(fileURLWithPath: path); versionCheck.arguments = ["--version"]
                versionCheck.standardOutput = versionPipe; versionCheck.standardError = versionPipe
                try versionCheck.run()
                let deadline = Date().addingTimeInterval(3)
                while versionCheck.isRunning && Date() < deadline { try await Task.sleep(nanoseconds: 50_000_000) }
                if versionCheck.isRunning { versionCheck.terminate(); throw M0Error.unavailable("翻译程序版本检查超时。") }
                let actualVersion = String(decoding: versionPipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                guard actualVersion.contains("0.35.0") else { throw M0Error.unavailable("已装翻译程序不属于固定 0.35.0 组合；保留原安装，请手动处理。") }
                child.standardOutput = handle; child.standardError = handle
                try child.run()
                serverProcess = child; serverLog = handle
                checkingService = true
                for _ in 0..<20 {
                    try await Task.sleep(nanoseconds: 250_000_000)
                    if child.isRunning, (try? await OllamaEngine().version()) == "0.35.0", let models = try? await OllamaEngine().models() {
                        installedModels = models.filter { $0.remote_host == nil && $0.remote_model == nil && !$0.name.lowercased().contains("cloud") }.map(\.name)
                        serviceAvailable = true; break
                    }
                    if !child.isRunning { break }
                }
                checkingService = false; serviceChecked = true
                if !serviceAvailable { throw M0Error.unavailable("本地服务启动失败。请打开本次测试目录查看 ollama.log。") }
                serviceStatus = "已启动本应用管理的 0.35.0 服务；保持 5 分钟自然驻留"
                let ownership = Process(); let ownershipPipe = Pipe()
                ownership.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
                ownership.arguments = ["-nP", "-a", "-p", String(child.processIdentifier), "-iTCP:11434", "-sTCP:LISTEN", "-Fn"]
                ownership.standardOutput = ownershipPipe; ownership.standardError = FileHandle.nullDevice
                try ownership.run(); ownership.waitUntilExit()
                let listener = String(decoding: ownershipPipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                guard ownership.terminationStatus == 0, listener.contains("127.0.0.1:11434"), child.isRunning else {
                    serviceAvailable = false; if child.isRunning { child.terminate() }
                    throw M0Error.unavailable("无法确认本应用服务的回环监听所有权，未启用推理。原服务保持。")
                }
                notice = "本地服务已启动，云功能已关闭"
            } catch { checkingService = false; self.error = error.localizedDescription }
        }
    }

    func operationForExit() { operation?.cancel() }
    func shutdown() {
        setupTask?.cancel()
        environmentCheckTask?.cancel()
        streaming.stop()
        streaming.pausePlayback()
        operation?.cancel()
        textTask.stop()
        documentTask.stop()
        if let process = serverProcess, process.isRunning { process.terminate() }
        try? serverLog?.close()
    }
    func cancel() {
        if documentTask.busy { documentTask.stop(); notice = "文档已停止；已有原文和完成译文可导出" }
        else if textTask.busy { textTask.stop(); notice = "已停止；已完成段可复制，全部原文可导出" }
        else { operation?.cancel(); notice = "正在停止，已完成结果仍可导出" }
        preserveCurrentRecovery()
    }

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
        guard !busy else { return }
        preserveCurrentRecovery()
        if textTask.job != nil {
            textRecoveryID = UUID()
            // Clear the old job before awaiting model metadata; a pending
            // autosave must not capture old results under the new recipe.
            textTask.clear()
            textConfiguration = TranslationConfiguration(model: model, direction: direction)
        }
        let input = source, selectedModel = model, selectedDirection = direction
        activity = "正在绑定本机翻译配置…"; error = nil
        operation = Task {
            defer { activity = nil; operation = nil }
            do {
                let engine = try OllamaEngine()
                let models = try await engine.models()
                try Task.checkCancellation()
                var config = textConfiguration
                try config.validate()
                if config.modelDigest == nil { config.modelDigest = models.first { $0.name == selectedModel }?.digest }
                textConfiguration = config
                textTask.start(source: input, model: selectedModel, direction: selectedDirection,
                    translate: recoveryTranslator(engine, configuration: textConfiguration), taskID: textRecoveryID)
                notice = "自动保存已启用；停止或重开后可主动继续"
            } catch { self.error = error.localizedDescription }
        }
    }
    func copyTextTranslation() {
        guard let snapshot = textTask.job else { return }
        Task {
            let text = await Task.detached { snapshot.completedTranslation }.value
            copy(text)
            notice = "已复制当前完成段的译文"
        }
    }
    func exportTextTranslation() {
        guard !source.isEmpty else { return }
        var snapshot = textTask.job ?? TextTranslationJob(id: textRecoveryID, source: source, model: model, direction: direction)
        if snapshot.segments.isEmpty { snapshot.phase = .stopped }
        let config = textConfiguration, saveStatus = savingStatus
        Task {
            let text = await Task.detached { "保存状态：\(saveStatus)\n配置版本：\(config.version) · 绑定：\(config.binding)\n模型 digest：\(config.modelDigest ?? "未知（草稿尚未请求模型）")\n" + snapshot.bilingualText }.value
            saveText(text, name: "文字双语对照.txt")
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
        preserveCurrentRecovery()
        documentTask.clear(); documentConfiguration = nil; document = nil; selectedBlock = nil; documentTranslations = [:]
        documentURL = url
        wholeDocument = ["pdf", "txt"].contains(url.pathExtension.lowercased())
        allDocumentPages = true; rangeFirst = "1"; rangeLast = "1"
        if wholeDocument { documentTask.load(url) } else { extractDocument() }
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
    func extractWholeDocument() {
        guard !busy else { return }
        let first = allDocumentPages || !documentTask.isPDF ? 1 : Int(rangeFirst) ?? 0
        let last = allDocumentPages || !documentTask.isPDF ? documentTask.totalPages : Int(rangeLast) ?? 0
        preserveCurrentRecovery()
        documentConfiguration = TranslationConfiguration(model: model, direction: direction)
        documentTask.extract(first: first, last: last, model: model, direction: direction, mode: documentMode)
    }
    func translateWholeDocument() {
        guard !busy, let snapshot = documentTask.snapshot else { return }
        preserveCurrentRecovery()
        activity = "正在绑定本机翻译配置…"; error = nil
        operation = Task {
            defer { activity = nil; operation = nil }
            do {
                let engine = try OllamaEngine()
                let models = try await engine.models()
                try Task.checkCancellation()
                var config = documentTask.translator.job == nil
                    ? documentConfiguration ?? TranslationConfiguration(model: snapshot.model, direction: snapshot.direction)
                    : TranslationConfiguration(model: snapshot.model, direction: snapshot.direction)
                try config.validate()
                if config.modelDigest == nil { config.modelDigest = models.first { $0.name == snapshot.model }?.digest }
                // beforeFork captures the old successful results with their old
                // recipe. Publish the new recipe only after the new UUID exists.
                documentTask.forkTranslation()
                documentConfiguration = config
                let translate = recoveryTranslator(engine, configuration: config)
                documentTask.start(translate: translate)
            } catch { self.error = error.localizedDescription }
        }
    }
    func exportDocumentTranslation(copyOnly: Bool = false) {
        guard let snapshot = documentTask.snapshot else { return }
        let translation = documentTask.translator.job
        let config = documentConfiguration, saveStatus = savingStatus
        Task {
            let text = await Task.detached {
                copyOnly ? (translation?.source == snapshot.sourceKey ? translation?.completedTranslation ?? "" : "") : "保存状态：\(saveStatus)\n配置绑定：\(config?.binding ?? "尚未绑定")\n" + snapshot.export(translation: translation)
            }.value
            if copyOnly { copy(text); notice = "已复制当前完成译文" }
            else { saveText(text, name: snapshot.file + "-双语.txt") }
        }
    }
    func translateBlock() {
        guard let block = chosenBlock, block.text.utf8.count <= TranslationBudget.sourceBytes else { return }
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
        Task {
            do {
                try await Task.detached { try text.write(to: url, atomically: true, encoding: .utf8) }.value
                notice = "已保存：\(url.lastPathComponent)"
            } catch { self.error = error.localizedDescription }
        }
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
