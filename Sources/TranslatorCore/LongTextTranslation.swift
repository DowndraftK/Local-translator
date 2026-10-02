import Combine
import Foundation
import NaturalLanguage

/// Conservative estimates, not tokenizer counts. A UTF-8 byte is charged as one
/// source token; the output reserve allows twice that amount in either direction.
/// The fixed prompt and safety reserves also cover chat framing and estimation error.
public enum TranslationBudget {
    public static let contextTokens = 8192
    public static let outputTokens = 4096
    public static let promptReserve = 1024
    public static let safetyReserve = 512
    public static let sourceBytes = min(outputTokens / 2,
        contextTokens - outputTokens - promptReserve - safetyReserve)

    public static func validate(source: String, promptBytes: Int) throws {
        guard !source.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              source.utf8.count <= sourceBytes, promptBytes + 64 <= promptReserve else {
            throw M0Error.invalid("单次请求超出保守容量预算（原文最多 \(sourceBytes) UTF-8 字节，另保留提示、译文及余量）；请分段或缩短术语提示。")
        }
    }
}

public enum TextSegmentState: String, Codable {
    case pending, processing, completed, failed, stopped
    public var label: String {
        switch self {
        case .pending: return "尚未翻译"
        case .processing: return "处理中（尚未完成）"
        case .completed: return "已完成"
        case .failed: return "翻译失败"
        case .stopped: return "已停止（本段中断）"
        }
    }
}

public struct TranslationSegment: Identifiable, Codable {
    public let id: Int
    /// Half-open UTF-8 byte offsets into the original input, including whitespace.
    public let utf8Start: Int
    public let utf8End: Int
    public let source: String
    public var state: TextSegmentState = .pending
    public var result: TranslationRecord?
    public var error: String?
    public var requestText: String { source.trimmingCharacters(in: .whitespacesAndNewlines) }
}

public enum LongTextSplitter {
    public static func split(_ source: String, byteLimit: Int = TranslationBudget.sourceBytes) throws -> [TranslationSegment] {
        guard byteLimit > 0 else { throw M0Error.invalid("分段容量必须大于零。") }
        let tokenizer = NLTokenizer(unit: .sentence)
        tokenizer.string = source
        var sentences = Set<String.Index>()
        tokenizer.enumerateTokens(in: source.startIndex..<source.endIndex) { range, _ in
            sentences.insert(range.upperBound)
            return !Task.isCancelled
        }
        try Task.checkCancellation()
        var pieces: [String] = []
        var pendingWhitespace = ""
        var start = source.startIndex
        while start < source.endIndex {
            try Task.checkCancellation()
            var cursor = start, bytes = 0
            var paragraph: String.Index?, sentence: String.Index?, word: String.Index?
            while cursor < source.endIndex {
                let next = source.index(after: cursor)
                let character = source[cursor]
                let size = source[cursor..<next].utf8.count
                if bytes + size > byteLimit { break }
                bytes += size; cursor = next
                if character.isNewline { paragraph = cursor }
                if sentences.contains(cursor) { sentence = cursor }
                if character.isWhitespace { word = cursor }
            }
            // An arbitrarily large grapheme (e.g. thousands of combining marks)
            // cannot be split intact and fit a request. Fail explicitly, retaining input.
            guard cursor > start else { throw M0Error.invalid("单个 Unicode 字符超出模型容量，无法在不破坏字符的情况下分段。原文仍可导出。") }
            let end = cursor == source.endIndex ? cursor : (paragraph ?? sentence ?? word ?? cursor)
            let piece = String(source[start..<end])
            if piece.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                pendingWhitespace += piece
            } else {
                pieces.append(pendingWhitespace + piece)
                pendingWhitespace = ""
            }
            start = end
        }
        if !pendingWhitespace.isEmpty, !pieces.isEmpty { pieces[pieces.count - 1] += pendingWhitespace }
        var offset = 0
        return pieces.enumerated().map { index, text in
            let end = offset + text.utf8.count
            defer { offset = end }
            return TranslationSegment(id: index, utf8Start: offset, utf8End: end, source: text)
        }
    }
}

public enum TextJobPhase: String, Codable {
    case preparing, running, completed, partial, stopped, failed
}

public struct TextTranslationJob: Identifiable, Codable {
    public let id: UUID
    public let source: String
    public let model: String
    public let direction: String
    public var segments: [TranslationSegment] = []
    public var attempts: [Int: Int]? = nil
    public var phase: TextJobPhase = .preparing
    public var error: String?
    public init(id: UUID, source: String, model: String, direction: String) {
        self.id = id; self.source = source; self.model = model; self.direction = direction
    }
    public var busy: Bool { phase == .preparing || phase == .running }
    public func count(_ state: TextSegmentState) -> Int { segments.filter { $0.state == state }.count }
    public var summary: String {
        let prefix: String
        switch phase {
        case .preparing: prefix = "正在分段"
        case .running: prefix = "正在翻译"
        case .completed: prefix = "全部翻译完成"
        case .partial: prefix = "部分翻译失败"
        case .stopped: prefix = "已停止"
        case .failed: prefix = "任务未完成"
        }
        return "\(prefix) · 共 \(segments.count) 段 · 已完成 \(count(.completed)) · 处理中 \(count(.processing)) · 待处理 \(count(.pending)) · 失败 \(count(.failed)) · 中断 \(count(.stopped))"
    }
    public var completedTranslation: String {
        segments.compactMap { $0.state == .completed ? $0.result?.translation : nil }.joined(separator: "\n\n")
    }
    public var bilingualText: String {
        var output = "文字翻译 · 双语对照\n方向：\(direction)\n模型：\(model)\n\(summary)\n"
        output += "本文件为导出时已确认状态的快照；自动保存状态请查看应用。完成不代表准确率保证，请核对原文。\n"
        if let error { output += "任务提示：\(error)\n" }
        if segments.isEmpty {
            output += "\n[完整原文]\n\(source)\n\n[译文]\n【尚未翻译\(phase == .stopped ? " · 已停止" : "")】\n"
        }
        for segment in segments {
            output += "\n===== 第 \(segment.id + 1) 段 · 原文 UTF-8 [\(segment.utf8Start), \(segment.utf8End)) =====\n[原文]\n\(segment.source)\n[译文]\n"
            if segment.state == .completed, let result = segment.result {
                output += result.translation + "\n"
                for warning in result.warnings { output += "核对提示：\(warning)\n" }
            } else {
                output += "【\(segment.state.label)】\n"
                if let error = segment.error { output += "原因：\(error)\n" }
            }
        }
        return output
    }
}

/// Each run has a separate generation, including resuming the same saved UUID.
@MainActor public final class TextTranslationController: ObservableObject {
    public typealias Translate = (String, String, String) async throws -> TranslationRecord
    public var changed: (() -> Void)?
    public var checkpoint: ((TextTranslationJob) async throws -> Void)?
    @Published public private(set) var job: TextTranslationJob? { didSet { changed?() } }
    private var operation: Task<Void, Never>?
    private var generation = UUID()
    public init() {}
    public var busy: Bool { job?.busy == true }
    public var canResume: Bool { !busy && (job?.segments.isEmpty == true && job?.source.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false || job?.segments.contains { $0.state == .pending || $0.state == .stopped || ($0.state == .failed && (job?.attempts?[$0.id] ?? 0) < 3) } == true) }
    public func clear() { stop(); generation = UUID(); job = nil }
    public func restore(_ saved: TextTranslationJob?) {
        stop(); generation = UUID(); job = saved
        if job?.busy == true { job?.phase = .stopped }
        if let parts = job?.segments {
            for i in parts.indices where parts[i].state == .processing { job?.segments[i].state = .stopped }
        }
    }
    public func stop() {
        guard busy else { return }
        generation = UUID(); operation?.cancel(); operation = nil
        job?.phase = .stopped
        if let index = job?.segments.firstIndex(where: { $0.state == .processing }) {
            job?.segments[index].state = .stopped
        }
    }
    public func start(source: String, model: String, direction: String, translate: @escaping Translate,
                      preparedSegments: [TranslationSegment]? = nil, taskID: UUID = UUID()) {
        stop(); generation = UUID(); let run = generation
        job = TextTranslationJob(id: taskID, source: source, model: model, direction: direction)
        if let preparedSegments { job?.segments = preparedSegments }
        operation = Task { [weak self] in
            let preparation = Task.detached(priority: .userInitiated) { try preparedSegments ?? LongTextSplitter.split(source) }
            do {
                let segments = try await withTaskCancellationHandler { try await preparation.value } onCancel: { preparation.cancel() }
                guard let self, self.isCurrent(run) else { return }
                guard !segments.isEmpty, ["en-zh", "zh-en"].contains(direction) else {
                    throw M0Error.invalid("请输入非空原文并选择英中或中英方向。")
                }
                self.job?.segments = segments
                await self.run(run, translate: translate)
            } catch {
                guard let self, self.isCurrent(run) else { return }
                self.job?.phase = .failed; self.job?.error = error.localizedDescription
                self.operation = nil
            }
        }
    }
    public func resume(translate: @escaping Translate) {
        guard canResume else { return }
        generation = UUID(); let run = generation
        job?.phase = .running; job?.error = nil
        operation = Task {
            if let captured = job, captured.segments.isEmpty {
                let preparation = Task.detached { try LongTextSplitter.split(captured.source) }
                do {
                    let segments = try await withTaskCancellationHandler { try await preparation.value } onCancel: { preparation.cancel() }
                    guard isCurrent(run) else { return }
                    job?.segments = segments
                } catch {
                    guard isCurrent(run) else { return }
                    job?.phase = .failed; job?.error = error.localizedDescription; operation = nil; return
                }
            }
            await self.run(run, translate: translate)
        }
    }
    private func persist(_ run: UUID) async throws {
        guard isCurrent(run), let snapshot = job else { throw CancellationError() }
        do { try await checkpoint?(snapshot) }
        catch {
            guard isCurrent(run) else { throw CancellationError() }
            job?.phase = .failed
            job?.error = "保存失败，已停止继续调度。内存结果可导出：" + error.localizedDescription
            operation = nil
            throw error
        }
        guard isCurrent(run) else { throw CancellationError() }
    }
    private func run(_ run: UUID, translate: @escaping Translate) async {
        guard isCurrent(run), let captured = job else { return }
        job?.phase = .running
        do {
            try await persist(run)
            for segment in captured.segments where segment.state != .completed && (segment.state != .failed || (captured.attempts?[segment.id] ?? 0) < 3) {
                guard isCurrent(run) else { return }
                if job?.attempts == nil { job?.attempts = [:] }
                job?.attempts?[segment.id] = (job?.attempts?[segment.id] ?? 0) + 1
                job?.segments[segment.id].state = .processing
                job?.segments[segment.id].error = nil
                try await persist(run)
                do {
                    let result = try await translate(segment.requestText, captured.model, captured.direction)
                    guard isCurrent(run) else { return }
                    guard result.source == segment.requestText, result.model == captured.model, result.direction == captured.direction,
                          !result.translation.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                        throw M0Error.incomplete("返回结果与本段输入不匹配或译文为空。")
                    }
                    job?.segments[segment.id].result = result
                    job?.segments[segment.id].state = .completed
                } catch {
                    guard isCurrent(run) else { return }
                    if error is CancellationError || (error as? URLError)?.code == .cancelled { stop(); return }
                    job?.segments[segment.id].state = .failed
                    job?.segments[segment.id].error = error.localizedDescription
                    if Self.stopsScheduling(error) {
                        var saved = job!
                        saved.phase = .failed; saved.error = error.localizedDescription
                        try await checkpoint?(saved)
                        guard isCurrent(run) else { return }
                        job = saved; operation = nil; return
                    }
                }
                // Successful result and completed status share one durable snapshot.
                try await persist(run)
            }
            guard isCurrent(run) else { return }
            var saved = job!
            saved.phase = saved.count(.failed) == 0 ? .completed : .partial
            try await checkpoint?(saved)
            guard isCurrent(run) else { return }
            job = saved; operation = nil
        } catch {
            guard generation == run, !Task.isCancelled else { return }
            job?.phase = .failed
            job?.error = "任务未完成（保存或准备失败）：" + error.localizedDescription
            operation = nil
        }
    }
    private func isCurrent(_ run: UUID) -> Bool { generation == run && busy && !Task.isCancelled }
    private static func stopsScheduling(_ error: Error) -> Bool {
        if error is URLError { return true }
        if let error = error as? M0Error {
            switch error { case .unavailable, .invalid: return true; case .incomplete: return false }
        }
        return false
    }
}
