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
        if state?.busy == true || state?.streaming.busy == true {
            let alert = NSAlert()
            alert.messageText = "停止当前任务并退出？"
            alert.informativeText = "录音任务中已保存的英文和翻译会保留，重新打开任务可补译。请优先在录音页停止任务并等待尾句保存；立即退出可能留下未完成的识别部分。"
            alert.addButton(withTitle: "停止并退出"); alert.addButton(withTitle: "继续处理")
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

@main struct LocalTranslatorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var state = AppState()
    var body: some Scene {
        Window("本地翻译器", id: "main") {
            WorkspaceView().environmentObject(state)
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
