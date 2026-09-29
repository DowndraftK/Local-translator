import Foundation
import Testing
import PDFKit
import Darwin
@testable import TranslatorCore

/// Opt-in, finite local acceptance. Generated PDFs, source text and reports are
/// only written to the explicitly supplied ignored artifact directory.
/// OCR_ACCEPTANCE_DIR=... OCR_ACCEPTANCE_MODEL=1 bash scripts/swift.sh test --filter finiteLocalOCRAcceptance
@Test(.enabled(if: ProcessInfo.processInfo.environment["OCR_ACCEPTANCE_DIR"] != nil))
@MainActor func finiteLocalOCRAcceptance() async throws {
    let root = URL(fileURLWithPath: try #require(ProcessInfo.processInfo.environment["OCR_ACCEPTANCE_DIR"]))
    let useModel = ProcessInfo.processInfo.environment["OCR_ACCEPTANCE_MODEL"] == "1"
    let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    func save<T: Encodable>(_ value: T, _ name: String) async throws {
        try await Task.detached { try encoder.encode(value).write(to: root.appendingPathComponent(name)) }.value
    }
    func wait(_ condition: () -> Bool) async throws {
        for _ in 0..<36000 { if condition() { return }; try await Task.sleep(nanoseconds: 50_000_000) }
        try #require(condition())
    }
    func export(_ c: DocumentTranslationController, _ name: String) async throws {
        let snapshot = try #require(c.snapshot), job = c.translator.job
        try await Task.detached { try snapshot.export(translation: job).write(to: root.appendingPathComponent(name), atomically: true, encoding: .utf8) }.value
    }
    func translate(_ c: DocumentTranslationController) throws {
        let engine = try OllamaEngine()
        c.start { text, model, direction in
            try await withTaskCancellationHandler { try await engine.translate(text, model: model, direction: direction) }
            onCancel: { engine.cancelRequests() }
        }
    }
    struct Measurement: Codable { var file: String; var seconds: Double; var maxRSSBytes: Int; var phase: String }
    var measurements: [Measurement] = (try? JSONDecoder().decode([Measurement].self, from: Data(contentsOf: root.appendingPathComponent("measurements.json")))) ?? []
    func measure(_ file: String, _ start: Date, _ phase: String) async throws {
        var usage = rusage(); getrusage(RUSAGE_SELF, &usage)
        measurements.append(Measurement(file: file, seconds: Date().timeIntervalSince(start), maxRSSBytes: usage.ru_maxrss, phase: phase))
        try await save(measurements, "measurements.json")
    }
    let names = ProcessInfo.processInfo.environment["OCR_ACCEPTANCE_FILES"]?.split(separator: ",").map(String.init) ?? ["scan-12", "chinese-scan", "public-raster", "mixed", "quality", "geometry"]
    for name in names {
        let c = DocumentTranslationController(), start = Date()
        c.load(root.appendingPathComponent(name + ".pdf")); try await wait { !c.busy }
        try #require(c.data != nil)
        if ["scan-12", "chinese-scan", "public-raster"].contains(name) {
            let reader = DocumentTextReader(); _ = try await reader.load(root.appendingPathComponent(name + ".pdf"))
            for n in 1...c.totalPages { #expect(try await reader.page(n).text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) }
        }
        if name == "mixed" {
            c.extract(first: 1, last: 1, model: "hy-mt2:1.8b-q8", direction: "en-zh")
            try await wait { !c.busy }; try await save(c.snapshot, "mixed-text-layer.json")
        }
        c.extract(first: 1, last: c.totalPages, model: "hy-mt2:1.8b-q8", direction: name == "chinese-scan" ? "zh-en" : "en-zh", mode: .ocr)
        try await wait { !c.busy }
        try await save(c.snapshot, name + "-ocr.json")
        try await export(c, name + "-zero.txt")
        try await measure(name, start, "OCR")
        #expect(c.snapshot?.pages.count == c.totalPages)
        #expect(c.snapshot?.pages.allSatisfy { ($0.ocr?.pixelWidth ?? 0) <= 3000 && ($0.ocr?.pixelHeight ?? 0) <= 3000 } == true)
        if ["scan-12", "chinese-scan", "public-raster"].contains(name) {
            try #require(c.snapshot?.pages.allSatisfy { $0.state == .extracted } == true)
        }
        if name == "geometry" {
            #expect(c.snapshot?.pages[0].ocr?.rotation == 90)
            #expect(c.snapshot?.pages[1].ocr?.cropBox[1] == 600)
            #expect(c.snapshot?.pages[1].text.contains("END-MARKER") == false)
            #expect(c.snapshot?.pages[2].ocr?.pixelHeight == 3000)
        }
        if name == "chinese-scan" {
            let original = try #require(c.snapshot?.pages[0].text)
            c.beginEdit(page: 1); c.updateDraft(page: 1, text: original + "\n人工补充：预算仍需审核。")
            c.saveEdit(page: 1); try await wait { !c.busy }
            #expect(c.snapshot?.pages[0].ocr?.text == original)
            c.setReviewed(page: 1, reviewed: true)
        }
        if useModel && ["scan-12","chinese-scan","public-raster"].contains(name) {
            let translationStart = Date()
            try translate(c); try await wait { !c.busy }
            try await save(c.translator.job, name + "-translation.json")
            try await save(c.snapshot, name + "-final-source.json")
            try await export(c, name + "-complete.txt")
            #expect(c.translator.job?.phase == .completed)
            try await measure(name, translationStart, "translation")
            if name == "scan-12" {
                #expect(c.snapshot?.pages.count == 12)
                let lastIDs = try #require(c.snapshot).sources.filter { $0.page == 12 }.map(\.segmentID)
                #expect(!lastIDs.isEmpty && lastIDs.allSatisfy { c.translator.job?.segments[$0].state == .completed })
                try translate(c)
                try await wait { (c.translator.job?.count(.completed) ?? 0) >= 1 || !c.busy }
                c.stop(); try await export(c, "scan-12-stopped.txt")
                try await save(c.translator.job, "scan-12-stopped.json")
                #expect(c.translator.job?.phase == .stopped)
            }
        }
    }
    // Real Vision cancellation: current synchronous perform receives cancel from
    // another executor; the full page result must never be accepted after stop.
    let reader = DocumentTextReader(); _ = try await reader.load(root.appendingPathComponent("scan-12.pdf"))
    let token = OCRCancellation(), start = Date()
    let worker = Task.detached { try await reader.ocrPage(1, cancellation: token) }
    try await Task.sleep(nanoseconds: 30_000_000); token.cancel()
    do { _ = try await worker.value; Issue.record("Vision completed before cancellation; cancellation sample inconclusive") }
    catch { #expect(token.isCancelled) }
    try await measure("scan-12", start, "cancel Vision and await return")
    let stopped = DocumentTranslationController(); stopped.load(root.appendingPathComponent("scan-12.pdf")); try await wait { !stopped.busy }
    stopped.extract(first: 1, last: 12, model: "hy-mt2:1.8b-q8", direction: "en-zh", mode: .ocr)
    try await wait { stopped.snapshot?.pages.first?.state == .extracted || !stopped.busy }
    stopped.stop(); try await export(stopped, "scan-12-OCR-stopped.txt")
    #expect(stopped.snapshot?.pages.count == 12)
    stopped.load(root.appendingPathComponent("chinese-scan.pdf")); try await wait { !stopped.busy }
    #expect(stopped.totalPages == 1 && stopped.snapshot == nil)
}
