import Foundation

public enum M0Error: LocalizedError {
    case invalid(String)
    case unavailable(String)
    case incomplete(String)
    public var errorDescription: String? {
        switch self {
        case .invalid(let s), .unavailable(let s), .incomplete(let s): return s
        }
    }
}

public struct SourceRange: Codable, Equatable {
    public var page: Int?
    public var element: String?
    public var rect: [Double]?
    public var startSeconds: Double?
    public var endSeconds: Double?
    public init(page: Int? = nil, element: String? = nil, rect: [Double]? = nil,
                startSeconds: Double? = nil, endSeconds: Double? = nil) {
        self.page = page; self.element = element; self.rect = rect
        self.startSeconds = startSeconds; self.endSeconds = endSeconds
    }
}

public struct TextBlock: Codable {
    public var id: String
    public var text: String
    public var kind: String
    public var source: SourceRange
    public init(id: String, text: String, kind: String = "paragraph", source: SourceRange) {
        self.id = id; self.text = text; self.kind = kind; self.source = source
    }
}

public struct ImportedDocument: Codable {
    public var file: String
    public var method: String
    public var blocks: [TextBlock]
    public var warnings: [String]
    public var layoutEvidenceJSON: String? = nil
}

public struct GlossaryTerm: Codable {
    public var source: String
    public var target: String
    public init(source: String, target: String) { self.source = source; self.target = target }
}

public struct TranslationRecord: Codable {
    public var source: String
    public var translation: String
    public var model: String
    public var direction: String
    public var elapsedSeconds: Double
    public var firstTextSeconds: Double?
    public var outputTokens: Int?
    public var warnings: [String]
    public var promptProfile: String? = nil
    public var modelLoadSeconds: Double? = nil
    public var promptTokens: Int? = nil
    public var promptEvaluationSeconds: Double? = nil
    public var generationSeconds: Double? = nil
}

public enum ContentChecks {
    /// Heuristic review hints only. Chinese written-out numbers may trigger false positives.
    public static func warnings(source: String, target: String, glossary: [GlossaryTerm]) -> [String] {
        let pattern = #"\d+(?:[.,]\d+)*(?:%)?"#
        let regex = try! NSRegularExpression(pattern: pattern)
        func numbers(_ text: String) -> [String] {
            let ns = text as NSString
            return regex.matches(in: text, range: NSRange(location: 0, length: ns.length))
                .map { ns.substring(with: $0.range).replacingOccurrences(of: ",", with: "") }.sorted()
        }
        var result: [String] = []
        if numbers(source) != numbers(target) { result.append("数字形式或数量有变化，请人工核对；这不代表已判定误译。") }
        for term in glossary where source.localizedCaseInsensitiveContains(term.source) && !target.contains(term.target) {
            result.append("偏好术语未出现：\(term.source) → \(term.target)")
        }
        if target.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { result.append("译文为空。") }
        return result
    }
}

public enum JSONOutput {
    public static func encode<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(value)
    }
    public static func write<T: Encodable>(_ value: T, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try encode(value).write(to: url, options: .atomic)
    }
}
