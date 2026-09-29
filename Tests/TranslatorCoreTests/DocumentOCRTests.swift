import Foundation
import Testing
import CoreGraphics
import CoreText
import PDFKit
@testable import TranslatorCore

private func ocrFixture(pages: Int = 3) throws -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".pdf")
    var box = CGRect(x: 0, y: 0, width: 612, height: 792)
    let context = try #require(CGContext(url as CFURL, mediaBox: &box, nil))
    for _ in 1...pages { context.beginPDFPage(nil); context.endPDFPage() }
    context.closePDF(); return url
}
private func ocrPage(_ number: Int, _ text: String = "Repeated 123. Do not extend.\nRepeated 123.") -> DocumentPageRecord {
    var page = DocumentPageRecord(id: number, text: text, state: text.isEmpty ? .noText : .extracted)
    page.mode = .ocr
    page.ocr = OCRPageEvidence(observations: text.isEmpty ? [] : [OCRObservation(id: "fixture-\(number)", order: 0, text: text, confidence: 0.2, rect: [0.1,0.2,0.8,0.1])],
        pixelWidth: 100, pixelHeight: 200, rotation: 0, cropBox: [0,0,612,792])
    return page
}
@MainActor private func awaitOCR(_ predicate: () -> Bool) async throws {
    for _ in 0..<10000 { if predicate() { return }; try await Task.sleep(nanoseconds: 1_000_000) }
    try #require(predicate())
}
private func ocrTranslation(_ text: String, _ model: String, _ direction: String) -> TranslationRecord {
    TranslationRecord(source: text, translation: "result: " + text, model: model, direction: direction, elapsedSeconds: 0, warnings: [])
}
@MainActor private class OCRGate {
    var calls: [Int] = []
    var tokens: [OCRCancellation] = []
    var pending: [CheckedContinuation<DocumentPageRecord, Error>] = []
    func recognize(_ page: Int, _ cancellation: OCRCancellation) async throws -> DocumentPageRecord {
        calls.append(page); tokens.append(cancellation)
        return try await withCheckedThrowingContinuation { pending.append($0) }
    }
    func finish(_ index: Int) { pending[index].resume(returning: ocrPage(calls[index])) }
}

@MainActor @Test func explicitOCRRangeAndDefaultNeverInvokesOCR() async throws {
    let url = try ocrFixture(pages: 220); defer { try? FileManager.default.removeItem(at: url) }
    var calls: [Int] = []
    let c = DocumentTranslationController(ocr: { number, _ in calls.append(number); return ocrPage(number) })
    c.load(url); try await awaitOCR { !c.busy }
    c.extract(first: 201, last: 220, model: "local", direction: "en-zh")
    try await awaitOCR { !c.busy }; #expect(calls.isEmpty && c.snapshot?.pages.count == 20)
    c.extract(first: 201, last: 220, model: "local", direction: "en-zh", mode: .ocr)
    try await awaitOCR { !c.busy }
    #expect(calls == Array(201...220) && c.snapshot?.mode == .ocr)
    for (first, last) in [(220,201),(1,201),(201,221)] {
        c.extract(first: first, last: last, model: "local", direction: "en-zh", mode: .ocr)
        #expect(c.error != nil && calls.count == 20)
    }
}
@MainActor @Test func OCRCancelSerialLatePageAndNewFileIsolation() async throws {
    let url = try ocrFixture(); defer { try? FileManager.default.removeItem(at: url) }
    let gate = OCRGate(), c = DocumentTranslationController(ocr: { try await gate.recognize($0, $1) })
    c.load(url); try await awaitOCR { !c.busy }
    c.extract(first: 1, last: 3, model: "local", direction: "en-zh", mode: .ocr)
    try await awaitOCR { gate.calls.count == 1 }; gate.finish(0)
    try await awaitOCR { gate.calls.count == 2 }; c.stop()
    #expect(gate.tokens[1].isCancelled)
    let s = try #require(c.snapshot)
    #expect(s.pages.map(\.state) == [.extracted,.stopped,.stopped])
    #expect(s.export(translation: nil).contains("PDF 物理页 3"))
    c.load(url); try await awaitOCR { !c.busy }
    c.extract(first: 3, last: 3, model: "new", direction: "zh-en", mode: .ocr)
    try await awaitOCR { gate.calls.count == 3 }
    gate.finish(1); gate.finish(2); try await awaitOCR { !c.busy }
    #expect(gate.calls == [1,2,3] && c.snapshot?.pages.map(\.id) == [3])
    #expect(c.snapshot?.model == "new")
}
@MainActor @Test func OCRCorrectionsPreserveEvidenceAndResetReview() async throws {
    let url = try ocrFixture(); defer { try? FileManager.default.removeItem(at: url) }
    let c = DocumentTranslationController(ocr: { n,_ in ocrPage(n, n == 3 ? "" : "original 123") })
    c.load(url); try await awaitOCR { !c.busy }
    c.extract(first: 1, last: 3, model: "local", direction: "en-zh", mode: .ocr); try await awaitOCR { !c.busy }
    let first = try #require(c.snapshot?.pages.first)
    c.setReviewed(page: 1, reviewed: true); c.beginEdit(page: 1)
    let corrected = "修正 👨‍👩‍👧‍👦 e\u{301}\n" + String(repeating: "重复123 不得删除。\n", count: 300)
    c.updateDraft(page: 1, text: corrected); c.beginEdit(page: 2)
    c.start { ocrTranslation($0,$1,$2) }; #expect(c.translator.job == nil && c.hasDrafts)
    c.cancelEdit(page: 2); #expect(c.drafts[1] == corrected)
    c.saveEdit(page: 1); #expect(c.preparing && !c.canStart); try await awaitOCR { !c.busy }
    let edited = try #require(c.snapshot?.pages.first)
    #expect(edited.ocr?.text == first.text && edited.text == corrected && edited.revision != first.revision && !edited.reviewed)
    let s = try #require(c.snapshot)
    let mapping = s.sources.filter { $0.page == 1 }
    #expect(mapping.map { s.segments[$0.segmentID].source }.joined() == corrected)
    #expect(mapping.allSatisfy { $0.revision == edited.revision && s.segments[$0.segmentID].requestText.utf8.count <= TranslationBudget.sourceBytes })
    for m in mapping { #expect(Array(s.segments[m.segmentID].source.utf8) == Array(Array(corrected.utf8)[m.utf8Start..<m.utf8End])) }
    c.beginEdit(page: 2); c.updateDraft(page: 2, text: " \n"); c.saveEdit(page: 2); try await awaitOCR { !c.busy }
    c.beginEdit(page: 3); c.updateDraft(page: 3, text: "manual only"); c.saveEdit(page: 3); try await awaitOCR { !c.busy }
    #expect(c.snapshot?.pages[1].sourceLabel == "人工清空" && c.snapshot?.pages[2].sourceLabel == "人工输入（非 OCR 覆盖）")
    #expect(c.snapshot?.pages[2].state == .noText)
    c.start { ocrTranslation($0,$1,$2) }; try await awaitOCR { !c.busy }
    #expect(c.snapshot?.translationStarted == true && !c.canEdit)
    c.beginEdit(page: 1); #expect(!c.hasDrafts)
    let oldJob = c.translator.job
    c.reopenReview(); #expect(c.translator.job == nil && c.canEdit)
    c.beginEdit(page: 1); c.updateDraft(page: 1, text: "new revision"); c.saveEdit(page: 1); try await awaitOCR { !c.busy }
    let output = try #require(c.snapshot).export(translation: oldJob)
    #expect(!output.contains("result:") && output.contains("original 123") && output.contains("new revision") && output.contains("人工清空") && output.contains("manual only"))
}
@MainActor @Test func OCRFailureBlankManualOnlyAndZeroTranslationExport() async throws {
    let url = try ocrFixture(); defer { try? FileManager.default.removeItem(at: url) }
    let c = DocumentTranslationController(ocr: { n,_ in
        if n == 2 { throw M0Error.invalid("controlled OCR failure") }; return ocrPage(n, "")
    })
    c.load(url); try await awaitOCR { !c.busy }; c.extract(first: 1, last: 3, model: "local", direction: "en-zh", mode: .ocr)
    try await awaitOCR { !c.busy }; #expect(!c.canStart)
    #expect(c.snapshot?.pages.map(\.state) == [.noText,.failed,.noText])
    c.beginEdit(page: 2); c.updateDraft(page: 2, text: "manual text"); c.saveEdit(page: 2); try await awaitOCR { !c.busy }
    var calls = 0
    c.start { calls += 1; return ocrTranslation($0,$1,$2) }; try await awaitOCR { !c.busy }
    #expect(calls == 1)
    let output = try #require(c.snapshot).export(translation: c.translator.job)
    #expect((1...3).allSatisfy { output.contains("PDF 物理页 \($0)") })
    #expect(output.contains("controlled OCR failure") && output.contains("人工输入") && output.contains("未获得完整页识别结果"))
    #expect(!output.contains("仅处理文字层") && output.contains("非 OCR 准确性保证"))
}
@MainActor @Test func OCRLatePreparationCannotReplaceNewSnapshot() async throws {
    let url = try ocrFixture(); defer { try? FileManager.default.removeItem(at: url) }
    var pending: [CheckedContinuation<DocumentSnapshot, Error>] = []
    var captures: [DocumentSnapshot] = []
    let c = DocumentTranslationController(ocr: { n,_ in ocrPage(n) }, prepare: { captured in
        captures.append(captured); return try await withCheckedThrowingContinuation { pending.append($0) }
    })
    c.load(url); try await awaitOCR { !c.busy }; c.extract(first: 1, last: 1, model: "old", direction: "en-zh", mode: .ocr)
    try await awaitOCR { pending.count == 1 }; c.stop()
    c.load(url); try await awaitOCR { !c.busy }; c.extract(first: 2, last: 2, model: "new", direction: "zh-en", mode: .ocr)
    try await awaitOCR { pending.count == 2 }
    var ready = captures[1]; try ready.prepare(); pending[1].resume(returning: ready)
    try await awaitOCR { !c.busy }; pending[0].resume(returning: captures[0])
    try await Task.sleep(nanoseconds: 10_000_000)
    #expect(c.snapshot?.model == "new" && c.snapshot?.pages.first?.id == 2)
}
@MainActor @Test func OCRLateTranslationCannotAttachToCorrectedRevision() async throws {
    let url = try ocrFixture(); defer { try? FileManager.default.removeItem(at: url) }
    var pending: CheckedContinuation<TranslationRecord, Error>?
    let c = DocumentTranslationController(ocr: { n,_ in ocrPage(n) })
    c.load(url); try await awaitOCR { !c.busy }; c.extract(first: 1, last: 1, model: "local", direction: "en-zh", mode: .ocr)
    try await awaitOCR { !c.busy }
    let original = try #require(c.snapshot?.segments.first?.requestText)
    c.start { _,_,_ in try await withCheckedThrowingContinuation { pending = $0 } }
    try await awaitOCR { pending != nil }; c.stop(); c.reopenReview()
    c.beginEdit(page: 1); c.updateDraft(page: 1, text: "corrected source"); c.saveEdit(page: 1); try await awaitOCR { !c.busy }
    c.start { ocrTranslation($0,$1,$2) }; try await awaitOCR { !c.busy }
    pending?.resume(returning: ocrTranslation(original,"local","en-zh"))
    try await Task.sleep(nanoseconds: 10_000_000)
    #expect(c.translator.job?.completedTranslation == "result: corrected source")
}
@Test func OCRPixelBudgetAndCancelBeforeVision() async throws {
    for (w,h) in [(612.0,792.0),(100_000.0,200_000.0),(1.0,1_000_000.0)] {
        let (a,b) = try DocumentOCR.pixelSize(width: w, height: h)
        #expect(a <= 3000 && b <= 3000 && a*b <= 9_000_000)
    }
    #expect(throws: (any Error).self) { try DocumentOCR.pixelSize(width: .infinity, height: 100) }
    let token = OCRCancellation(); token.cancel()
    let url = try ocrFixture(); defer { try? FileManager.default.removeItem(at: url) }
    let reader = DocumentTextReader(); _ = try await reader.load(url)
    do { _ = try await reader.ocrPage(1, cancellation: token); Issue.record("cancelled OCR accepted") } catch is CancellationError {} catch { Issue.record("unexpected error") }
}

@Test func cancelledOllamaEngineRejectsNewRequestsWithoutInvalidSessionCrash() async throws {
    let engine = try OllamaEngine(endpoint: "http://127.0.0.1:1")
    engine.cancelRequests()
    do { _ = try await engine.models(); Issue.record("cancelled engine accepted request") }
    catch is CancellationError {} catch { Issue.record("expected cancellation before any network access") }
}

@Test func OCRRendererUpscalesVisiblePageAndMapsCropRotation() throws {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".pdf")
    defer { try? FileManager.default.removeItem(at: url) }
    var box = CGRect(x: 0, y: 0, width: 100, height: 200)
    let context = try #require(CGContext(url as CFURL, mediaBox: &box, nil))
    context.beginPDFPage(nil); context.setFillColor(CGColor(gray: 0, alpha: 1)); context.fill(box)
    context.endPDFPage(); context.closePDF()
    let document = try #require(PDFDocument(url: url)), page = try #require(document.page(at: 0))
    page.setBounds(CGRect(x: 10,y: 20,width: 80,height: 160), for: .cropBox)
    page.rotation = 90
    let (image, rotation, crop) = try DocumentOCR.render(page)
    #expect(rotation == 90 && crop.width == 80 && image.width == 480 && image.height == 240)
    let data = try #require(image.dataProvider?.data)
    let bytes = try #require(CFDataGetBytePtr(data))
    // The visible black page fills the bitmap, including corners. Without explicit
    // upscaling, CGPDF centers a small page and these corners incorrectly stay white.
    for (x,y) in [(2,2),(477,2),(2,237),(477,237)] {
        let offset = y * image.bytesPerRow + x * 4
        #expect(bytes[offset] < 10 && bytes[offset+1] < 10 && bytes[offset+2] < 10)
    }
    #expect(DocumentOCR.languages(direction: "zh-en").first == "zh-Hans")
    #expect(DocumentOCR.languages(direction: "en-zh").first == "en-US")
}
