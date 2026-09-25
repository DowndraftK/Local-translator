import Foundation
import Testing
@testable import TranslatorCore

private func record(_ source: String, _ model: String, _ direction: String) -> TranslationRecord {
    TranslationRecord(source: source, translation: "译文：" + source, model: model, direction: direction,
        elapsedSeconds: 0, warnings: ["核对提示样本"])
}

@Test func longTextReconstructsExactBytesAndRanges() throws {
    let paragraph = "Dr. Smith paid 3.14 dollars. Visit https://example.org/a.b?q=3.14. 👨‍👩‍👧‍👦 e\u{301} 中文。\r\n\r\n"
    let input = " \n" + String(repeating: paragraph, count: 130) + "\n \t"
    let parts = try LongTextSplitter.split(input)
    #expect(input.count > 8000)
    #expect(parts.count > 1)
    #expect(Array(parts.map(\.source).joined().utf8) == Array(input.utf8))
    var cursor = 0
    let original = Array(input.utf8)
    for part in parts {
        #expect(part.utf8Start == cursor)
        #expect(Array(part.source.utf8) == Array(original[part.utf8Start..<part.utf8End]))
        #expect(part.requestText.utf8.count <= TranslationBudget.sourceBytes)
        #expect(!part.requestText.isEmpty)
        cursor = part.utf8End
    }
    #expect(cursor == input.utf8.count)
    #expect(parts.map(\.source).joined().components(separatedBy: "Dr. Smith").count == 131)
}

@Test func noPunctuationUnicodeAndWhitespaceAreSafe() throws {
    for input in [String(repeating: "无标点👨‍👩‍👧‍👦e\u{301}", count: 3000), String(repeating: "a", count: 17000),
                  String(repeating: "\n ", count: 6000) + "word" + String(repeating: "\t", count: 5000)] {
        let parts = try LongTextSplitter.split(input)
        #expect(Array(parts.map(\.source).joined().utf8) == Array(input.utf8))
        #expect(parts.allSatisfy { !$0.requestText.isEmpty && $0.requestText.utf8.count <= TranslationBudget.sourceBytes })
        for part in parts { #expect(input.range(of: part.requestText) != nil) }
    }
    #expect(try LongTextSplitter.split(" \n\t").isEmpty)
    #expect(throws: (any Error).self) { try LongTextSplitter.split("e" + String(repeating: "\u{301}", count: 2000)) }
}

@Test func paragraphSentenceAndWordBoundariesArePreferred() throws {
    let paragraphs = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    let split = try LongTextSplitter.split(paragraphs, byteLimit: 40)
    #expect(split.first?.source == "First paragraph.\n\nSecond paragraph.\n\n")
    let sentence = "Dr. Smith paid 3.14 dollars. Visit https://example.org/a.b today. Another sentence follows."
    let parts = try LongTextSplitter.split(sentence, byteLimit: 67)
    #expect(parts.first?.source.contains("Visit https://example.org/a.b today.") == true)
    #expect(parts.map(\.source).joined() == sentence)
}

@Test func capacityReservesPromptOutputAndMargin() throws {
    #expect(TranslationBudget.sourceBytes + TranslationBudget.promptReserve + TranslationBudget.outputTokens
            + TranslationBudget.safetyReserve <= TranslationBudget.contextTokens)
    #expect(TranslationBudget.sourceBytes * 2 <= TranslationBudget.outputTokens)
    try TranslationBudget.validate(source: String(repeating: "a", count: TranslationBudget.sourceBytes), promptBytes: 600)
    #expect(throws: (any Error).self) { try TranslationBudget.validate(source: String(repeating: "中", count: 1000), promptBytes: 0) }
    #expect(throws: (any Error).self) { try TranslationBudget.validate(source: "short", promptBytes: 1024) }
}

@MainActor private final class ControlledTranslator {
    var calls: [(String, String, String)] = []
    var continuations: [CheckedContinuation<TranslationRecord, Error>] = []
    func translate(_ source: String, _ model: String, _ direction: String) async throws -> TranslationRecord {
        calls.append((source, model, direction))
        return try await withCheckedThrowingContinuation { continuations.append($0) }
    }
    func finish(_ index: Int, error: Error? = nil) {
        if let error { continuations[index].resume(throwing: error) }
        else { let c = calls[index]; continuations[index].resume(returning: record(c.0, c.1, c.2)) }
    }
}

@MainActor private func eventually(_ predicate: () -> Bool) async throws {
    let deadline = Date().addingTimeInterval(8)
    while !predicate(), Date() < deadline { try await Task.sleep(nanoseconds: 1_000_000) }
    try #require(predicate())
}

private let threeParts = String(repeating: "a", count: 2048) + "\n" + String(repeating: "b", count: 2048) + "\nlast"

@MainActor @Test func serialProgressPartialFailureAndExport() async throws {
    let service = ControlledTranslator(), controller = TextTranslationController()
    controller.start(source: threeParts, model: "local", direction: "en-zh", translate: service.translate)
    try await eventually { service.calls.count == 1 }
    #expect(controller.job?.count(.processing) == 1)
    #expect(controller.job?.count(.pending) == 2)
    service.finish(0)
    try await eventually { service.calls.count == 2 }
    #expect(controller.job?.count(.completed) == 1)
    #expect(controller.job?.completedTranslation == "译文：" + service.calls[0].0)
    let inFlight = try #require(controller.job)
    #expect(inFlight.bilingualText.contains("处理中（尚未完成）"))
    #expect(inFlight.bilingualText.contains("尚未翻译"))
    service.finish(1, error: M0Error.incomplete("输出截断"))
    try await eventually { service.calls.count == 3 }
    service.finish(2)
    try await eventually { !controller.busy }
    let job = try #require(controller.job)
    #expect(job.phase == .partial)
    #expect(job.count(.completed) == 2 && job.count(.failed) == 1)
    #expect(job.segments.map(\.source).joined() == threeParts)
    #expect(!job.summary.contains("全部翻译完成"))
    #expect(job.bilingualText.contains("【翻译失败】\n原因：输出截断"))
    #expect(job.bilingualText.components(separatedBy: "[原文]").count == 4)
    #expect(job.bilingualText.contains("核对提示样本"))
    #expect(job.completedTranslation.contains("last"))
}

@MainActor @Test func stopPreservesCompletedAndLateCallbacksCannotTouchNewJob() async throws {
    let old = ControlledTranslator(), next = ControlledTranslator(), controller = TextTranslationController()
    controller.start(source: threeParts, model: "old-model", direction: "en-zh", translate: old.translate)
    try await eventually { old.calls.count == 1 }; old.finish(0)
    try await eventually { old.calls.count == 2 }
    controller.stop()
    let stopped = try #require(controller.job)
    #expect(stopped.phase == .stopped)
    #expect(stopped.count(.completed) == 1 && stopped.count(.stopped) == 1 && stopped.count(.pending) == 1)
    #expect(stopped.bilingualText.contains("【已停止（本段中断）】"))
    #expect(stopped.bilingualText.contains("【尚未翻译】"))
    #expect(stopped.segments.map(\.source).joined() == threeParts)
    controller.start(source: "新文章", model: "new-model", direction: "zh-en", translate: next.translate)
    try await eventually { next.calls.count == 1 }
    let id = controller.job?.id
    old.finish(1) // Deliberately ignores cancellation and returns a normal result late.
    next.finish(0)
    try await eventually { !controller.busy }
    #expect(old.calls.count == 2)
    #expect(controller.job?.id == id)
    #expect(controller.job?.phase == .completed)
    #expect(controller.job?.completedTranslation == "译文：新文章")
    #expect(controller.job?.model == "new-model" && controller.job?.direction == "zh-en")
}

@MainActor @Test func serviceFailureLeavesPendingAndNoTranslationCanStillExport() async throws {
    let service = ControlledTranslator(), controller = TextTranslationController()
    controller.start(source: threeParts, model: "local", direction: "en-zh", translate: service.translate)
    try await eventually { service.calls.count == 1 }
    service.finish(0, error: M0Error.unavailable("服务不可用"))
    try await eventually { !controller.busy }
    let job = try #require(controller.job)
    #expect(service.calls.count == 1 && job.phase == .failed)
    #expect(job.count(.failed) == 1 && job.count(.pending) == 2)
    #expect(job.completedTranslation.isEmpty)
    #expect(job.bilingualText.contains("服务不可用"))
    #expect(job.bilingualText.components(separatedBy: "[原文]").count == 4)
    controller.start(source: threeParts, model: "local", direction: "en-zh", translate: service.translate)
    controller.stop() // During preparation, before there are any segments.
    #expect(controller.job?.bilingualText.contains(threeParts) == true)
    #expect(controller.job?.bilingualText.contains("尚未翻译 · 已停止") == true)
}

@MainActor @Test func wrongResultAndCancelledResponseAreNotSuccess() async throws {
    let controller = TextTranslationController()
    controller.start(source: "source", model: "local", direction: "en-zh") { _, m, d in record("different", m, d) }
    try await eventually { !controller.busy }
    #expect(controller.job?.phase == .partial && controller.job?.count(.failed) == 1)
    controller.start(source: "source", model: "local", direction: "en-zh") { _, _, _ in throw URLError(.cancelled) }
    try await eventually { !controller.busy }
    #expect(controller.job?.phase == .stopped && controller.job?.count(.completed) == 0)
}
