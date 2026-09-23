import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct StreamingAudioWorkspace: View {
    @EnvironmentObject var state: AppState
    @ObservedObject var controller: StreamingSessionController
    @State private var input: URL?
    @State private var editing: StreamingSegment?
    @State private var editedText = ""
    @State private var showLegacy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Button { chooseInput() } label: { Label("导入英语音频或视频", systemImage: "plus") }
                Button("打开已保存任务") { controller.openSession() }
                Spacer()
                Menu("导出") {
                    Button("双语文本 TXT") { controller.export("txt", resourceRoot: state.resourceRoot) }
                    Button("字幕 SRT") { controller.export("srt", resourceRoot: state.resourceRoot) }
                    Button("字幕 WebVTT") { controller.export("vtt", resourceRoot: state.resourceRoot) }
                }.disabled(controller.snapshot?.segments.isEmpty != false)
            }.disabled(controller.busy || state.busy)

            HStack {
                Toggle("按原速回放输入", isOn: $controller.paced).toggleStyle(.checkbox)
                Toggle("同时翻译中文", isOn: $controller.translationEnabled).toggleStyle(.checkbox)
                Toggle("仅录音，稍后识别", isOn: $controller.recordOnly).toggleStyle(.checkbox)
                Picker("解码", selection: $controller.useCPU) {
                    Text("Mac GPU").tag(false); Text("CPU 对照").tag(true)
                }.frame(width: 175)
                Spacer()
            }.font(.system(size: 12)).disabled(controller.busy || state.busy)

            HStack {
                if let input { Text(input.lastPathComponent).lineLimit(1).font(.system(size: 12)).foregroundStyle(.secondary) }
                Spacer()
                if controller.busy {
                    if controller.recording || controller.paused {
                        Button(controller.paused ? "继续录音" : "暂停录音") {
                            if controller.paused { controller.resumeRecording() } else { controller.pauseRecording() }
                        }.disabled(controller.stopping || controller.pauseInProgress)
                    }
                    Button("停止并保存") { controller.stop() }.tint(.red).disabled(controller.stopping)
                } else {
                    Button { controller.start(input: nil, resourceRoot: state.resourceRoot, model: state.model, microphone: true) } label: {
                        Label("开始麦克风录音", systemImage: "mic.fill")
                    }
                    Button("开始处理文件") { controller.start(input: input, resourceRoot: state.resourceRoot, model: state.model) }
                        .buttonStyle(.borderedProminent).disabled(input == nil)
                }
            }.disabled(state.busy)

            HStack(spacing: 10) {
                if controller.busy { ProgressView().controlSize(.small) }
                Text(controller.status).font(.system(size: 12, weight: .medium))
                Spacer()
                if let backlog = controller.snapshot?.asr_backlog_seconds, controller.busy, backlog > 2 {
                    Text("待识别 \(Int(backlog)) 秒").font(.system(size: 12)).foregroundStyle(.orange)
                }
                if let seconds = controller.snapshot?.received_audio_seconds {
                    Text(AppState.time(seconds)).monospacedDigit().font(.system(size: 12))
                }
            }
            if let message = controller.error ?? controller.snapshot?.asr_error {
                Text(message).font(.system(size: 12)).foregroundStyle(.red).textSelection(.enabled).lineLimit(5)
            }
            if let parent = controller.snapshot?.parent_session_path {
                HStack {
                    Text(controller.snapshot?.parent_asr_complete == false ? "录后校对版 · 原任务识别曾中断，仅处理已保存音频" : "录后校对版 · 原始字幕保留，可回看比较")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("查看原版") { controller.loadSession(URL(fileURLWithPath: parent)) }.disabled(controller.busy)
                }
            }

            Card {
                if controller.snapshot?.segments.isEmpty != false && controller.snapshot?.pending_english?.isEmpty != false {
                    VStack(spacing: 12) {
                        Image(systemName: "waveform").font(.system(size: 35)).foregroundStyle(.secondary)
                        Text(controller.snapshot?.input_kind == "refinement" && controller.busy ? "正在校对完整录音" : controller.busy ? "正在准备连续识别" : "英文先显示，中文随后补齐").font(.headline)
                        Text("录音和确认的英文会保存到本机任务目录。\n中途关闭后，可继续识别已保存录音或补译。")
                            .font(.system(size: 13)).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }.frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 0) {
                            ForEach(controller.snapshot?.segments ?? []) { segment in
                                HStack(alignment: .top, spacing: 16) {
                                    Button { controller.play(from: segment.start) } label: {
                                        VStack(spacing: 5) {
                                            Image(systemName: "play.circle")
                                            Text(AppState.time(segment.start)).monospacedDigit()
                                        }.font(.system(size: 11)).frame(width: 50)
                                    }.buttonStyle(.plain).help("从此处播放保存的录音")
                                    VStack(alignment: .leading, spacing: 8) {
                                        Text(segment.english).font(.system(size: 13)).foregroundStyle(.secondary).lineSpacing(4)
                                        if let chinese = segment.chinese {
                                            Text(chinese).font(.system(size: 15)).lineSpacing(5)
                                        } else {
                                            Text(translationLabel(segment)).font(.system(size: 12)).foregroundStyle(segment.translation_state == "failed" ? .orange : .secondary)
                                        }
                                        HStack {
                                            if segment.boundary == "length_limit" { Text("长句暂分，核对上下文").font(.caption2).foregroundStyle(.secondary) }
                                            Spacer()
                                            Button("修改英文") { editing = segment; editedText = segment.english }
                                                .buttonStyle(.link).font(.system(size: 11)).disabled(controller.busy)
                                        }
                                    }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                                }.padding(16)
                                Divider().padding(.leading, 80)
                            }
                            if let pending = controller.snapshot?.pending_english, !pending.isEmpty {
                                VStack(alignment: .leading, spacing: 6) {
                                    Text("正在组句 · 英文已保存").font(.caption).foregroundStyle(.secondary)
                                    Text(pending).font(.system(size: 13)).textSelection(.enabled)
                                }.padding(16)
                            }
                        }
                    }
                }
            }

            if let total = controller.snapshot?.segment_count, total > 200 {
                HStack {
                    let offset = controller.snapshot?.segment_offset ?? 0
                    Button("较早字幕") { controller.showPage(offset: max(0, offset-200), resourceRoot: state.resourceRoot) }
                        .disabled(offset == 0)
                    Text("显示 \(offset+1)–\(min(total, offset+(controller.snapshot?.segments.count ?? 0))) / \(total) 段")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("较新字幕") { controller.showPage(offset: min(max(0, total-200), offset+200), resourceRoot: state.resourceRoot) }
                        .disabled(offset+200 >= total)
                    Button("跟随最新") { controller.showPage(offset: nil, resourceRoot: state.resourceRoot) }
                    Spacer()
                }
            }

            HStack {
                Text(controller.countDescription).font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                if controller.isPlaying { Button("暂停回放") { controller.pausePlayback() } }
                Picker("倍速", selection: $controller.playbackRate) {
                    Text("0.75×").tag(Float(0.75)); Text("1×").tag(Float(1)); Text("1.25×").tag(Float(1.25)); Text("1.5×").tag(Float(1.5))
                }.frame(width: 115)
                Button("继续识别") { controller.resumeASR(resourceRoot: state.resourceRoot) }
                    .disabled(controller.busy || state.busy || controller.snapshot?.audio_path == nil || controller.snapshot?.asr_complete == true)
                Button("继续补译 / 重试失败") { controller.retry(resourceRoot: state.resourceRoot) }
                    .disabled(controller.busy || state.busy || controller.folder == nil)
                Button("录后重新校对") { controller.refine(resourceRoot: state.resourceRoot) }
                    .disabled(controller.busy || state.busy || controller.snapshot?.audio_path == nil)
                    .help("使用完整录音重新识别并翻译，生成独立任务，保留当前字幕。")
            }
            HStack {
                Text("任务保存在本机资料库。时间戳为估计值，课堂内容和译文仍需核对。")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                if let folder = controller.folder { Button("打开任务目录") { NSWorkspace.shared.open(folder) }.buttonStyle(.link) }
                Button("原生引擎对照") { showLegacy = true }.buttonStyle(.link).disabled(controller.busy)
            }
        }
        .sheet(item: $editing) { segment in
            VStack(alignment: .leading, spacing: 15) {
                Text("修改第 \(segment.id) 段英文").font(.headline)
                TextEditor(text: $editedText).frame(minWidth: 620, minHeight: 180)
                Text("保存后旧译文会失效；点击“继续补译”生成新译文。原始版本仍保留在任务数据库中。")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Spacer(); Button("取消") { editing = nil }
                    Button("保存修改") { controller.revise(segment, text: editedText, resourceRoot: state.resourceRoot); editing = nil }
                        .buttonStyle(.borderedProminent).disabled(editedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }.padding(22)
        }
        .sheet(isPresented: $showLegacy) {
            VStack { HStack { Text("WhisperKit 原生引擎对照").font(.headline); Spacer(); Button("关闭") { showLegacy = false } }; LegacyAudioWorkspace() }
                .padding(20).frame(minWidth: 960, minHeight: 660)
        }
    }

    private func translationLabel(_ segment: StreamingSegment) -> String {
        switch segment.translation_state {
        case "running": return "正在翻译…"
        case "failed": return "翻译失败：\(segment.translation_error ?? "可稍后重试")"
        case "disabled": return "本次仅转写英文"
        default: return "等待翻译…"
        }
    }
    private func chooseInput() {
        let panel = NSOpenPanel(); panel.title = "选择英语音频或视频"
        panel.allowedContentTypes = [.audio, .movie]; panel.allowsMultipleSelection = false
        if panel.runModal() == .OK { input = panel.url }
    }
}
