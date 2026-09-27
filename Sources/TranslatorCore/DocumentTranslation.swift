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
    case pending, extracted, noText, failed, stopped
    public var label: String {
        switch self {
        case .pending: return "待提取"
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
}
public struct DocumentSource: Codable {
    public let segmentID: Int
    public let page: Int
    public let paragraph: Int
    public let utf8Start: Int
    public let utf8End: Int
}
public struct DocumentSnapshot: Identifiable, Codable {
    public let id: UUID
    public let file: String
    public let fingerprint: String
    public let totalPages: Int
    public let isPDF: Bool
    public let range: DocumentPageRange
    public let model: String
    public let direction: String
    public var pages: [DocumentPageRecord]
    public var sources: [DocumentSource] = []
    public var segments: [TranslationSegment] = []
    public var phase = "正在提取"
    public var issue: String?
    public static let limitations = "仅处理文字层，不执行 OCR；未提取到文字可能是空白、扫描、图片或提取问题。有文字也不保证图片中文字已覆盖。复杂双栏、表格、公式及阅读顺序需核对；不跨页拼句，不猜测断词，不删除重复或页眉页脚。"
    public var coverage: String {
        "提取覆盖：所选 \(pages.count) \(isPDF ? "页" : "份文本") · 有文字 \(pages.filter { $0.state == .extracted }.count) · 无文字 \(pages.filter { $0.state == .noText }.count) · 失败 \(pages.filter { $0.state == .failed }.count) · 未处理 \(pages.filter { $0.state == .pending || $0.state == .stopped }.count)"
    }
    public func export(translation: TextTranslationJob?) -> String {
        var out = "文档双语对照\n文件：\(file)\n文件 SHA-256：\(fingerprint)\n总页数：\(isPDF ? String(totalPages) : "不适用（TXT）")\n选定范围：\(isPDF ? range.label : "全文")\n模型：\(model)\n方向：\(direction)\n提取方式：\(isPDF ? "PDFKit 文字层" : "UTF-8")\n任务：\(phase)\n\(coverage)\n"
        out += "\(translation?.summary.replacingOccurrences(of: "全部翻译完成", with: "所选范围的可提取文字翻译完成") ?? "尚未翻译")\n\(Self.limitations)\n范围外页面未选入本次任务。结果仅保留于当前窗口；退出不恢复。\n"
        if let issue { out += "提示：\(issue)\n" }
        if let error = translation?.error { out += "翻译提示：\(error)\n" }
        for page in pages {
            out += "\n===== \(isPDF ? "PDF 物理页 \(page.id)" : "TXT 全文") · \(page.state.label) =====\n"
            if let label = page.label { out += "文档页标签：\(label)\n" }
            if let issue = page.issue { out += "提取提示：\(issue)\n" }
            out += "[完整提取原文]\n\(page.text)\n[对应译文]\n"
            let mapping = sources.filter { $0.page == page.id }
            if mapping.isEmpty { out += page.state == .extracted ? "【尚未分段或翻译；已提取原文完整保留】\n" : "【未获得可翻译片段；\(page.state.label)】\n" }
            for source in mapping {
                let segment = translation?.segments.first { $0.id == source.segmentID }
                out += "第 \(source.paragraph) 段 · 原文 UTF-8 [\(source.utf8Start), \(source.utf8End))\n"
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
        for page in pages where page.state == .extracted {
            // Group consecutive same-page lines into bounded passages, retaining every
            // byte. PDFKit provides no reliable paragraph semantics; no dehyphenation.
            let pieces = try LongTextSplitter.split(page.text)
            guard pieces.map(\.source).joined() == page.text else { throw M0Error.invalid("提取后原文范围校验失败。") }
            for (index, piece) in pieces.enumerated() {
                mapping.append(DocumentSource(segmentID: plan.count, page: page.id, paragraph: index + 1,
                    utf8Start: piece.utf8Start, utf8End: piece.utf8End))
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
    @Published public private(set) var snapshot: DocumentSnapshot?
    @Published public private(set) var data: Data?
    @Published public private(set) var totalPages = 0
    @Published public private(set) var isPDF = true
    @Published public private(set) var extracting = false
    @Published public private(set) var error: String?
    public let translator = TextTranslationController()
    private var observation: AnyCancellable?
    private var operation: Task<Void, Never>?
    private var generation = UUID()
    private var reader = DocumentTextReader()
    private var url: URL?
    private var fingerprint = ""
    public init() {
        observation = translator.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
    }
    public var busy: Bool { extracting || translator.busy }
    public func clear() {
        stop(); generation = UUID(); snapshot = nil; data = nil; totalPages = 0; error = nil; url = nil
        translator.clear()
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
    public func extract(first: Int, last: Int, model: String, direction: String) {
        guard !busy, let url, data != nil else { return }
        do {
            let range = try DocumentPageRange(first: first, last: last, total: totalPages)
            translator.clear(); generation = UUID(); let id = generation
            error = nil; extracting = true
            snapshot = DocumentSnapshot(id: id, file: url.lastPathComponent, fingerprint: fingerprint,
                totalPages: totalPages, isPDF: isPDF, range: range, model: model, direction: direction,
                pages: (first...last).map { DocumentPageRecord(id: $0) })
            operation = Task {
                do {
                    let warning = await reader.sourceWarning(url, fingerprint: fingerprint)
                    guard generation == id, !Task.isCancelled else { return }
                    snapshot?.issue = warning
                    for number in first...last {
                        let page = try await reader.page(number)
                        guard generation == id, !Task.isCancelled else { return }
                        snapshot?.pages[number - first] = page
                    }
                    guard let captured = snapshot else { return }
                    let preparation = Task.detached { () throws -> DocumentSnapshot in
                        var ready = captured; try ready.prepare(); return ready
                    }
                    var ready = try await withTaskCancellationHandler { try await preparation.value } onCancel: { preparation.cancel() }
                    guard generation == id, !Task.isCancelled else { return }
                    ready.phase = ready.segments.isEmpty ? "没有可翻译的文字" : "提取完成，等待翻译"
                    snapshot = ready; extracting = false; operation = nil
                } catch {
                    guard generation == id, !Task.isCancelled else { return }
                    snapshot?.phase = "提取或分段失败"; snapshot?.issue = error.localizedDescription
                    self.error = error.localizedDescription; extracting = false; operation = nil
                }
            }
        } catch { self.error = error.localizedDescription }
    }
    public func start(translate: @escaping TextTranslationController.Translate) {
        guard !busy, let snapshot, !snapshot.segments.isEmpty,
              snapshot.pages.allSatisfy({ $0.state != .pending && $0.state != .stopped }) else { return }
        self.snapshot?.phase = "提取快照已固定；翻译状态见下方"
        let id = generation, reader = reader, url = url
        translator.start(source: "", model: snapshot.model, direction: snapshot.direction, translate: { [weak self] input, model, direction in
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
        operation?.cancel(); operation = nil
        if extracting {
            generation = UUID(); extracting = false; snapshot?.phase = "提取已停止，请重新提取后翻译"
            if let pages = snapshot?.pages {
                for i in pages.indices where pages[i].state == .pending { snapshot?.pages[i].state = .stopped }
            }
        }
        translator.stop()
    }
}
