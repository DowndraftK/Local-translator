import Foundation
import Testing
@testable import TranslatorCore

@Test func newRecipeFitsBudgetAndOldRecipeEncodingRemainsIdentical() throws {
    for direction in ["en-zh", "zh-en"] {
        let old = TranslationConfiguration(model: "hy-mt2:1.8b-q8", direction: direction, digest: "old", profileVersion: 1)
        let bytes = try JSONOutput.encode(old)
        #expect(!String(decoding: bytes, as: UTF8.self).contains("contextVersion"))
        let restored = try JSONDecoder().decode(TranslationConfiguration.self, from: bytes)
        #expect(try JSONOutput.encode(restored) == bytes && restored.binding == old.binding)
        try restored.validate()
        let new = TranslationConfiguration(model: TranslationConfiguration.defaultModel, direction: direction, digest: "new")
        try new.validate()
        try TranslationBudget.validate(source: String(repeating: "x", count: TranslationBudget.sourceBytes), promptBytes: new.userPrefix.utf8.count)
        #expect(new.version == 2 && new.contextVersion == "none-v1")
        #expect(new.options["seed"] == 42 && new.options["repeat_penalty"] == 1 && new.options["temperature"] == 0.1)
        #expect(new.binding != old.binding && restored.options["temperature"] == 0.7)
        var bad = new; bad.contextVersion = "unknown"
        #expect(throws: (any Error).self) { try bad.validate() }
        bad = new; bad.version = 99
        #expect(throws: (any Error).self) { try bad.validate() }
    }
}

@Test func longQualitySourcesKeepRepetitionsAndLastSentence() throws {
    let source = String(repeating: "Do not delete this repeated sentence. Do not delete this repeated sentence.\n", count: 100)
        + "The balance must be received before July 9, not merely sent."
    let segments = try LongTextSplitter.split(source)
    #expect(segments.map(\.source).joined() == source)
    #expect(segments.last?.source.hasSuffix("not merely sent.") == true)
    #expect(segments.allSatisfy { $0.requestText.utf8.count <= TranslationBudget.sourceBytes })
}

@Test func futureRecipeIsRejectedBeforeNetwork() async throws {
    var c = TranslationConfiguration(model: "hy-mt2:7b-q8", direction: "en-zh")
    c.version = 99
    let engine = try OllamaEngine(endpoint: "http://127.0.0.1:1")
    do {
        _ = try await engine.translate("test", model: c.model, configuration: c)
        Issue.record("Unknown recipe accepted")
    } catch { #expect(error.localizedDescription.contains("配置版本")) }
}

@Test func qualityBudgetRejectsOversizeBeforeNetworkAndHintsDoNotCertifyFacts() async throws {
    let engine = try OllamaEngine(endpoint: "http://127.0.0.1:1")
    do {
        _ = try await engine.translate(String(repeating: "x", count: 2049), model: "hy-mt2:7b-q8")
        Issue.record("Oversize request accepted")
    } catch { #expect(error.localizedDescription.contains("容量预算")) }
    let warnings = ContentChecks.warnings(source: "At least 14 days before the course begins.",
        target: "课程开始前14天以内。", glossary: [])
    #expect(warnings.contains { $0.contains("数字一致也不保证正确") })
}
