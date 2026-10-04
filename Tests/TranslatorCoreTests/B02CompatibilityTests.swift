import Foundation
import Testing
@testable import TranslatorCore

private var b02Root: URL {
    URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("fixtures/b02")
}
private struct SplitCase: Codable {
    var name: String
    var source: String
    var limit: Int
    var segments: [TranslationSegment]
}
private struct FrozenSplits: Codable {
    var schema = 1
    var origin = "production Swift LongTextSplitter / Foundation CharacterSet; self-authored"
    var cases: [SplitCase]
    var trims: [String: String]
}

@Test func b02FrozenProductionSplitsAndTrimSemantics() throws {
    let samples: [(String, String, Int)] = [
        ("english", String(repeating: "The team saved 12 local notes. Review them before Friday.\n", count: 130), 2048),
        ("chinese", String(repeating: "这是本地保存验证。请在周五之前核对12条记录。\r\n", count: 80), 2048),
        ("crlf", "one\r\ntwo\r\nthree\r\n", 7),
        ("graphemes", String(repeating: "👨‍👩‍👧‍👦 e\u{301} 🇨🇳 👍🏽 中文。 ", count: 7), 67),
        ("whitespace", String(repeating: " \t\r\n", count: 1200) + "saved text" + String(repeating: " \n", count: 1500), 2048),
        ("no-spaces", String(repeating: "abcdef", count: 800), 2048),
        ("unicode-lines", "甲\u{2028}乙\u{2029}丙\n丁。", 9)
    ]
    var trims: [String: String] = [:]
    for value in ["\u{200b}test\u{200b}", "\u{001c}test\u{001c}", "\u{0085}test\u{0085}", "\u{00a0}test\u{00a0}", "\u{3000}test\u{3000}", "\r\ntest\r\n"] {
        trims[value] = value.trimmingCharacters(in: .whitespacesAndNewlines)
    }
    let cases = try samples.map { name, source, limit in
        SplitCase(name: name, source: source, limit: limit, segments: try LongTextSplitter.split(source, byteLimit: limit))
    }
    let bytes = try JSONOutput.encode(FrozenSplits(cases: cases, trims: trims))
    if let output = ProcessInfo.processInfo.environment["B02_GENERATE_FIXTURES"] {
        let target = URL(fileURLWithPath: output)
        try FileManager.default.createDirectory(at: target, withIntermediateDirectories: false)
        try bytes.write(to: target.appendingPathComponent("swift-splits.json"))
    } else {
        #expect(try Data(contentsOf: b02Root.appendingPathComponent("swift-splits.json")) == bytes)
    }
    do {
        _ = try LongTextSplitter.split("e" + String(repeating: "\u{301}", count: 1500))
        Issue.record("Oversized grapheme accepted")
    } catch {}
}

@Test func b02SharedSchemaOneIsReadableByProductionSwift() async throws {
    let path = ProcessInfo.processInfo.environment["B02_SHARED_SNAPSHOTS"]
        .map { URL(fileURLWithPath: $0) } ?? b02Root.appendingPathComponent("shared-task")
    let id = UUID(uuidString: "11111111-2222-4333-8444-555555555555")!
    let store = TaskRecoveryStore(root: path)
    let task = try await store.read(id).task
    #expect(task.revision == 3)
    #expect(task.configuration.version == 1 && task.configuration.contextVersion == nil)
    #expect(task.configuration.binding == "f09724ccc5643c119b4077b0a8754f51f557d5fa93b49216ccb726b8ba5ddc89")
    #expect(task.text?.count(.completed) == 1)
    #expect(task.text?.segments[0].result?.translation == "自编夹具已保存结果。")
    #expect(task.drafts[2] == "未应用的自编草稿")
    #expect(try await store.sourceData(task).1 == Data(task.input.utf8))
    // A second shared snapshot is a new v2 task with real JSON date/optional fields.
    let newID = UUID(uuidString: "22222222-3333-4444-8555-666666666666")!
    let new = try await store.read(newID).task
    #expect(new.configuration.version == 2 && new.text?.count(.completed) == 1)
    #expect(new.configuration.binding == "c19bbf5bbe70bd67b17b65ae6457f7d1bf16ddec7a1c9ea1f64961bc5c907edc")
    #expect(new.text?.segments[0].result?.translation == "共享夹具结果。")
}
