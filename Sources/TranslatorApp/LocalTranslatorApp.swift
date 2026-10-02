import AppKit
import Darwin
import SwiftUI

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var state: AppState?
    func applicationDidFinishLaunching(_ notification: Notification) {
        signal(SIGPIPE, SIG_IGN)
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    private var terminating = false
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !terminating else { return .terminateLater }
        if state?.busy == true || state?.streaming.busy == true {
            let alert = NSAlert()
            alert.messageText = "停止当前任务、保存并退出？"
            alert.informativeText = "文字和文档会保存当前原文、草稿及完整完成的译文。重开后需主动继续，当前生成中的半段会标为中断。录音请优先在录音页停止并等待尾句保存；立即退出可能留下未完成的识别部分。"
            alert.addButton(withTitle: "停止、保存并退出"); alert.addButton(withTitle: "返回应用")
            if alert.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        terminating = true
        if state?.streaming.busy == true { state?.streaming.stop() }
        Task { @MainActor [weak self] in
            guard let self else { return }
            while self.state?.streaming.busy == true { try? await Task.sleep(nanoseconds: 100_000_000) }
            do {
                try await self.state?.finishRecoveryForExit()
                self.state?.shutdown()
                NSApp.reply(toApplicationShouldTerminate: true)
            } catch {
                let alert = NSAlert()
                alert.messageText = "保存失败，返回应用导出结果？"
                alert.informativeText = "已落盘内容仍保留；本次未保存内容可能丢失。\n" + error.localizedDescription
                alert.addButton(withTitle: "返回应用"); alert.addButton(withTitle: "仍然退出")
                let exit = alert.runModal() == .alertSecondButtonReturn
                if exit { self.state?.shutdown() }
                self.terminating = false
                NSApp.reply(toApplicationShouldTerminate: exit)
            }
        }
        return .terminateLater
    }
}

/// Ask to terminate before closing the last window, so choosing “return” keeps
/// the editor and in-memory results accessible. Forward SwiftUI's other window
/// delegate methods instead of replacing its normal window management.
private final class CloseGuardDelegate: NSObject, NSWindowDelegate {
    let original: NSWindowDelegate?
    init(original: NSWindowDelegate?) { self.original = original }
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        NSApp.terminate(nil)
        return false
    }
    override func responds(to selector: Selector!) -> Bool {
        super.responds(to: selector) || original?.responds(to: selector) == true
    }
    override func forwardingTarget(for selector: Selector!) -> Any? {
        original?.responds(to: selector) == true ? original : super.forwardingTarget(for: selector)
    }
}

private struct WindowCloseGuard: NSViewRepresentable {
    final class GuardView: NSView {
        private var closeDelegate: CloseGuardDelegate?
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard let window, !(window.delegate is CloseGuardDelegate) else { return }
            let delegate = CloseGuardDelegate(original: window.delegate)
            closeDelegate = delegate
            window.delegate = delegate
        }
    }
    func makeNSView(context: Context) -> GuardView { GuardView() }
    func updateNSView(_ nsView: GuardView, context: Context) {}
}

@main struct LocalTranslatorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var state = AppState()
    var body: some Scene {
        Window("本地翻译器", id: "main") {
            WorkspaceView().environmentObject(state)
                .background(WindowCloseGuard())
                .onAppear { delegate.state = state }
                .task { await state.checkService() }
        }
        .defaultSize(width: 1180, height: 800)
        .windowResizability(.contentMinSize)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("翻译") {
                Button("翻译当前文字") { state.translateText() }
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(state.busy || state.source.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || state.page != .text)
                Button("停止当前任务") {
                    if state.streaming.busy { state.streaming.stop() } else { state.cancel() }
                }.keyboardShortcut(".", modifiers: .command).disabled(!state.busy && !state.streaming.busy)
            }
        }
    }
}
