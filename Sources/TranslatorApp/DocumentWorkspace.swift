import SwiftUI
import PDFKit
import TranslatorCore

struct DocumentWorkspace: View {
    @EnvironmentObject var state: AppState
    @State private var selectedPage: Int?
    @State private var showPDF = false
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Picker("流程", selection: $state.wholeDocument) {
                    Text("PDF / TXT 整篇").tag(true)
                    Text("旧提取 / OCR / Office 选段").tag(false)
                }.pickerStyle(.segmented).disabled(state.busy)
            }
            if state.wholeDocument { wholeBody } else { LegacyDocumentWorkspace() }
        }
        .onChange(of: state.documentTask.snapshot?.id) { _, _ in
            selectedPage = state.documentTask.snapshot?.range.first
        }
        .onChange(of: state.wholeDocument) { _, whole in
            if whole, let url = state.documentURL { state.documentTask.load(url) }
            else { state.documentTask.clear() }
        }
    }
    private var wholeBody: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Button("选择文件") { state.chooseDocument() }.disabled(state.busy)
                Text(state.documentURL?.lastPathComponent ?? "文字型 PDF 或 UTF-8 TXT").lineLimit(1)
                Spacer()
                DirectionPicker()
            }
            HStack {
                Text(state.documentTask.isPDF ? "PDF 共 \(state.documentTask.totalPages) 页" : "TXT 全文（无 PDF 页码）")
                if state.documentTask.isPDF {
                    Toggle("全部页", isOn: $state.allDocumentPages).toggleStyle(.checkbox).disabled(state.busy)
                    if !state.allDocumentPages {
                        TextField("起页", text: $state.rangeFirst).frame(width: 55).disabled(state.busy)
                        Text("至")
                        TextField("末页", text: $state.rangeLast).frame(width: 55).disabled(state.busy)
                    }
                }
                Button("提取预览") { state.extractWholeDocument() }.disabled(state.busy || state.documentTask.data == nil)
                Spacer()
                if state.busy { Button("停止") { state.cancel() } }
                Button(state.documentTask.translator.job == nil ? "翻译所选范围" : "从头重新翻译") { state.translateWholeDocument() }
                    .disabled(state.busy || state.documentTask.snapshot?.segments.isEmpty != false)
                    .buttonStyle(.borderedProminent)
            }.font(.system(size: 12))
            Text("每轮最多 200 页；范围从 1 开始使用 PDF 物理页码。修改范围后点击提取预览；重新提取会替换当前结果。")
                .font(.caption).foregroundStyle(.secondary)
            if let error = state.documentTask.error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            if let snapshot = state.documentTask.snapshot {
                Text("本轮：\(snapshot.isPDF ? snapshot.range.label + " 页" : "TXT 全文") · \(snapshot.direction) · \(snapshot.model) · \(snapshot.phase)").font(.caption)
                Text(snapshot.coverage).font(.caption)
                if let issue = snapshot.issue { Text(issue).font(.caption).foregroundStyle(.orange) }
                if let job = state.documentTask.translator.job {
                    Text(job.summary.replacingOccurrences(of: "全部翻译完成", with: "所选范围的可提取文字翻译完成")).font(.caption)
                }
                HStack(spacing: 10) {
                    List(selection: $selectedPage) {
                        ForEach(snapshot.pages) { page in
                            VStack(alignment: .leading) {
                                Text(snapshot.isPDF ? "第 \(page.id) 页" : "TXT 全文")
                                Text(page.state.label).font(.caption).foregroundStyle(.secondary)
                            }.tag(page.id)
                        }
                    }.frame(width: 175)
                    VStack(alignment: .leading) {
                        if let page = snapshot.pages.first(where: { $0.id == selectedPage }) {
                            HStack {
                                Text(snapshot.isPDF ? "PDF 物理页 \(page.id)" : "TXT 原文范围").font(.headline)
                                Spacer()
                                if snapshot.isPDF { Toggle("查看原 PDF 对应页", isOn: $showPDF).toggleStyle(.button) }
                            }
                            if showPDF, snapshot.isPDF, let data = state.documentTask.data {
                                DocumentPDFPreview(data: data, fingerprint: snapshot.fingerprint, page: page.id)
                            } else {
                                ScrollView {
                                    LazyVStack(alignment: .leading, spacing: 18) {
                                        Text(page.state.label).foregroundStyle(.secondary)
                                        if let issue = page.issue { Text(issue).foregroundStyle(.orange) }
                                        let sources = snapshot.sources.filter { $0.page == page.id }
                                        if sources.isEmpty { Text(page.text).textSelection(.enabled) }
                                        ForEach(sources, id: \.segmentID) { mapping in
                                            let planned = snapshot.segments[mapping.segmentID]
                                            let translated = state.documentTask.translator.job?.segments.first { $0.id == mapping.segmentID }
                                            VStack(alignment: .leading, spacing: 8) {
                                                Text("第 \(mapping.paragraph) 段 · UTF-8 [\(mapping.utf8Start), \(mapping.utf8End))").font(.caption).foregroundStyle(.secondary)
                                                Text(planned.source).textSelection(.enabled)
                                                Divider()
                                                Text(translated?.state == .completed ? translated?.result?.translation ?? "" : translated?.state.label ?? "尚未翻译")
                                                    .textSelection(.enabled)
                                                if let error = translated?.error { Text(error).foregroundStyle(.red) }
                                                ForEach(translated?.result?.warnings ?? [], id: \.self) { Text($0).font(.caption).foregroundStyle(.orange) }
                                            }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                                                .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
                                        }
                                    }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                                }.id(page.id)
                            }
                        }
                    }
                }.frame(maxHeight: .infinity)
                HStack {
                    Button("复制已有译文") { state.exportDocumentTranslation(copyOnly: true) }
                        .disabled((state.documentTask.translator.job?.count(.completed) ?? 0) == 0)
                    Button("导出双语 TXT") { state.exportDocumentTranslation() }
                    Text("结果仅保留于当前窗口；退出不恢复。").font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Spacer()
                Text(state.documentTask.extracting ? "正在后台读取文件…" : "选择文件后设置范围，先提取预览，再启动翻译。")
                Spacer()
            }
            Text(DocumentSnapshot.limitations).font(.caption).foregroundStyle(.secondary)
        }
    }
}

/// UI PDFKit objects belong to the main thread and are never shared with extraction.
private struct DocumentPDFPreview: NSViewRepresentable {
    let data: Data
    let fingerprint: String
    let page: Int
    final class Coordinator { var fingerprint = ""; var page = 0 }
    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeNSView(context: Context) -> PDFView {
        let view = PDFView(); view.autoScales = true; view.displayMode = .singlePageContinuous
        return view
    }
    func updateNSView(_ view: PDFView, context: Context) {
        if context.coordinator.fingerprint != fingerprint {
            view.document = PDFDocument(data: data); context.coordinator.fingerprint = fingerprint
            context.coordinator.page = 0
        }
        if context.coordinator.page != page, let target = view.document?.page(at: page - 1) {
            view.go(to: target); context.coordinator.page = page
        }
    }
}
