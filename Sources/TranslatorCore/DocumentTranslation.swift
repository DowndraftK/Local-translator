import Foundation
import Combine
import PDFKit
import CryptoKit

public struct DocumentPageRange: Codable, Equatable {
    public let first: Int
    public let last: Int
    public init(first: Int, last: Int, total: Int) throws {
        guard first >= 1, last >= first, last <= total, last - first < 200 else {
            throw M0Error.invalid("请输入从 1 开始的连续物理页码，范围须在文件内且每轮最多 200 页；大文件请指定范围，例如 201–220。")
        }
        self.first = first; self.last = last
    }
    public var label: String { "\(first)–\(last)" }
}
public enum DocumentPageState: String, Codable {
    case pending, recognizing, extracted, noText, failed, stopped
    public var label: String {
        switch self {
        case .pending: return "待提取"
        case .recognizing: return "识别中"
        case .extracted: return "已提取文字"
        case .noText: return "未提取到文字（请查看原页）"
        case .failed: return "读取失败"
        case .stopped: return "停止未处理"
        }
    }
}
public struct DocumentPageRecord: Identifiable, Codable {
    public var id: Int
    public var label: String?
    public var text = ""
    public var state: DocumentPageState = .pending
    public var issue: String?
    public var mode: DocumentExtractionMode = .text
    public var ocr: OCRPageEvidence?
    public var revision = UUID()
    public var edited = false
    public var reviewed = false
    public var sourceLabel: String {
        guard mode == .ocr else { return "文字层" }
        if edited {
            if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "人工清空" }
            return (ocr?.text ?? "").isEmpty ? "人工输入（非 OCR 覆盖）" : "人工校正"
        }
        return "原始 OCR"
    }
}
public struct DocumentSource: Codable {
    public let segmentID: Int
    public let page: Int
    public let paragraph: Int
    public let utf8Start: Int
    public let utf8End: Int
    public let revision: UUID
}
public struct DocumentSnapshot: Identifiable, Codable {
    public var id: UUID
    public let file: String
    public let fingerprint: String
    public var originalPath: String? = nil
    public var extractionProfile: String? = "pdfkit-vision-v1"
    public var savedOCRSettings: String? = DocumentOCR.settings
    public let totalPages: Int
    public let isPDF: Bool
    public let range: DocumentPageRange
    public let model: String
    public let direction: String
    public var pages: [DocumentPageRecord]
    public var sources: [DocumentSource] = []
    public var segments: [TranslationSegment] = []
    public var mode: DocumentExtractionMode = .text
    public var translationStarted = false
    public var phase = "正在提取"
    public var issue: String?
    public static let limitations = "仅处理文字层，不执行 OCR；未提取到文字可能是空白、扫描、图片或提取问题。有文字也不保证图片中文字已覆盖。复杂双栏、表格、公式及阅读顺序需核对；不跨页拼句，不猜测断词，不删除重复或页眉页脚。"
    public var sourceKey: String { id.uuidString + pages.map { $0.revision.uuidString }.joined() }
    public var completionLabel: String { mode == .ocr ? "本轮有效原文翻译完成（非 OCR 准确性保证）" : "所选范围的可提取文字翻译完成" }
    public var modeLimitations: String { mode == .ocr ? DocumentOCR.limitations : Self.limitations }
    public var coverage: String {
        "\(mode == .ocr ? "识别进度" : "提取覆盖")：所选 \(pages.count) \(isPDF ? "页" : "份文本") · 已完成提取 \(pages.filter { $0.state == .extracted || $0.state == .noText }.count) · 有文字 \(pages.filter { $0.state == .extracted }.count) · 无文字 \(pages.filter { $0.state == .noText }.count) · 失败 \(pages.filter { $0.state == .failed }.count) · 未处理 \(pages.filter { $0.state == .pending || $0.state == .stopped || $0.state == .recognizing }.count)"
    }
    public func export(translation candidate: TextTranslationJob?) -> String {
        let translation = candidate?.source == sourceKey ? candidate : nil
        var out = "文档双语对照\n文件：\(file)\n文件 SHA-256：\(fingerprint)\n总页数：\(isPDF ? String(totalPages) : "不适用（TXT）")\n选定范围：\(isPDF ? range.label : "全文")\n模型：\(model)\n方向：\(direction)\n提取方式：\(isPDF ? mode.label : "UTF-8")\n任务：\(phase)\n\(coverage)\n"
        out += "\(translation?.summary.replacingOccurrences(of: "全部翻译完成", with: completionLabel) ?? "尚未翻译")\n\(modeLimitations)\n范围外页面未选入本次任务。自动保存状态请查看应用；本文件是导出时已确认的有效修订。\n"
        if mode == .ocr { out += (savedOCRSettings ?? DocumentOCR.settings) + "\n" }
        if let issue { out += "提示：\(issue)\n" }
        if let error = translation?.error { out += "翻译提示：\(error)\n" }
        for page in pages {
            out += "\n===== \(isPDF ? "PDF 物理页 \(page.id)" : "TXT 全文") · \(page.state.label) =====\n"
            if let label = page.label { out += "文档页标签：\(label)\n" }
            if let issue = page.issue { out += "提取提示：\(issue)\n" }
            if mode == .ocr {
                out += "来源：\(page.sourceLabel) · \(page.reviewed ? "已人工核对当前修订" : "未人工核对当前修订")\n有效原文修订：\(page.revision)\n"
                if let evidence = page.ocr {
                    out += "OCR 语言优先级：\(evidence.languages.joined(separator: ", "))\n渲染：\(evidence.pixelWidth)×\(evidence.pixelHeight) px；rotation=\(evidence.rotation)；cropBox=\(evidence.cropBox)\n坐标：\(evidence.coordinateSystem)\n"
                    out += "[原始 OCR\(page.edited ? "（保留证据）" : "＝有效原文")]\n\(evidence.text)\n"
                    out += "[OCR 观察项证据：顺序 / ID / 置信度 / 坐标 / 文字]\n"
                    for observation in evidence.observations {
                        out += "\(observation.order) / \(observation.id) / \(observation.confidence) / \(observation.rect) / \(observation.text)\n"
                    }
                } else { out += "[原始 OCR]\n【未获得完整页识别结果】\n" }
                if page.edited { out += "[当前有效原文（\(page.sourceLabel)，非原 PDF 完整性声明）]\n\(page.text)\n" }
                out += "[对应译文；范围指向上述有效原文修订]\n"
            } else { out += "[完整提取原文]\n\(page.text)\n[对应译文]\n" }
            let mapping = sources.filter { $0.page == page.id }
            if mapping.isEmpty { out += page.edited && page.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "【人工清空；无可翻译文字】\n" : page.state == .extracted ? "【尚未分段或翻译；已提取原文完整保留】\n" : "【未获得可翻译片段；\(page.state.label)】\n" }
            for source in mapping {
                let segment = translation?.segments.first { $0.id == source.segmentID }
                out += "第 \(source.paragraph) 段 · 修订 \(source.revision) · 原文 UTF-8 [\(source.utf8Start), \(source.utf8End))\n"
                if let segment, segment.state == .completed, let result = segment.result {
                    out += result.translation + "\n"
                    out += result.warnings.map { "核对提示：\($0)\n" }.joined()
                } else {
                    out += "【\(segment?.state.label ?? "尚未翻译")】\(segment?.error.map { " 原因：\($0)" } ?? "")\n"
                }
            }
        }
        return out
    }
    public mutating func prepare() throws {
        var plan: [TranslationSegment] = [], mapping: [DocumentSource] = []
        for page in pages where !page.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            try Task.checkCancellation()
            // Group consecutive same-page lines into bounded passages, retaining every
            // byte. PDFKit provides no reliable paragraph semantics; no dehyphenation.
            let pieces = try LongTextSplitter.split(page.text)
            guard pieces.map(\.source).joined() == page.text else { throw M0Error.invalid("提取后原文范围校验失败。") }
            for (index, piece) in pieces.enumerated() {
                mapping.append(DocumentSource(segmentID: plan.count, page: page.id, paragraph: index + 1,
                    utf8Start: piece.utf8Start, utf8End: piece.utf8End, revision: page.revision))
                plan.append(TranslationSegment(id: plan.count, utf8Start: piece.utf8Start, utf8End: piece.utf8End, source: piece.source))
            }
        }
        segments = plan; sources = mapping
    }
}

/// All extraction PDFKit objects are confined to this actor. The UI creates an
/// independent PDFDocument from immutable bytes, never from the mutable source URL.
public actor DocumentTextReader {
    private var pdf: PDFDocument?
    private var text: String?
    private var sourceSize: Int?
    private var sourceModified: Date?
    public init() {}
    public func load(_ url: URL) throws -> (Data, Int, Bool, String) {
        guard (try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0) <= 256_000_000 else {
            throw M0Error.invalid("单文件上限为 256 MB。")
        }
        let identity = try url.resourceValues(forKeys: [.fileSizeKey, .contentModificationDateKey])
        sourceSize = identity.fileSize; sourceModified = identity.contentModificationDate
        let data = try Data(contentsOf: url)
        guard data.count <= 256_000_000 else { throw M0Error.invalid("单文件上限为 256 MB。") }
        let isPDF = url.pathExtension.lowercased() == "pdf"
        let count: Int
        if isPDF {
            guard let document = PDFDocument(data: data), !document.isLocked, document.pageCount > 0 else {
                throw M0Error.invalid("PDF 无法读取、损坏或已锁定；请提供可读取的副本。")
            }
            pdf = document; text = nil; count = document.pageCount
        } else {
            guard url.pathExtension.lowercased() == "txt", let decoded = String(data: data, encoding: .utf8) else {
                throw M0Error.invalid("整篇流程仅支持文字型 PDF 和 UTF-8 TXT。")
            }
            text = decoded.hasPrefix("\u{feff}") ? String(decoded.dropFirst()) : decoded
            pdf = nil; count = 1
        }
        return (data, count, isPDF, SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined())
    }
    public func sourceWarning(_ url: URL, fingerprint: String) -> String? {
        // URL caches resource values; use a fresh URL to observe replacement/deletion.
        var currentURL = URL(fileURLWithPath: url.path)
        currentURL.removeAllCachedResourceValues()
        guard FileManager.default.isReadableFile(atPath: currentURL.path),
              let identity = try? currentURL.resourceValues(forKeys: [.fileSizeKey, .contentModificationDateKey]) else {
            return "源文件已无法读取；本轮继续使用载入时的原文和 PDF 预览快照。"
        }
        return identity.fileSize == sourceSize && identity.contentModificationDate == sourceModified ? nil :
            "源文件大小或修改时间已变化；本轮使用载入时快照。若要处理新文件，请重新选择文件。"
    }
    public func ocrPage(_ number: Int, cancellation: OCRCancellation, direction: String = "en-zh") throws -> DocumentPageRecord {
        try cancellation.check()
        guard let page = pdf?.page(at: number - 1) else { throw M0Error.invalid("无法读取此物理页。") }
        return try DocumentOCR.recognize(page, number: number, cancellation: cancellation, direction: direction)
    }
    public func page(_ number: Int) throws -> DocumentPageRecord {
        try Task.checkCancellation()
        var result = DocumentPageRecord(id: number)
        if let pdf {
            guard let page = pdf.page(at: number - 1) else {
                result.state = .failed; result.issue = "无法读取此物理页，未获得原文。"; return result
            }
            result.text = page.string ?? ""; result.label = page.label
        } else { result.text = text ?? "" }
        result.state = result.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? .noText : .extracted
        return result
    }
}

@MainActor public final class DocumentTranslationController: ObservableObject {
    public typealias OCR = (Int, OCRCancellation) async throws -> DocumentPageRecord
    public typealias Prepare = (DocumentSnapshot) async throws -> DocumentSnapshot
    public var changed: (() -> Void)?
    public var checkpoint: (() async throws -> Void)?
    public var beforeFork: (() -> Void)?
    @Published public private(set) var snapshot: DocumentSnapshot? { didSet { changed?() } }
    @Published public private(set) var data: Data?
    @Published public private(set) var totalPages = 0
    @Published public private(set) var isPDF = true
    @Published public private(set) var extracting = false
    @Published public private(set) var preparing = false
    @Published public private(set) var error: String?
    @Published public private(set) var drafts: [Int: String] = [:] { didSet { changed?() } }
    public let translator = TextTranslationController()
    private var observation: AnyCancellable?
    private var operation: Task<Void, Never>?
    private var generation = UUID()
    private var reader = DocumentTextReader()
    private var cancellation: OCRCancellation?
    private let injectedOCR: OCR?
    private let prepare: Prepare
    private var url: URL?
    private var fingerprint = ""
    public init(ocr: OCR? = nil, prepare: Prepare? = nil) {
        injectedOCR = ocr
        self.prepare = prepare ?? { captured in
            let task = Task.detached { () throws -> DocumentSnapshot in
                var ready = captured; try ready.prepare(); return ready
            }
            return try await withTaskCancellationHandler { try await task.value } onCancel: { task.cancel() }
        }
        observation = translator.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
    }
    public var busy: Bool { extracting || preparing || translator.busy }
    public var hasDrafts: Bool { !drafts.isEmpty }
    public var canEdit: Bool { !busy && snapshot?.mode == .ocr && snapshot?.translationStarted == false }
    public var canStart: Bool {
        !busy && !hasDrafts && data != nil && snapshot?.segments.isEmpty == false &&
        snapshot?.pages.allSatisfy { $0.state != .pending && $0.state != .recognizing && $0.state != .stopped } == true
    }
    public func clear() {
        stop(); generation = UUID(); snapshot = nil; data = nil; totalPages = 0; error = nil; url = nil
        drafts = [:]; translator.clear()
    }
    public func load(_ url: URL) {
        clear(); self.url = url; extracting = true
        let id = generation, reader = DocumentTextReader(); self.reader = reader
        operation = Task {
            do {
                let loaded = try await reader.load(url)
                guard generation == id, !Task.isCancelled else { return }
                data = loaded.0; totalPages = loaded.1; isPDF = loaded.2; fingerprint = loaded.3
                extracting = false; operation = nil
            } catch {
                guard generation == id, !Task.isCancelled else { return }
                self.error = error.localizedDescription; extracting = false; operation = nil
            }
        }
    }
    public func extract(first: Int, last: Int, model: String, direction: String, mode: DocumentExtractionMode = .text) {
        guard !busy, !hasDrafts, let url, data != nil else { return }
        do {
            let range = try DocumentPageRange(first: first, last: last, total: totalPages)
            let effectiveMode: DocumentExtractionMode = isPDF ? mode : .text
            translator.clear(); generation = UUID(); let id = generation
            let token = OCRCancellation(); cancellation = token
            let reader = reader
            error = nil; extracting = true
            snapshot = DocumentSnapshot(id: id, file: url.lastPathComponent, fingerprint: fingerprint,
                totalPages: totalPages, isPDF: isPDF, range: range, model: model, direction: direction,
                pages: (first...last).map { var page = DocumentPageRecord(id: $0); page.mode = effectiveMode; return page }, mode: effectiveMode)
            snapshot?.originalPath = url.path
            operation = Task { await processPages(id: id, reader: reader, url: url, token: token) }
        } catch { self.error = error.localizedDescription }
    }
    public var canContinueExtraction: Bool {
        !busy && !hasDrafts && data != nil && snapshot != nil && translator.job == nil &&
        (snapshot?.segments.isEmpty == true || snapshot?.pages.contains { [.pending, .stopped, .failed, .recognizing].contains($0.state) } == true)
    }
    public func restore(snapshot saved: DocumentSnapshot, translation: TextTranslationJob?, drafts: [Int: String], assetURL: URL?) async {
        clear(); generation = UUID(); snapshot = saved; self.drafts = drafts
        translator.restore(translation)
        totalPages = saved.totalPages; isPDF = saved.isPDF; fingerprint = saved.fingerprint
        for i in saved.pages.indices where [.pending, .recognizing].contains(saved.pages[i].state) { snapshot?.pages[i].state = .stopped }
        snapshot?.phase = "已打开保存任务；请主动继续未完成部分"
        if let assetURL {
            do {
                let loaded = try await reader.load(assetURL)
                guard loaded.3 == saved.fingerprint else { throw M0Error.invalid("源副本指纹不匹配。") }
                data = loaded.0; url = assetURL
            } catch { self.error = error.localizedDescription }
        } else { error = "源副本无法确认；原文与译文保留并可导出，暂不能继续处理或回查原页。" }
    }
    public func continueExtraction() {
        guard canContinueExtraction, let url else { return }
        guard snapshot?.extractionProfile == nil || snapshot?.extractionProfile == "pdfkit-vision-v1" else {
            error = "此任务的提取/OCR配置版本不兼容；保留数据并可导出，请新建任务。"; return
        }
        generation = UUID(); let id = generation, token = OCRCancellation(); cancellation = token
        extracting = true; error = nil
        operation = Task { await processPages(id: id, reader: reader, url: url, token: token) }
    }
    private func processPages(id: UUID, reader: DocumentTextReader, url: URL, token: OCRCancellation) async {
        guard let captured = snapshot else { return }
        do {
            snapshot?.issue = await reader.sourceWarning(url, fingerprint: captured.fingerprint)
            try await checkpoint?()
            guard generation == id, !Task.isCancelled else { return }
            for i in captured.pages.indices where ![.extracted, .noText].contains(captured.pages[i].state) {
                guard generation == id, !Task.isCancelled else { return }
                snapshot?.pages[i].state = .recognizing
                try await checkpoint?()
                guard generation == id, !Task.isCancelled else { return }
                do {
                    let page: DocumentPageRecord
                    let number = captured.pages[i].id
                    if captured.mode == .ocr {
                        if let injectedOCR { page = try await injectedOCR(number, token) }
                        else { page = try await reader.ocrPage(number, cancellation: token, direction: captured.direction) }
                    } else { page = try await reader.page(number) }
                    guard generation == id, !Task.isCancelled, !token.isCancelled else { return }
                    snapshot?.pages[i] = page
                } catch {
                    guard generation == id, !Task.isCancelled, !token.isCancelled else { return }
                    snapshot?.pages[i].state = .failed; snapshot?.pages[i].issue = error.localizedDescription
                }
                try await checkpoint?()
                guard generation == id, !Task.isCancelled else { return }
            }
            extracting = false; cancellation = nil
            await rebuild(id: id)
        } catch {
            guard generation == id, !Task.isCancelled else { return }
            stop(); self.error = "保存失败，已停止文档处理；内存原文可导出：" + error.localizedDescription
        }
    }
    private func rebuild(id: UUID) async {
        guard let captured = snapshot, generation == id else { return }
        preparing = true; snapshot?.phase = "正在后台重建有效原文分段"
        do {
            var ready = try await prepare(captured)
            guard generation == id, !Task.isCancelled else { return }
            ready.phase = ready.segments.isEmpty ? "没有可翻译的有效原文" : "提取/校正计划已就绪，等待翻译"
            snapshot = ready
            try await checkpoint?()
            guard generation == id, !Task.isCancelled else { return }
            preparing = false; operation = nil
        } catch {
            guard generation == id, !Task.isCancelled else { return }
            snapshot?.segments = []; snapshot?.sources = []
            snapshot?.phase = "分段失败；原始结果与有效原文仍可导出"
            self.error = error.localizedDescription; preparing = false; operation = nil
        }
    }
    public func beginEdit(page: Int) {
        guard canEdit, let record = snapshot?.pages.first(where: { $0.id == page }) else { return }
        if drafts[page] == nil { drafts[page] = record.text }
    }
    public func updateDraft(page: Int, text: String) { guard canEdit, drafts[page] != nil else { return }; drafts[page] = text }
    public func cancelEdit(page: Int) { drafts.removeValue(forKey: page) }
    public func saveEdit(page: Int) {
        guard canEdit, let text = drafts[page], let index = snapshot?.pages.firstIndex(where: { $0.id == page }) else { return }
        snapshot?.pages[index].text = text
        snapshot?.pages[index].edited = true
        snapshot?.pages[index].reviewed = false
        snapshot?.pages[index].revision = UUID()
        drafts.removeValue(forKey: page)
        translator.clear(); snapshot?.segments = []; snapshot?.sources = []
        generation = UUID(); let id = generation
        preparing = true; error = nil
        operation = Task { await rebuild(id: id) }
    }
    public func setReviewed(page: Int, reviewed: Bool) {
        guard !busy, drafts[page] == nil, let index = snapshot?.pages.firstIndex(where: { $0.id == page }) else { return }
        snapshot?.pages[index].reviewed = reviewed
    }
    /// UI confirms that translations will be cleared before invoking this action.
    public func reopenReview() {
        guard !busy, !hasDrafts, snapshot?.mode == .ocr else { return }
        beforeFork?()
        generation = UUID(); translator.clear(); snapshot?.id = UUID(); snapshot?.translationStarted = false
        snapshot?.phase = "重新核对；旧任务另存，原始 OCR 与当前校正文保留，下一轮从头翻译"
    }
    public func forkTranslation() {
        guard !busy, translator.job != nil else { return }
        beforeFork?(); generation = UUID(); translator.clear(); snapshot?.id = UUID()
    }
    public func start(translate: @escaping TextTranslationController.Translate) {
        guard canStart, let snapshot else { return }
        self.snapshot?.translationStarted = true
        self.snapshot?.phase = "本轮来源及修订已固定；翻译状态见下方"
        let id = generation, reader = reader, url = url
        translator.start(source: snapshot.sourceKey, model: snapshot.model, direction: snapshot.direction, translate: { [weak self] input, model, direction in
            if let url {
                let warning = await reader.sourceWarning(url, fingerprint: snapshot.fingerprint)
                guard let self, self.generation == id, !Task.isCancelled else { throw CancellationError() }
                if let warning { self.snapshot?.issue = warning }
            }
            try Task.checkCancellation()
            let result = try await translate(input, model, direction)
            if let url {
                let warning = await reader.sourceWarning(url, fingerprint: snapshot.fingerprint)
                guard let self, self.generation == id, !Task.isCancelled else { throw CancellationError() }
                if let warning { self.snapshot?.issue = warning }
            }
            return result
        }, preparedSegments: snapshot.segments)
    }
    public func stop() {
        cancellation?.cancel(); cancellation = nil
        operation?.cancel(); operation = nil
        if extracting || preparing {
            generation = UUID(); extracting = false; preparing = false
            snapshot?.phase = "提取/分段已停止；已获结果保留。当前页已请求取消，可能仍在等待底层返回；不会接受迟到结果。可主动继续未完成页。"
            snapshot?.segments = []; snapshot?.sources = []
            if let pages = snapshot?.pages {
                for i in pages.indices where pages[i].state == .pending || pages[i].state == .recognizing { snapshot?.pages[i].state = .stopped }
            }
        }
        translator.stop()
    }
}
