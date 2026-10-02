import AppKit
import Foundation
import SwiftUI
import TranslatorCore

extension AppState {
    func recoveryTranslator(_ engine: OllamaEngine, configuration: TranslationConfiguration) -> TextTranslationController.Translate {
        { input, model, direction in
            try await withTaskCancellationHandler {
                try await engine.translate(input, model: model, direction: direction, configuration: configuration)
            } onCancel: { engine.cancelRequests() }
        }
    }
    func scheduleRecoverySave() {
        guard !restoringRecovery else { return }
        dirtyGeneration += 1
        savingStatus = "有未落盘内容 · 正在自动保存"
        autosave?.cancel()
        autosave = Task {
            do {
                try await Task.sleep(nanoseconds: 350_000_000)
                try Task.checkCancellation()
                try await flushRecovery()
            } catch is CancellationError {} catch { recoverySaveFailed(error) }
        }
    }
    private func stamp(_ task: RecoveryTask) -> RecoveryTask {
        var t = task
        t.created = recoveryMetadata[t.id]?.created ?? t.created
        let next = max(recoveryRevision[t.id] ?? 0, recoveryMetadata[t.id]?.revision ?? 0) + 1
        recoveryRevision[t.id] = next; t.revision = next; t.updated = Date()
        recoveryMetadata[t.id] = t
        return t
    }
    func textRecoveryRecord(job: TextTranslationJob? = nil) -> RecoveryTask? {
        let job = job ?? textTask.job
        let input = job?.source ?? source
        let id = job?.id ?? textRecoveryID
        guard !input.isEmpty || recoveryMetadata[id] != nil else { return nil }
        return stamp(RecoveryTask(id: id, name: "文字 · " + String(input.prefix(30)).replacingOccurrences(of: "\n", with: " "),
            configuration: textConfiguration, input: input, text: job))
    }
    func documentRecoveryRecord(job: TextTranslationJob? = nil) -> RecoveryTask? {
        guard let s = documentTask.snapshot, let data = documentTask.data else { return nil }
        let ext = s.isPDF ? "pdf" : "txt"
        let asset = RecoveryAsset(originalPath: s.originalPath ?? recoveryMetadata[s.id]?.asset?.originalPath ?? documentURL?.path ?? s.file,
            filename: "source-" + s.fingerprint + "." + ext, fingerprint: s.fingerprint, byteCount: data.count)
        return stamp(RecoveryTask(id: s.id, name: s.file + " · " + (s.isPDF ? s.range.label + " 页" : "全文"),
            configuration: documentConfiguration ?? TranslationConfiguration(model: s.model, direction: s.direction),
            text: job ?? documentTask.translator.job, document: s, drafts: documentTask.drafts, asset: asset))
    }
    private func persist(_ record: RecoveryTask, assetData: Data? = nil) async throws {
        let generation = dirtyGeneration
        let start = Date()
        do {
            let saved = try await recoveryStore.save(record, assetData: assetData)
            if saved, generation == dirtyGeneration {
                savingStatus = "已自动保存 \(record.updated.formatted(date: .omitted, time: .standard)) · \(Int(Date().timeIntervalSince(start) * 1000)) ms"
                if let failure = persistenceFailure,
                   self.error == "自动保存失败，已停止继续处理：" + failure {
                    self.error = nil
                }
                persistenceFailure = nil
            }
        } catch {
            if record.id == textTask.job?.id || record.id == textRecoveryID || record.id == documentTask.snapshot?.id {
                recoverySaveFailed(error)
            } else { self.error = "旧任务保存失败（" + record.name + "）：" + error.localizedDescription }
            throw error
        }
    }
    func saveTextCheckpoint(_ job: TextTranslationJob) async throws {
        guard let record = textRecoveryRecord(job: job) else { return }
        try await persist(record)
    }
    func saveDocumentCheckpoint(job: TextTranslationJob? = nil) async throws {
        guard let record = documentRecoveryRecord(job: job) else { throw M0Error.invalid("无法保存文档源副本。") }
        try await persist(record, assetData: documentTask.data)
    }
    func preserveCurrentRecovery() {
        guard !restoringRecovery else { return }
        // Capture before switching/clearing. These tasks must survive cancellation
        // of a debounce timer; revisions prevent a delayed capture winning later.
        let text = textRecoveryRecord(), document = documentRecoveryRecord(), data = documentTask.data
        Task {
            do {
                if let text { try await persist(text) }
                if let document { try await persist(document, assetData: data) }
            } catch { /* persist reports the failure for its owning task */ }
        }
    }
    func flushRecovery() async throws {
        let text = textRecoveryRecord(), document = documentRecoveryRecord(), data = documentTask.data
        if let text { try await persist(text) }
        if let document { try await persist(document, assetData: data) }
    }
    private func recoverySaveFailed(_ error: Error) {
        persistenceFailure = error.localizedDescription
        savingStatus = "保存失败 · 当前内存结果尚未落盘，请导出"
        self.error = "自动保存失败，已停止继续处理：" + error.localizedDescription
        textTask.stop(); documentTask.stop()
    }
    func refreshSavedTasks() async {
        do {
            let list = try await recoveryStore.list()
            savedTasks = list.tasks; recoveryErrors = list.errors
        } catch { recoveryErrors = [error.localizedDescription] }
    }
    func openSavedTasks() {
        guard !busy else { return }
        Task {
            do { try await flushRecovery() } catch { /* keep the in-memory task available */ }
            await refreshSavedTasks(); showSavedTasks = true
        }
    }
    func openRecovery(_ id: UUID) {
        guard !busy else { return }
        Task {
            do {
                try await flushRecovery()
                let opened = try await recoveryStore.read(id), saved = opened.task
                restoringRecovery = true
                defer { restoringRecovery = false }
                recoveryMetadata[id] = saved; recoveryRevision[id] = saved.revision
                model = saved.configuration.model; direction = saved.configuration.direction
                if var d = saved.document {
                    if d.originalPath == nil { d.originalPath = saved.asset?.originalPath }
                    textTask.clear(); source = ""; textRecoveryID = UUID(); textConfiguration = saved.configuration
                    var assetURL: URL?
                    do { assetURL = try await recoveryStore.sourceData(saved).0 }
                    catch { self.error = error.localizedDescription }
                    documentConfiguration = saved.configuration
                    wholeDocument = true; documentMode = d.mode; allDocumentPages = d.range.first == 1 && d.range.last == d.totalPages
                    rangeFirst = String(d.range.first); rangeLast = String(d.range.last)
                    documentURL = assetURL
                    await documentTask.restore(snapshot: d, translation: saved.text, drafts: saved.drafts, assetURL: assetURL)
                    page = .documents
                } else {
                    documentTask.clear(); documentURL = nil; documentConfiguration = nil
                    textRecoveryID = id; textConfiguration = saved.configuration
                    source = saved.input; textTask.restore(saved.text); page = .text
                }
                showSavedTasks = false
                savingStatus = opened.warning ?? "已打开保存于 \(saved.updated.formatted()) 的任务 · 未启动推理"
                notice = "已打开任务；点击继续才会处理未完成部分"
                if let warning = opened.warning { self.error = warning }
            } catch { self.error = error.localizedDescription }
        }
    }
    func continueTextRecovery() {
        guard !busy, textTask.canResume else { return }
        do {
            try textConfiguration.validate()
            let engine = try OllamaEngine()
            textTask.resume(translate: recoveryTranslator(engine, configuration: textConfiguration))
        } catch { self.error = error.localizedDescription }
    }
    func continueDocumentRecovery() {
        guard !busy, let config = documentConfiguration, documentTask.data != nil else { return }
        do {
            try config.validate()
            if documentTask.canContinueExtraction { documentTask.continueExtraction() }
            else if documentTask.translator.canResume {
                let engine = try OllamaEngine()
                documentTask.translator.resume(translate: recoveryTranslator(engine, configuration: config))
            }
        } catch { self.error = error.localizedDescription }
    }
    func finishRecoveryForExit() async throws {
        autosave?.cancel(); autosave = nil
        operationForExit()
        textTask.stop(); documentTask.stop()
        try await flushRecovery()
        if let persistenceFailure { throw M0Error.unavailable(persistenceFailure) }
    }
}

struct SavedTasksSheet: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("打开已保存任务").font(.title2)
                Spacer()
                Button("关闭") { state.showSavedTasks = false }
            }
            Text("打开只读取保存内容。需要时主动继续；成功段保留，失败段每轮最多尝试三次。输入草稿也会保存。").font(.callout).foregroundStyle(.secondary)
            if !state.recoveryErrors.isEmpty {
                ScrollView { ForEach(state.recoveryErrors, id: \.self) { Text($0).foregroundStyle(.orange).textSelection(.enabled) } }.frame(maxHeight: 100)
            }
            if state.savedTasks.isEmpty { Text("还没有已保存任务").frame(maxWidth: .infinity, maxHeight: .infinity) }
            else {
                List(state.savedTasks) { task in
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("\(task.kind) · \(task.name)")
                            Text("\(task.updated.formatted()) · \(task.configuration.direction) · \(task.status)").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("打开") { state.openRecovery(task.id) }.accessibilityLabel("打开 " + task.name)
                    }.padding(.vertical, 5)
                }
            }
        }.padding(24).frame(width: 820, height: 500)
    }
}
