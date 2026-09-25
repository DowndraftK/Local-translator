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
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if state?.busy == true || state?.streaming.busy == true || state?.textTask.job != nil {
            let alert = NSAlert()
            alert.messageText = state?.busy == true || state?.streaming.busy == true ? "停止当前任务并退出？" : "退出应用？"
            var messages: [String] = []
            if state?.textTask.job != nil {
                messages.append("文字翻译结果仅保留于当前窗口，退出后不能恢复。请先复制已有译文或导出双语 TXT；尚未完成的段落不会继续处理。")
            }
            if state?.streaming.busy == true { messages.append("录音任务中已保存的英文和翻译会保留，重新打开任务可补译。请优先在录音页停止任务并等待尾句保存；立即退出可能留下未完成的识别部分。") }
            if messages.isEmpty { messages.append("当前处理尚未完成；退出会停止当前任务。") }
            alert.informativeText = messages.joined(separator: "\n\n")
            alert.addButton(withTitle: state?.textTask.job != nil ? "退出并丢弃未导出的文字结果" : "停止并退出"); alert.addButton(withTitle: "返回应用")
            if alert.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        if state?.streaming.busy == true {
            state?.streaming.stop()
            Task { @MainActor [weak self] in
                while self?.state?.streaming.busy == true {
                    try? await Task.sleep(nanoseconds: 100_000_000)
                }
                self?.state?.shutdown()
                NSApp.reply(toApplicationShouldTerminate: true)
            }
            return .terminateLater
        }
        state?.shutdown()
        return .terminateNow
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
