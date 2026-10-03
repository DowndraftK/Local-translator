import AppKit
import TranslatorCore
import UniformTypeIdentifiers

extension AppState {
    func refreshEnvironment() {
        guard !preparingEnvironment else { return }
        environmentCheckTask?.cancel()
        let operation = UUID(); environmentCheckOperation = operation
        do {
            components = try RuntimePaths.bundledCatalog().components
            guard let component = components.first(where: { $0.id == "speech-runtime" }), let folder = try? RuntimePaths.active(component) else {
                runtimeStatus = "语音运行环境尚未准备或需要修复"; return
            }
            runtimeStatus = "正在校验已安装语音环境…"
            environmentCheckTask = Task {
                let verification = Task.detached(priority: .utility) { try RuntimeInstaller.validate(component, at: folder) }
                do {
                    try await withTaskCancellationHandler(operation: { try await verification.value }, onCancel: { verification.cancel() })
                    guard !Task.isCancelled, environmentCheckOperation == operation else { return }
                    runtimeStatus = "语音运行环境完整校验通过；开始时仍核对模型与资源"
                } catch {
                    guard !Task.isCancelled, environmentCheckOperation == operation else { return }
                    runtimeStatus = "已装环境损坏或不配，请修复：" + error.localizedDescription
                }
            }
        } catch { runtimeStatus = error.localizedDescription }
    }
    func prepareComponent(_ component: RuntimeComponent, local: URL? = nil) {
        guard !preparingEnvironment, !busy, !streaming.busy else { return }
        environmentCheckTask?.cancel()
        let operation = UUID(); setupOperation = operation
        preparingEnvironment = true; setupProgress = 0; setupMessage = "正在准备…"; error = nil
        setupTask = Task { [weak self] in
            guard let self else { return }
            defer { self.preparingEnvironment = false; self.setupTask = nil; self.refreshEnvironment() }
            do {
                try await RuntimeInstaller.install(component, from: local, busy: { self.busy || self.streaming.busy }, report: { [weak self] progress, message in
                    Task { @MainActor in
                        guard let self, self.preparingEnvironment, self.setupOperation == operation else { return }
                        self.setupProgress = progress; self.setupMessage = message
                    }
                })
                self.setupProgress = 1; self.setupMessage = "已准备。可开始处理；模型仍由外部目录提供。"
            } catch is CancellationError { self.setupMessage = "准备已取消，旧环境与任务保持。重试会重新下载。" }
            catch { self.setupMessage = "准备未完成，旧环境与任务保持。"; self.error = error.localizedDescription }
        }
    }
    func importComponent(_ component: RuntimeComponent) {
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.zip]; panel.allowsMultipleSelection = false
        panel.message = "选择本版本可信清单匹配的运行包；校验不配不会安装。"
        if panel.runModal() == .OK, let url = panel.url { prepareComponent(component, local: url) }
    }
    func cancelPreparation() { setupTask?.cancel(); setupMessage = "正在取消并清理本次临时文件…" }
    func rollbackEnvironment() {
        guard !preparingEnvironment, !busy, !streaming.busy else { return }
        preparingEnvironment = true
        setupTask = Task {
            defer { preparingEnvironment = false; setupTask = nil; refreshEnvironment() }
            do {
                try await RuntimeInstaller.rollback(RuntimePaths.bundledCatalog(), busy: { self.busy || self.streaming.busy })
                setupMessage = "已切回校验通过的上一运行环境，任务和数据库保持。"
            } catch { self.error = error.localizedDescription; setupMessage = "回退未完成，当前环境与任务保持。" }
        }
    }
}
