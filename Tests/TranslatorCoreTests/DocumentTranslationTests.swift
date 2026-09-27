import Foundation
import Testing
import CoreGraphics
import CoreText
import PDFKit
@testable import TranslatorCore

private func fixturePDF(pages: Int = 220) throws -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".pdf")
    var box = CGRect(x: 0, y: 0, width: 612, height: 792)
    let context = try #require(CGContext(url as CFURL, mediaBox: &box, nil))
    for page in 1...pages {
        context.beginPDFPage(nil)
        if page != 202 {
            let line = CTLineCreateWithAttributedString(NSAttributedString(string: "PHYSICAL-PAGE-\(page) repeated repeated 123.",
                attributes: [NSAttributedString.Key(kCTFontAttributeName as String): CTFontCreateWithName("Helvetica" as CFString, 14, nil)]))
            context.textPosition = CGPoint(x: 40, y: 700); CTLineDraw(line, context)
        }
        context.endPDFPage()
    }
    context.closePDF(); return url
}
private func fixtureTXT(_ text: String) throws -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".txt")
    try text.write(to: url, atomically: true, encoding: .utf8); return url
}
@MainActor private func until(_ condition: () -> Bool) async throws {
    for _ in 0..<10000 { if condition() { return }; try await Task.sleep(nanoseconds: 1_000_000) }
    try #require(condition())
}
private func response(_ text: String, _ model: String, _ direction: String) -> TranslationRecord {
    TranslationRecord(source: text, translation: "translated " + text, model: model, direction: direction, elapsedSeconds: 0, warnings: [])
}
@MainActor private class Gate {
    var calls: [(String, String, String)] = []
    var pending: [CheckedContinuation<TranslationRecord, Error>] = []
    func translate(_ text: String, _ model: String, _ direction: String) async throws -> TranslationRecord {
        calls.append((text, model, direction))
        return try await withCheckedThrowingContinuation { pending.append($0) }
    }
    func finish(_ index: Int, error: Error? = nil) {
        if let error { pending[index].resume(throwing: error) }
        else { let c = calls[index]; pending[index].resume(returning: response(c.0, c.1, c.2)) }
    }
}
@Test func documentRangesRejectInvalidWithoutClipping() throws {
    #expect(try DocumentPageRange(first: 201, last: 220, total: 220).label == "201–220")
    for (a,b,total) in [(0,1,10),(5,3,10),(1,11,10),(1,201,220),(1,0,0)] {
        #expect(throws: (any Error).self) { try DocumentPageRange(first: a, last: b, total: total) }
    }
}
@MainActor @Test func physicalPagesBeyond200AndBlankCoverage() async throws {
    let url = try fixturePDF(); defer { try? FileManager.default.removeItem(at: url) }
    let c = DocumentTranslationController(); c.load(url); try await until { !c.busy }
    #expect(c.totalPages == 220)
    c.extract(first: 201, last: 220, model: "local", direction: "en-zh"); try await until { !c.busy }
    let s = try #require(c.snapshot)
    #expect(s.pages.map(\.id) == Array(201...220))
    #expect(s.pages[1].state == .noText && s.pages[1].text.isEmpty)
    #expect(s.pages.last?.text.contains("PHYSICAL-PAGE-220") == true)
    #expect(s.segments.count == 19)
    c.start { response($0, $1, $2) }; try await until { !c.busy }
    #expect(c.translator.job?.count(.completed) == 19)
    let exported = s.export(translation: c.translator.job)
    #expect(exported.contains("PDF 物理页 202") && exported.contains("未提取到文字"))
    #expect(exported.contains("所选范围的可提取文字翻译完成"))
    #expect(!exported.contains("全部翻译完成"))
}
@Test func documentSourceRangesPreserveUnicodeRepeatedLinesAndPageBoundaries() throws {
    let raw = "标题\n" + String(repeating: "重复 123. 👨‍👩‍👧‍👦 e\u{301}\n", count: 240) + "\nfooter"
    var s = DocumentSnapshot(id: UUID(), file: "fixture", fingerprint: "test", totalPages: 2, isPDF: true,
        range: try DocumentPageRange(first: 1, last: 2, total: 2), model: "local", direction: "en-zh",
        pages: [DocumentPageRecord(id: 1, text: raw, state: .extracted), DocumentPageRecord(id: 2, text: "next page sentence", state: .extracted)])
    try s.prepare()
    for page in s.pages {
        let mapping = s.sources.filter { $0.page == page.id }
        #expect(mapping.map { s.segments[$0.segmentID].source }.joined() == page.text)
        for m in mapping {
            #expect(Array(s.segments[m.segmentID].source.utf8) == Array(Array(page.text.utf8)[m.utf8Start..<m.utf8End]))
            #expect(s.segments[m.segmentID].requestText.utf8.count <= TranslationBudget.sourceBytes)
        }
    }
    #expect(s.export(translation: nil).components(separatedBy: raw).count == 2)
}
@MainActor @Test func documentSerialFailureStopAndLateResults() async throws {
    let url = try fixtureTXT(String(repeating: "a", count: 5000)); defer { try? FileManager.default.removeItem(at: url) }
    let c = DocumentTranslationController(), gate = Gate()
    c.load(url); try await until { !c.busy }; c.extract(first: 1, last: 1, model: "local", direction: "en-zh")
    try await until { !c.busy }; c.start(translate: gate.translate)
    try await until { gate.calls.count == 1 }; gate.finish(0)
    try await until { gate.calls.count == 2 }; gate.finish(1, error: M0Error.incomplete("截断"))
    try await until { gate.calls.count == 3 }
    #expect(c.translator.job?.count(.failed) == 1)
    c.stop()
    let stopped = try #require(c.translator.job)
    #expect(stopped.count(.completed) == 1 && stopped.count(.stopped) == 1)
    #expect(stopped.segments.count == stopped.count(.completed) + stopped.count(.failed) + stopped.count(.stopped))
    let output = try #require(c.snapshot).export(translation: stopped)
    #expect(output.contains("截断") && output.contains("本段中断") && output.contains(String(repeating: "a", count: 5000)))
    c.load(url); try await until { !c.busy }; c.extract(first: 1, last: 1, model: "new", direction: "zh-en")
    try await until { !c.busy }; c.start { response($0, $1, $2) }
    gate.finish(2); try await until { !c.busy }
    #expect(c.translator.job?.model == "new" && c.translator.job?.direction == "zh-en")
    #expect(c.translator.job?.count(.completed) == 3 && gate.calls.count == 3)
}
@MainActor @Test func documentStoppedExtractionListsEveryUnprocessedPage() async throws {
    let url = try fixturePDF(); defer { try? FileManager.default.removeItem(at: url) }
    let c = DocumentTranslationController(); c.load(url); try await until { !c.busy }
    c.extract(first: 1, last: 200, model: "local", direction: "en-zh"); c.stop()
    #expect(c.snapshot?.pages.count == 200)
    #expect(c.snapshot?.pages.allSatisfy { $0.state == .stopped } == true)
    let output = try #require(c.snapshot).export(translation: nil)
    #expect(output.contains("PDF 物理页 200") && output.contains("停止未处理"))
    c.start { response($0, $1, $2) }; #expect(c.translator.job == nil)
    c.clear(); try await Task.sleep(nanoseconds: 20_000_000); #expect(c.snapshot == nil)
}
@MainActor @Test func utf8BOMInvalidEncodingAndSourceDeletion() async throws {
    let url = try fixtureTXT("\u{feff}中文\n重复\n重复\n123"); defer { try? FileManager.default.removeItem(at: url) }
    let c = DocumentTranslationController(); c.load(url); try await until { !c.busy }
    try FileManager.default.removeItem(at: url)
    c.extract(first: 1, last: 1, model: "local", direction: "zh-en"); try await until { !c.busy }
    #expect(c.snapshot?.pages.first?.text == "中文\n重复\n重复\n123")
    #expect(c.data != nil) // Original immutable bytes remain usable after deletion.
    #expect(c.snapshot?.issue?.contains("源文件已无法读取") == true)
    try Data([0xff,0xfe,0x00]).write(to: url); c.load(url); try await until { !c.busy }
    #expect(c.error != nil && c.snapshot == nil && c.data == nil)
}
@MainActor @Test func documentZeroTranslationAndServiceFailureExport() async throws {
    let url = try fixtureTXT(String(repeating: "b", count: 5000)); defer { try? FileManager.default.removeItem(at: url) }
    let c = DocumentTranslationController(); c.load(url); try await until { !c.busy }
    c.extract(first: 1, last: 1, model: "local", direction: "en-zh"); try await until { !c.busy }
    #expect(c.snapshot?.export(translation: nil).contains("尚未翻译") == true)
    var calls = 0
    c.start { _,_,_ in calls += 1; throw M0Error.unavailable("service unavailable") }
    try await until { !c.busy }
    #expect(calls == 1 && c.translator.job?.count(.pending) == 2)
    #expect(c.snapshot?.export(translation: c.translator.job).contains("service unavailable") == true)
}
@Test func failedAndWhitespacePagesRemainInExport() throws {
    var s = DocumentSnapshot(id: UUID(), file: "fixture", fingerprint: "test", totalPages: 3, isPDF: true,
        range: try DocumentPageRange(first: 1, last: 3, total: 3), model: "local", direction: "en-zh",
        pages: [DocumentPageRecord(id: 1, state: .failed, issue: "未获得原文"),
                DocumentPageRecord(id: 2, text: " \n\t", state: .noText), DocumentPageRecord(id: 3)])
    try s.prepare(); #expect(s.segments.isEmpty)
    let output = s.export(translation: nil)
    #expect(output.contains("未获得原文") && output.contains("待提取") && output.contains(" \n\t"))
}
@Test func corruptAndOversizeFilesFailBeforeTranslation() async throws {
    let url = try fixturePDF(pages: 1); defer { try? FileManager.default.removeItem(at: url) }
    try Data("not PDF".utf8).write(to: url)
    do { _ = try await DocumentTextReader().load(url); Issue.record("Corrupt PDF accepted") } catch {}
    let handle = try FileHandle(forWritingTo: url); try handle.truncate(atOffset: 256_000_001); try handle.close()
    do { _ = try await DocumentTextReader().load(url); Issue.record("Oversize accepted") } catch {}
}

@Test func lockedPDFRejectedAndImageOnlyPDFNeverOCRs() async throws {
    let url = try fixturePDF(pages: 1)
    defer { try? FileManager.default.removeItem(at: url) }
    let document = try #require(PDFDocument(url: url))
    #expect(document.write(to: url, withOptions: [.userPasswordOption: "fixture-password", .ownerPasswordOption: "fixture-owner"]))
    do { _ = try await DocumentTextReader().load(url); Issue.record("Locked PDF accepted") } catch {}
    var box = CGRect(x: 0, y: 0, width: 612, height: 792)
    let ctx = try #require(CGContext(url as CFURL, mediaBox: &box, nil))
    ctx.beginPDFPage(nil); ctx.setFillColor(CGColor(gray: 0.4, alpha: 1)); ctx.fill(CGRect(x: 50, y: 50, width: 400, height: 600)); ctx.endPDFPage(); ctx.closePDF()
    let reader = DocumentTextReader(); _ = try await reader.load(url)
    let page = try await reader.page(1)
    #expect(page.state == .noText && page.text.isEmpty)
}

@MainActor @Test func sourceChangedDuringTranslationKeepsSnapshotAndWarns() async throws {
    let url = try fixtureTXT("original source")
    defer { try? FileManager.default.removeItem(at: url) }
    let c = DocumentTranslationController(), gate = Gate()
    c.load(url); try await until { !c.busy }
    c.extract(first: 1, last: 1, model: "local", direction: "en-zh"); try await until { !c.busy }
    c.start(translate: gate.translate); try await until { gate.calls.count == 1 }
    try "replacement source with different size".write(to: url, atomically: true, encoding: .utf8)
    gate.finish(0); try await until { !c.busy }
    #expect(c.snapshot?.pages.first?.text == "original source")
    #expect(c.snapshot?.issue?.contains("已变化") == true)
    #expect(c.translator.job?.count(.completed) == 1)
}
