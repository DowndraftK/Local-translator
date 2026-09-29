import SwiftUI
import PDFKit
import TranslatorCore

struct DocumentWorkspace: View {
    @EnvironmentObject var state: AppState
    @State private var selectedPage: Int?
    @State private var showPDF = false
    @State private var confirmReview = false
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Picker("流程", selection: $state.wholeDocument) {
                    Text("PDF / TXT 整篇").tag(true)
                    Text("旧提取 / OCR / Office 选段").tag(false)
                }.pickerStyle(.segmented).disabled(state.busy || state.documentTask.hasDrafts)
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
                Button("选择文件") { state.chooseDocument() }.disabled(state.busy || state.documentTask.hasDrafts)
                Text(state.documentURL?.lastPathComponent ?? "PDF 或 UTF-8 TXT").lineLimit(1)
                Spacer()
                DirectionPicker().disabled(state.documentTask.hasDrafts)
            }
            if state.documentTask.isPDF {
                Picker("提取模式", selection: $state.documentMode) {
                    Text("文字层（默认，不 OCR）").tag(DocumentExtractionMode.text)
                    Text("显式整页 OCR（需核对）").tag(DocumentExtractionMode.ocr)
                }.pickerStyle(.segmented).disabled(state.busy || state.documentTask.hasDrafts)
            }
            HStack {
                Text(state.documentTask.isPDF ? "PDF 共 \(state.documentTask.totalPages) 页" : "TXT 全文（无 PDF 页码）")
                if state.documentTask.isPDF {
                    Toggle("全部页", isOn: $state.allDocumentPages).toggleStyle(.checkbox).disabled(state.busy || state.documentTask.hasDrafts)
                    if !state.allDocumentPages {
                        TextField("起页", text: $state.rangeFirst).frame(width: 55).disabled(state.busy || state.documentTask.hasDrafts)
                        Text("至")
                        TextField("末页", text: $state.rangeLast).frame(width: 55).disabled(state.busy || state.documentTask.hasDrafts)
                    }
                }
                Button(state.documentMode == .ocr && state.documentTask.isPDF ? "识别所选范围" : "提取预览") { state.extractWholeDocument() }.disabled(state.busy || state.documentTask.hasDrafts || state.documentTask.data == nil)
                Spacer()
                if state.busy { Button("停止") { state.cancel() } }
                Button(state.documentTask.translator.job == nil ? "翻译所选范围" : "从头重新翻译") { state.translateWholeDocument() }
                    .disabled(!state.documentTask.canStart)
                    .buttonStyle(.borderedProminent)
            }.font(.system(size: 12))
            Text("每轮最多 200 页；范围从 1 开始使用 PDF 物理页码。修改范围后点击提取预览；重新提取会替换当前结果。")
                .font(.caption).foregroundStyle(.secondary)
            if state.documentTask.hasDrafts {
                Text("第 \(state.documentTask.drafts.keys.sorted().map(String.init).joined(separator: "、")) 页有未保存编辑；切页保留草稿。请保存校正或取消编辑后再翻译、换文件或切换设置。")
                    .font(.caption).foregroundStyle(.orange)
            }
            if let error = state.documentTask.error { Text(error).foregroundStyle(.red).textSelection(.enabled) }
            if let snapshot = state.documentTask.snapshot {
                Text("本轮：\(snapshot.isPDF ? snapshot.mode.label : "UTF-8") · \(snapshot.isPDF ? snapshot.range.label + " 页" : "TXT 全文") · \(snapshot.direction) · \(snapshot.model) · \(snapshot.phase)").font(.caption)
                Text(snapshot.coverage).font(.caption)
                if let issue = snapshot.issue { Text(issue).font(.caption).foregroundStyle(.orange) }
                if let job = state.documentTask.translator.job {
                    Text(job.summary.replacingOccurrences(of: "全部翻译完成", with: snapshot.completionLabel)).font(.caption)
                }
                HStack(spacing: 10) {
                    List(selection: $selectedPage) {
                        ForEach(snapshot.pages) { page in
                            VStack(alignment: .leading) {
                                Text(snapshot.isPDF ? "第 \(page.id) 页" : "TXT 全文")
                                Text(page.state.label).font(.caption).foregroundStyle(.secondary)
                                if snapshot.mode == .ocr { Text("\(page.sourceLabel) · \(page.reviewed ? "已核对" : "未核对")\(state.documentTask.drafts[page.id] != nil ? " · 草稿" : "")").font(.caption) }
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
                            if snapshot.mode == .ocr { reviewControls(page) }
                            if showPDF, snapshot.isPDF, let data = state.documentTask.data {
                                DocumentPDFPreview(data: data, fingerprint: snapshot.fingerprint, page: page.id).frame(minHeight: 150, maxHeight: 280)
                            }
                            if let draft = state.documentTask.drafts[page.id] {
                                TextEditor(text: Binding(get: { state.documentTask.drafts[page.id] ?? draft }, set: { state.documentTask.updateDraft(page: page.id, text: $0) }))
                                    .font(.body).frame(minHeight: 160).disabled(state.documentTask.busy)
                            } else {
                                ScrollView {
                                    LazyVStack(alignment: .leading, spacing: 18) {
                                        Text(page.state.label).foregroundStyle(.secondary)
                                        if let issue = page.issue { Text(issue).foregroundStyle(.orange) }
                                        if snapshot.mode == .ocr {
                                            Text("\(page.sourceLabel) · 有效修订 \(page.revision.uuidString)").font(.caption)
                                            if page.edited {
                                                DisclosureGroup("查看保留的原始 OCR") { Text(page.ocr?.text ?? "未获得完整页 OCR").textSelection(.enabled) }
                                            }
                                            if let evidence = page.ocr {
                                                DisclosureGroup("识别证据（\(evidence.pixelWidth)×\(evidence.pixelHeight) px）") {
                                                    Text(evidence.coordinateSystem).font(.caption)
                                                    ForEach(evidence.observations) { item in
                                                        Text("#\(item.order) · 置信度 \(item.confidence, specifier: "%.2f") · \(item.text)").font(.caption).textSelection(.enabled)
                                                    }
                                                }
                                            }
                                        }
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
                    Text(state.documentTask.hasDrafts ? "导出仅包含已保存版本，不含编辑草稿。" : "结果仅保留于当前窗口；退出不恢复。").font(.caption).foregroundStyle(.secondary)
                }
            } else {
                Spacer()
                Text(state.documentTask.extracting ? "正在后台读取文件…" : "选择文件后设置范围，先提取预览，再启动翻译。")
                Spacer()
            }
            Text(state.documentTask.isPDF && state.documentMode == .ocr ? DocumentOCR.limitations : DocumentSnapshot.limitations).font(.caption).foregroundStyle(.secondary)
        }
    }
    private func reviewControls(_ page: DocumentPageRecord) -> some View {
        HStack {
            if state.documentTask.snapshot?.translationStarted == true {
                Button("重新核对并开始新一轮") { confirmReview = true }.disabled(state.busy)
                    .alert("清除当前译文并重新核对？", isPresented: $confirmReview) {
                        Button("清除译文，重新核对", role: .destructive) { state.documentTask.reopenReview() }
                        Button("取消", role: .cancel) {}
                    } message: { Text("保留原始 OCR 和当前校正文；下一次翻译从头开始，旧译文不会对应新原文。") }
            } else if state.documentTask.drafts[page.id] != nil {
                Button("保存校正") { state.documentTask.saveEdit(page: page.id) }.disabled(state.busy)
                Button("取消编辑") { state.documentTask.cancelEdit(page: page.id) }.disabled(state.busy)
            } else {
                Button("编辑本页原文") { state.documentTask.beginEdit(page: page.id) }.disabled(!state.documentTask.canEdit)
            }
            Toggle("已人工核对", isOn: Binding(get: { page.reviewed }, set: { state.documentTask.setReviewed(page: page.id, reviewed: $0) }))
                .toggleStyle(.checkbox).disabled(state.busy || state.documentTask.drafts[page.id] != nil)
        }.font(.caption)
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
