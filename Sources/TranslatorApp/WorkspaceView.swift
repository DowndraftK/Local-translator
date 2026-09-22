import AppKit
import SwiftUI
import TranslatorCore

private let accent = Color(red: 0.04, green: 0.47, blue: 0.43)

struct WorkspaceView: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        HStack(spacing: 0) {
            sidebar.frame(width: 205)
            Divider()
            VStack(alignment: .leading, spacing: 0) {
                header
                Divider()
                Group {
                    switch state.page {
                    case .text: TextWorkspace()
                    case .documents: DocumentWorkspace()
                    case .audio: AudioWorkspace()
                    case .settings: ResourceWorkspace()
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .padding(24)
                Divider()
                footer
            }
            .background(Color(nsColor: .windowBackgroundColor))
        }
        .tint(accent)
        .frame(minWidth: 980, minHeight: 690)
    }
    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Image(systemName: "character.bubble.fill").font(.system(size: 28)).foregroundStyle(accent)
                VStack(alignment: .leading, spacing: 3) {
                    Text("本地翻译器").font(.system(size: 17, weight: .semibold))
                    Text("你的 Mac，你的译文").font(.system(size: 10)).foregroundStyle(.secondary)
                }
            }.padding(.bottom, 29).padding(.top, 18)
            ForEach(WorkspacePage.allCases) { page in
                Button { state.page = page } label: {
                    Label(page.rawValue, systemImage: page.symbol)
                        .font(.system(size: 13, weight: state.page == page ? .semibold : .regular))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 12).padding(.vertical, 12)
                        .foregroundStyle(state.page == page ? accent : .primary)
                        .background(state.page == page ? accent.opacity(0.10) : .clear, in: RoundedRectangle(cornerRadius: 9))
                }.buttonStyle(.plain)
            }
            Spacer()
            VStack(alignment: .leading, spacing: 9) {
                Label(state.serviceAvailable ? "本机服务已连接" : state.serviceChecked ? "本机服务未连接" : "正在检查本机服务", systemImage: state.serviceAvailable ? "checkmark.circle.fill" : "circle.dotted")
                    .font(.system(size: 11)).foregroundStyle(state.serviceAvailable ? accent : .secondary)
                Text("M0 交互测试版").font(.system(size: 11, weight: .medium))
                Text("模型与结果在本机处理\n当前功能仍需质量核对")
                    .font(.system(size: 10)).foregroundStyle(.secondary).lineSpacing(3)
            }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
        }
        .padding(.horizontal, 16).padding(.bottom, 18)
        .background(Color(nsColor: .controlBackgroundColor))
    }
    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 5) {
                Text(state.page.rawValue).font(.system(size: 25, weight: .semibold))
                Text(subtitle).font(.system(size: 12)).foregroundStyle(.secondary)
            }
            Spacer()
            if state.page != .settings {
                Menu {
                    Picker("翻译模型", selection: $state.model) {
                        ForEach(state.models, id: \.self) { Text($0).tag($0) }
                    }
                } label: { Label(state.modelLabel, systemImage: "cpu").font(.system(size: 12, weight: .medium)) }
                    .fixedSize().disabled(state.busy).accessibilityLabel("选择翻译模型")
            }
        }.padding(.horizontal, 26).padding(.vertical, 20)
    }
    private var subtitle: String {
        switch state.page {
        case .text: return "粘贴原文，查看中英对照，保留重要细节。"
        case .documents: return "从文档或图片提取文字，选择段落进行翻译。"
        case .audio: return "导入英语录音，查看带时间轴的双语片段。"
        case .settings: return "检查本机服务和已有模型的位置。"
        }
    }
    private var footer: some View {
        VStack(alignment: .leading, spacing: 7) {
            if let error = state.error {
                HStack(alignment: .top) {
                    Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
                    Text(error).font(.system(size: 11)).lineLimit(4).textSelection(.enabled)
                    Spacer()
                    Button { state.error = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain).accessibilityLabel("关闭错误提示")
                }
            }
            HStack(spacing: 9) {
                if state.busy { ProgressView().controlSize(.small) }
                Text(state.activity ?? state.notice).font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                if state.busy { Button("停止", role: .cancel) { state.cancel() }.controlSize(.small) }
                if let folder = state.lastWorkFolder {
                    Button("打开本次测试目录") { NSWorkspace.shared.open(folder) }.buttonStyle(.link).font(.system(size: 11))
                }
            }
        }.padding(.horizontal, 24).padding(.vertical, 12)
    }
}

struct Card<Content: View>: View {
    @ViewBuilder let content: Content
    var body: some View {
        content.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(.quaternary, lineWidth: 1))
    }
}

private struct EmptyWorkspace: View {
    let symbol: String
    let title: String
    let message: String
    var body: some View {
        VStack(spacing: 13) {
            Image(systemName: symbol).font(.system(size: 33, weight: .light)).foregroundStyle(accent.opacity(0.65))
            Text(title).font(.system(size: 15, weight: .medium))
            Text(message).font(.system(size: 12)).foregroundStyle(.secondary).multilineTextAlignment(.center).lineSpacing(4)
        }.frame(maxWidth: .infinity, maxHeight: .infinity).padding(30)
    }
}

private struct ReviewHint: View {
    let text: String
    var body: some View {
        Label { Text(text).font(.system(size: 11)).lineSpacing(3) } icon: {
            Image(systemName: "info.circle").font(.system(size: 12))
        }.foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
    }
}

private struct DirectionPicker: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        Picker("翻译方向", selection: $state.direction) {
            Text("英语 → 简体中文").tag("en-zh")
            Text("中文 → 英语").tag("zh-en")
        }.labelsHidden().frame(width: 190).disabled(state.busy)
    }
}

struct TextWorkspace: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        VStack(spacing: 16) {
            HStack {
                DirectionPicker()
                Spacer()
                Button("粘贴") { state.paste() }.disabled(state.busy)
                Button("试用示例") { state.source = "Students must submit the application by September 30. Late submissions will not be accepted unless an extension has been approved in advance."; state.direction = "en-zh" }.disabled(state.busy)
            }
            HStack(alignment: .top, spacing: 16) {
                Card {
                    VStack(alignment: .leading, spacing: 0) {
                        HStack {
                            Text("原文").font(.system(size: 13, weight: .semibold))
                            Spacer()
                            Text("\(state.source.count) / 8000").font(.system(size: 11, design: .monospaced)).foregroundStyle(state.source.count > 8000 ? .red : .secondary)
                        }.padding(17)
                        Divider()
                        ZStack(alignment: .topLeading) {
                            if state.source.isEmpty { Text("在这里输入或粘贴需要翻译的文字…").foregroundStyle(.tertiary).padding(.horizontal, 21).padding(.top, 20) }
                            TextEditor(text: $state.source).font(.system(size: 15)).lineSpacing(7)
                                .scrollContentBackground(.hidden).padding(13).disabled(state.busy).accessibilityLabel("输入原文")
                        }
                    }
                }
                Card {
                    VStack(alignment: .leading, spacing: 0) {
                        HStack {
                            Text("译文").font(.system(size: 13, weight: .semibold))
                            Spacer()
                            if let result = state.translation {
                                Text(String(format: "%.2f 秒", result.elapsedSeconds)).font(.system(size: 11, design: .monospaced)).foregroundStyle(accent)
                                Button { state.copy(result.translation) } label: { Image(systemName: "doc.on.doc") }.buttonStyle(.plain).accessibilityLabel("复制译文")
                            }
                        }.padding(17)
                        Divider()
                        if let result = state.translation {
                            ScrollView {
                                VStack(alignment: .leading, spacing: 18) {
                                    Text(result.translation).font(.system(size: 15)).lineSpacing(7).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                                    ForEach(result.warnings, id: \.self) { ReviewHint(text: $0) }
                                }.padding(20)
                            }
                        } else {
                            EmptyWorkspace(symbol: "character.bubble", title: state.busy ? "正在本机处理中" : "译文将显示在这里", message: state.busy ? "首次请求可能需要先加载模型。" : "选择翻译方向，然后点击「开始翻译」。")
                        }
                    }
                }
            }
            HStack {
                ReviewHint(text: "M0 每次最多 8000 字符。日期、否定和条件关系请对照原文核对。")
                Spacer()
                if let result = state.translation {
                    Button("保存对照") { state.saveText("原文\n\(result.source)\n\n译文\n\(result.translation)", name: "文字翻译.txt") }
                }
                Button { state.translateText() } label: { Label("开始翻译", systemImage: "arrow.right").padding(.horizontal, 8) }
                    .buttonStyle(.borderedProminent).controlSize(.large)
                    .disabled(state.busy || state.source.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || state.source.count > 8000)
            }
        }
    }
}

struct DocumentWorkspace: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Button { state.chooseDocument() } label: { Label("选择文件", systemImage: "plus") }.disabled(state.busy)
                Picker("提取方式", selection: $state.extractMode) {
                    Text("自动").tag("auto"); Text("文字层").tag("text"); Text("OCR").tag("ocr"); Text("结构识别").tag("layout")
                }.frame(width: 170).disabled(state.busy)
                Stepper("PDF 前 \(state.pageLimit) 页", value: $state.pageLimit, in: 1...200).frame(width: 150).disabled(state.busy)
                Spacer()
                if state.documentURL != nil { Button("重新提取") { state.extractDocument() }.disabled(state.busy) }
            }
            if let document = state.document {
                HStack {
                    Label(document.file, systemImage: "doc.text").font(.system(size: 12, weight: .medium)).lineLimit(1)
                    Text("\(document.blocks.count) 个片段").font(.system(size: 11)).foregroundStyle(.secondary)
                    Spacer()
                    if let url = state.documentURL { Button("打开原文件") { NSWorkspace.shared.open(url) }.buttonStyle(.link) }
                }
                HStack(spacing: 14) {
                    List(selection: $state.selectedBlock) {
                        ForEach(document.blocks, id: \.id) { block in
                            VStack(alignment: .leading, spacing: 5) {
                                Text(state.sourceLabel(for: block)).font(.system(size: 10)).foregroundStyle(.secondary)
                                Text(block.text).font(.system(size: 12)).lineLimit(3)
                            }.padding(.vertical, 6).tag(block.id)
                        }
                    }.listStyle(.inset).frame(width: 235).clipShape(RoundedRectangle(cornerRadius: 10))
                    Card {
                        if let block = state.chosenBlock {
                            ScrollView {
                                VStack(alignment: .leading, spacing: 18) {
                                    Text("所选原文").font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
                                    Text(block.text).font(.system(size: 15)).lineSpacing(6).textSelection(.enabled)
                                    Divider()
                                    HStack {
                                        DirectionPicker()
                                        Spacer()
                                        Button("翻译所选段落") { state.translateBlock() }.buttonStyle(.borderedProminent).disabled(state.busy || block.text.count > 8000)
                                    }
                                    if block.text.count > 8000 { ReviewHint(text: "此片段超过 8000 字符，请复制较短内容到文字翻译页。") }
                                    if let result = state.selectedTranslation {
                                        Text(result.translation).font(.system(size: 15)).lineSpacing(6).textSelection(.enabled)
                                        ForEach(result.warnings, id: \.self) { ReviewHint(text: $0) }
                                        Button("保存本段对照") { state.saveText("来源：\(document.file)\n\(state.sourceLabel(for: block))\n\n\(result.source)\n\n\(result.translation)", name: "文档选段翻译.txt") }
                                    }
                                }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
                            }
                        }
                    }
                }
                if !document.warnings.isEmpty {
                    DisclosureGroup("提取范围与核对提示（\(document.warnings.count)）") {
                        ScrollView { VStack(alignment: .leading, spacing: 6) { ForEach(document.warnings, id: \.self) { ReviewHint(text: $0) } } }.frame(maxHeight: 90)
                    }.font(.system(size: 11)).foregroundStyle(.secondary)
                }
            } else {
                Card { EmptyWorkspace(symbol: "doc.text.image", title: "把阅读材料带进来", message: "支持 PDF、Word、PowerPoint、TXT、Markdown 和图片。\n先提取文字，再选择需要翻译的段落。") }
            }
            ReviewHint(text: "当前提供提取和选段翻译；整篇翻译、原版排版导出及完整表格语义尚未完成。")
        }
    }
}

struct AudioWorkspace: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        StreamingAudioWorkspace(controller: state.streaming)
    }
}

struct LegacyAudioWorkspace: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Button { state.chooseAudio() } label: { Label("导入英语录音", systemImage: "plus") }.disabled(state.busy)
                Picker("语音模型", selection: $state.speechModel) {
                    Text("Whisper turbo").tag("turbo"); Text("Whisper small.en").tag("small.en")
                }.frame(width: 220).disabled(state.busy)
                Spacer()
                Button("开始转写与翻译") { state.processAudio() }.buttonStyle(.borderedProminent)
                    .disabled(state.busy || state.audioURL == nil || !state.speechFilesPresent)
            }
            ReviewHint(text: "当前处理导入文件。麦克风实时字幕尚未实现；15 秒窗口可能切断句子，译文需核对。")
            if !state.speechFilesPresent {
                HStack {
                    Label("未找到匹配的语音资源", systemImage: "folder.badge.questionmark").foregroundStyle(.orange)
                    Button("配置资源") { state.page = .settings }
                }.font(.system(size: 12))
            }
            if let url = state.audioURL {
                HStack {
                    Label(url.lastPathComponent, systemImage: "waveform").font(.system(size: 12, weight: .medium)).lineLimit(1)
                    Spacer()
                    Button("打开原录音") { NSWorkspace.shared.open(url) }.buttonStyle(.link)
                    if !state.segments.isEmpty { Button("导出双语文本") { state.exportAudio() } }
                }
            }
            Card {
                if state.segments.isEmpty {
                    EmptyWorkspace(symbol: "waveform", title: state.busy ? "正在准备语音模型" : "听见原文，读懂译文", message: state.busy ? "首次加载可能需要约两分钟。\n完成的双语片段会逐步显示，你也可以停止任务。" : "导入一段清晰英语，开始本机转写与翻译。\n结果保留片段时间，可导出为双语文本。")
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 0) {
                            ForEach(state.segments, id: \.index) { segment in
                                HStack(alignment: .top, spacing: 18) {
                                    Text("\(AppState.time(segment.start))\n\(AppState.time(segment.end))")
                                        .font(.system(size: 11, design: .monospaced)).foregroundStyle(accent).lineSpacing(5).frame(width: 47)
                                    VStack(alignment: .leading, spacing: 9) {
                                        Text(segment.english).font(.system(size: 13)).foregroundStyle(.secondary).lineSpacing(4)
                                        Text(segment.chinese ?? "尚未翻译").font(.system(size: 15)).lineSpacing(5)
                                    }.frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                                }.padding(20)
                                Divider().padding(.leading, 85)
                            }
                        }
                    }
                }
            }
            if let run = state.speechRun {
                Text(String(format: "音频 %.1f 秒 · 处理 %.2f 秒 · 资源加载 %.2f 秒 · %d 个片段", run.audioSeconds, run.elapsedSeconds, run.resourceLoadSeconds ?? 0, run.segments.count))
                    .font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
            }
            ReviewHint(text: "已完成片段保存在本次临时测试目录；停止后可导出。长期资料库、自动续跑和 SRT/VTT 导出尚未实现。")
        }
    }
}

struct ResourceWorkspace: View {
    @EnvironmentObject var state: AppState
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                resourceCard(title: "文字翻译服务", symbol: "cpu") {
                    HStack {
                        Label(state.serviceAvailable ? "Ollama 已连接" : "Ollama 未连接", systemImage: state.serviceAvailable ? "checkmark.circle.fill" : "exclamationmark.circle")
                            .foregroundStyle(state.serviceAvailable ? accent : .orange)
                        Spacer()
                        if state.checkingService { ProgressView().controlSize(.small) }
                        Button("刷新状态") { Task { await state.checkService() } }.disabled(state.checkingService || state.busy)
                        if !state.serviceAvailable { Button("启动本地服务") { state.startLocalService() }.buttonStyle(.borderedProminent).disabled(state.checkingService || state.busy) }
                    }
                    Text("127.0.0.1:11434").font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                    ForEach(state.installedModels, id: \.self) { name in
                        Label(name, systemImage: "internaldrive").font(.system(size: 12))
                    }
                    ReviewHint(text: "应用启动的服务会关闭云功能，并在退出时停止。已经运行的服务会继续保留；程序只请求本机已安装的模型。")
                }
                resourceCard(title: "Whisper 语音资源", symbol: "waveform") {
                    HStack {
                        Label(state.speechFilesPresent ? "找到所选模型的文件" : "需要选择资源目录", systemImage: state.speechFilesPresent ? "checkmark.circle.fill" : "folder.badge.questionmark")
                            .foregroundStyle(state.speechFilesPresent ? accent : .orange)
                        Spacer()
                        Button("选择 models 文件夹") { state.chooseResourceRoot() }.disabled(state.busy)
                    }
                    Text(state.resourceRoot.isEmpty ? "尚未配置" : state.resourceRoot)
                        .font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary).textSelection(.enabled)
                    ReviewHint(text: "目录应包含 whisper-coreml、whisper-tokenizer 和 whisper-tokenizer-small.en。这里只检查文件位置，开始处理时会进行完整校验和实际加载。")
                }
                resourceCard(title: "关于这个测试包", symbol: "shippingbox") {
                    Text("本地翻译器 · M0 交互测试版").font(.system(size: 14, weight: .medium))
                    Text("适用于这台 Apple Silicon Mac，macOS 26 或更新版本。\n应用包复用本机 Ollama 和语音模型；移动应用无需复制权重，移动模型后请重新选择资源目录。")
                        .font(.system(size: 12)).foregroundStyle(.secondary).lineSpacing(5)
                    ReviewHint(text: "连续识别的录音任务保存在本机 Application Support/LocalTranslator/Recordings，可打开任务继续补译。文字、文档与原生引擎对照仍使用临时目录。")
                    if let folder = state.lastWorkFolder { Button("打开本次测试目录") { NSWorkspace.shared.open(folder) } }
                }
            }.padding(1)
        }
    }
    private func resourceCard<Content: View>(title: String, symbol: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            Label(title, systemImage: symbol).font(.system(size: 15, weight: .semibold))
            Divider()
            content()
        }.padding(22).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(.quaternary, lineWidth: 1))
    }
}
