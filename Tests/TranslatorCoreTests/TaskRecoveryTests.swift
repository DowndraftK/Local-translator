import Foundation
import Testing
import PDFKit
import CoreGraphics
@testable import TranslatorCore

private func recoveryRoot() throws -> URL {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("RecoveryTests-" + UUID().uuidString)
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    return root
}
private func textTask(_ source: String = "first\nsecond\nthird", revision: UInt64 = 1) throws -> RecoveryTask {
    let c = TranslationConfiguration(model: "local", direction: "en-zh", digest: "fixed-digest")
    var j = TextTranslationJob(id: UUID(), source: source, model: c.model, direction: c.direction)
    j.segments = try LongTextSplitter.split(source, byteLimit: 7); j.phase = .running
    return RecoveryTask(id: j.id, revision: revision, name: "fixture", configuration: c, input: source, text: j)
}
private func recoveredResult(_ text: String) -> TranslationRecord {
    TranslationRecord(source: text, translation: "译文 " + text, model: "local", direction: "en-zh", elapsedSeconds: 0, warnings: [])
}
@MainActor private func awaitRecovery(_ check: () -> Bool) async throws {
    for _ in 0..<8000 { if check() { return }; try await Task.sleep(nanoseconds: 1_000_000) }
    try #require(check())
}

@Test func atomicSnapshotRetainsGoodPredecessorAndRejectsOutOfOrder() async throws {
    let root = try recoveryRoot(); defer { try? FileManager.default.removeItem(at: root) }
    let store = TaskRecoveryStore(root: root)
    var t = try textTask(); try await store.save(t)
    t.revision = 3; let result = recoveredResult(t.text!.segments[0].requestText); t.text?.segments[0].result = result
    t.text?.segments[0].state = .completed; try await store.save(t)
    var old = t; old.revision = 2; old.text?.segments[0].state = .pending; old.text?.segments[0].result = nil
    #expect(try await store.save(old) == false)
    let read = try await store.read(t.id)
    #expect(read.task.revision == 3 && read.task.text?.count(.completed) == 1)
    let previous = root.appendingPathComponent(t.id.uuidString).appendingPathComponent("previous.json")
    #expect(FileManager.default.fileExists(atPath: previous.path))
    let reopened = TaskRecoveryStore(root: root)
    #expect(try await reopened.save(old) == false)
}

@Test func controlledWriteFailurePreservesCurrentAndStopsAtDurableBoundary() async throws {
    let root = try recoveryRoot(); defer { try? FileManager.default.removeItem(at: root) }
    let good = TaskRecoveryStore(root: root); var t = try textTask(); try await good.save(t)
    let before = try Data(contentsOf: root.appendingPathComponent(t.id.uuidString).appendingPathComponent("current.json"))
    let failing = TaskRecoveryStore(root: root, write: { data, url in
        if url.lastPathComponent == "current.json" { throw CocoaError(.fileWriteOutOfSpace) }
        try data.write(to: url, options: .atomic)
    })
    t.revision += 1; t.text?.segments[0].state = .completed; let result = recoveredResult(t.text!.segments[0].requestText); t.text?.segments[0].result = result
    do { try await failing.save(t); Issue.record("write failure accepted") } catch {}
    #expect(try Data(contentsOf: root.appendingPathComponent(t.id.uuidString).appendingPathComponent("current.json")) == before)
    #expect(try await good.read(t.id).task.text?.count(.completed) == 0)
}

@Test func corruptedSnapshotReportsFallbackAndNeverOverwritesEvidence() async throws {
    let root = try recoveryRoot(); defer { try? FileManager.default.removeItem(at: root) }
    let store = TaskRecoveryStore(root: root); var t = try textTask(); try await store.save(t)
    t.revision = 2; try await store.save(t)
    let url = root.appendingPathComponent(t.id.uuidString).appendingPathComponent("current.json")
    try Data("broken".utf8).write(to: url)
    let r = try await store.read(t.id)
    #expect(r.warning != nil && r.task.revision == 1)
    t.revision = 3
    do { try await store.save(t); Issue.record("corrupt evidence overwritten") } catch {}
    #expect(try Data(contentsOf: url) == Data("broken".utf8))
    try FileManager.default.removeItem(at: url.deletingLastPathComponent().appendingPathComponent("previous.json"))
    do { _ = try await store.read(t.id); Issue.record("corruption silently cleared") } catch {}
    #expect(try await store.list().errors.count == 1)
}

@Test func futureFormatAndChecksumMismatchAreExplicit() async throws {
    let root = try recoveryRoot(); defer { try? FileManager.default.removeItem(at: root) }
    let store = TaskRecoveryStore(root: root); var t = try textTask(); try await store.save(t)
    t.revision = 2; try await store.save(t)
    let url = root.appendingPathComponent(t.id.uuidString).appendingPathComponent("current.json")
    var obj = try #require(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    obj["schema"] = 99; try JSONSerialization.data(withJSONObject: obj).write(to: url)
    do { _ = try await store.read(t.id); Issue.record("future format fell back") } catch { #expect(error.localizedDescription.contains("版本 99")) }
    let payload = obj["payload"]; obj.removeValue(forKey: "payload")
    try JSONSerialization.data(withJSONObject: obj).write(to: url)
    do { _ = try await store.read(t.id); Issue.record("future payload shape fell back") } catch { #expect(error.localizedDescription.contains("版本 99")) }
    obj["payload"] = payload
    obj["schema"] = 1; obj["checksum"] = "wrong"; try JSONSerialization.data(withJSONObject: obj).write(to: url)
    #expect(try await store.read(t.id).warning != nil)
}

@Test func stateSourcePlanAndConfigurationMismatchCannotBeCommitted() async throws {
    let root = try recoveryRoot(); defer { try? FileManager.default.removeItem(at: root) }
    let store = TaskRecoveryStore(root: root)
    var t = try textTask(); t.text?.segments[0].state = .completed
    do { try await store.save(t); Issue.record("completed without result") } catch {}
    t = try textTask(); t.input = "changed source"
    do { try await store.save(t); Issue.record("source mismatch") } catch {}
    t = try textTask(); t.configuration.direction = "zh-en"
    do { try await store.save(t); Issue.record("direction mismatch") } catch {}
    t = try textTask(); t.configuration.model = "other-model"
    do { try await store.save(t); Issue.record("model mismatch") } catch {}
    let original = TranslationConfiguration(model: "hy-mt2:1.8b-q8", direction: "en-zh", digest: "digest")
    var changed = original; changed.options["temperature"] = 0.2
    #expect(changed.binding != original.binding)
    changed = original; changed.version = 2
    #expect(throws: (any Error).self) { try changed.validate() }
    t = try textTask()
    var result = recoveredResult(t.text!.segments[0].requestText); result.configurationBinding = t.configuration.binding
    t.text?.segments[0].state = .completed; t.text?.segments[0].result = result
    t.configuration.options["temperature"] = 0.4
    do { try await store.save(t); Issue.record("result bound to changed parameters") } catch {}
    #expect(original.promptProfile == "hy-mt2-faithful-v1" && original.options["temperature"] == 0.7)
}

@MainActor @Test func reopenKeepsPlanAndSuccessfulSegmentsOnlyResumesUnfinished() async throws {
    let root = try recoveryRoot(); defer { try? FileManager.default.removeItem(at: root) }
    let store = TaskRecoveryStore(root: root); var t = try textTask()
    let result = recoveredResult(t.text!.segments[0].requestText); t.text?.segments[0].result = result; t.text?.segments[0].state = .completed
    t.text?.segments[1].state = .processing; try await store.save(t)
    let saved = try await store.read(t.id).task
    let controller = TextTranslationController(); var calls: [String] = []; var sequence: UInt64 = 1
    controller.checkpoint = { job in var next = saved; sequence += 1; next.revision = sequence; next.text = job; try await store.save(next) }
    controller.restore(saved.text)
    #expect(!controller.busy && calls.isEmpty && controller.job?.segments[1].state == .stopped)
    controller.resume { input, _, _ in calls.append(input); return recoveredResult(input) }
    controller.resume { _,_,_ in Issue.record("double click scheduled"); throw CancellationError() }
    try await awaitRecovery { !controller.busy }
    #expect(calls == saved.text!.segments.dropFirst().map(\.requestText))
    #expect(controller.job?.segments.map(\.source) == saved.text?.segments.map(\.source))
    #expect(try JSONOutput.encode(controller.job!.segments[0]) == JSONOutput.encode(saved.text!.segments[0]))
    #expect(try await store.read(t.id).task.text?.phase == .completed)
}

@MainActor @Test func persistenceGatePreventsNextRequestAndFailureLeavesMemoryExport() async throws {
    let c = TextTranslationController(); var calls = 0
    c.checkpoint = { job in if job.count(.completed) == 1 { throw CocoaError(.fileWriteOutOfSpace) } }
    c.start(source: "first\nsecond\nthird", model: "local", direction: "en-zh", translate: { input,_,_ in calls += 1; return recoveredResult(input) },
        preparedSegments: try LongTextSplitter.split("first\nsecond\nthird", byteLimit: 7))
    try await awaitRecovery { !c.busy }
    #expect(calls == 1 && c.job?.count(.completed) == 1 && c.job?.count(.pending) == 2)
    #expect(c.job?.error?.contains("保存") == true && c.job?.bilingualText.contains("third") == true)
}

@MainActor @Test func sameUUIDResumeRejectsOldGenerationAndRetriesAreBounded() async throws {
    let c = TextTranslationController(); var pending: CheckedContinuation<TranslationRecord, Error>?
    c.start(source: "first", model: "local", direction: "en-zh") { _,_,_ in try await withCheckedThrowingContinuation { pending = $0 } }
    try await awaitRecovery { pending != nil }; c.stop()
    let saved = try #require(c.job); c.restore(saved)
    c.resume { input,_,_ in recoveredResult(input) }; try await awaitRecovery { !c.busy }
    let good = try #require(c.job); pending?.resume(returning: recoveredResult("first"))
    try await Task.sleep(nanoseconds: 10_000_000)
    #expect(try JSONOutput.encode(c.job) == JSONOutput.encode(good))
    var calls = 0
    c.start(source: "failure", model: "local", direction: "en-zh") { _,_,_ in calls += 1; throw M0Error.incomplete("controlled failure") }
    try await awaitRecovery { !c.busy }
    for _ in 0..<4 { c.resume { _,_,_ in calls += 1; throw M0Error.incomplete("controlled failure") }; try await awaitRecovery { !c.busy } }
    #expect(calls == 3 && !c.canResume)
}

@MainActor @Test func OCRDraftRevisionRawEvidenceAndSourceCopySurviveMove() async throws {
    let root = try recoveryRoot(); defer { try? FileManager.default.removeItem(at: root) }
    let file = root.appendingPathComponent("original.pdf")
    var box = CGRect(x: 0,y: 0,width: 100,height: 100)
    let ctx = try #require(CGContext(file as CFURL, mediaBox: &box, nil)); ctx.beginPDFPage(nil); ctx.endPDFPage(); ctx.closePDF()
    let c = DocumentTranslationController(ocr: { n,_ in
        var p = DocumentPageRecord(id: n, text: "raw 123", state: .extracted); p.mode = .ocr
        p.ocr = OCRPageEvidence(observations: [OCRObservation(id: "raw", order: 0,text: "raw 123",confidence: 0.5,rect: [0,0,1,1])],pixelWidth: 100,pixelHeight: 100,rotation: 0,cropBox: [0,0,100,100]); return p
    })
    c.load(file); try await awaitRecovery { !c.busy }; c.extract(first: 1,last: 1,model: "local",direction: "en-zh",mode: .ocr)
    try await awaitRecovery { !c.busy }; c.beginEdit(page: 1); c.updateDraft(page: 1,text: "unapplied correction 456")
    let s = try #require(c.snapshot), data = try #require(c.data), store = TaskRecoveryStore(root: root.appendingPathComponent("tasks"))
    let a = RecoveryAsset(originalPath: file.path,filename: "source.pdf",fingerprint: s.fingerprint,byteCount: data.count)
    var t = RecoveryTask(id: s.id,revision: 1,name: "OCR",configuration: TranslationConfiguration(model: "local",direction: "en-zh"),document: s,drafts: c.drafts,asset: a)
    try await store.save(t,assetData: data)
    try FileManager.default.moveItem(at: file,to: root.appendingPathComponent("moved.pdf"))
    let saved = try await store.read(t.id).task, source = try await store.sourceData(saved)
    let reopened = DocumentTranslationController()
    await reopened.restore(snapshot: saved.document!,translation: nil,drafts: saved.drafts,assetURL: source.0)
    #expect(reopened.drafts[1] == "unapplied correction 456" && reopened.snapshot?.pages[0].text == "raw 123" && !reopened.canStart)
    reopened.saveEdit(page: 1); try await awaitRecovery { !reopened.busy }
    let effective = try #require(reopened.snapshot)
    #expect(effective.pages[0].revision != s.pages[0].revision && effective.pages[0].ocr?.text == "raw 123")
    #expect(effective.export(translation: nil).contains("unapplied correction 456"))
    t.revision = 2; t.document = effective; t.drafts = [:]; try await store.save(t)
    try Data("tampered".utf8).write(to: source.0)
    do { _ = try await store.sourceData(t); Issue.record("mismatched source accepted") } catch {}
    #expect(try await store.read(t.id).task.document?.pages[0].text == "unapplied correction 456")
}

@MainActor @Test func interruptedOCRContinuesOnlyMissingPagesPreservingEdits() async throws {
    let root = try recoveryRoot(); defer { try? FileManager.default.removeItem(at: root) }
    let file = root.appendingPathComponent("pages.pdf"); var box = CGRect(x: 0,y: 0,width: 100,height: 100)
    let ctx = try #require(CGContext(file as CFURL,mediaBox: &box,nil))
    for _ in 0..<3 { ctx.beginPDFPage(nil); ctx.endPDFPage() }; ctx.closePDF()
    let reader = DocumentTextReader(); let loaded = try await reader.load(file)
    var p = DocumentPageRecord(id: 1,text: "corrected",state: .extracted); p.mode = .ocr; p.edited = true
    let s = DocumentSnapshot(id: UUID(),file: "pages.pdf",fingerprint: loaded.3,totalPages: 3,isPDF: true,
        range: try DocumentPageRange(first: 1,last: 3,total: 3),model: "local",direction: "en-zh",
        pages: [p,DocumentPageRecord(id: 2,state: .recognizing),DocumentPageRecord(id: 3)],mode: .ocr)
    var calls: [Int] = []
    let c = DocumentTranslationController(ocr: { n,_ in calls.append(n); var r = DocumentPageRecord(id: n,text: "OCR \(n)",state: .extracted); r.mode = .ocr; return r })
    await c.restore(snapshot: s,translation: nil,drafts: [:],assetURL: file)
    #expect(calls.isEmpty && c.snapshot?.pages[1].state == .stopped)
    c.continueExtraction(); try await awaitRecovery { !c.busy }
    #expect(calls == [2,3] && c.snapshot?.pages[0].revision == p.revision && c.snapshot?.pages[0].text == "corrected")
    #expect(c.snapshot?.segments.count == 3)
}

@MainActor @Test func finalCompletionWaitsForSuccessfulSnapshotCommit() async throws {
    let c = TextTranslationController(); var commit: CheckedContinuation<Void, Error>?
    c.checkpoint = { job in
        if job.phase == .completed { try await withCheckedThrowingContinuation { commit = $0 } }
    }
    c.start(source: "first", model: "local", direction: "en-zh") { input,_,_ in recoveredResult(input) }
    try await awaitRecovery { commit != nil }
    #expect(c.busy && c.job?.phase == .running && !c.canResume)
    commit?.resume(); try await awaitRecovery { !c.busy }
    #expect(c.job?.phase == .completed)
}

@MainActor @Test func immediatePreparationStopCanResumeWithoutLosingInputOrDocumentPlan() async throws {
    let input = String(repeating: "A full source paragraph.\n", count: 400), c = TextTranslationController()
    c.start(source: input, model: "local", direction: "en-zh") { s,_,_ in recoveredResult(s) }
    c.stop()
    #expect(c.job?.segments.isEmpty == true && c.canResume)
    c.resume { s,_,_ in recoveredResult(s) }; try await awaitRecovery { !c.busy }
    #expect(c.job?.phase == .completed && c.job?.segments.map(\.source).joined() == input)
    let plan = try LongTextSplitter.split("page-one\npage-two", byteLimit: 10)
    c.start(source: "document-source-key", model: "local", direction: "en-zh", translate: { s,_,_ in recoveredResult(s) }, preparedSegments: plan)
    c.stop(); #expect(c.job?.segments.map(\.source) == plan.map(\.source))
    var calls: [String] = []
    c.resume { s,_,_ in calls.append(s); return recoveredResult(s) }; try await awaitRecovery { !c.busy }
    #expect(calls == plan.map(\.requestText) && c.job?.phase == .completed)
}

@MainActor @Test func backgroundAtomicCheckpointLatencyMeasurement() async throws {
    let root = try recoveryRoot(); defer { try? FileManager.default.removeItem(at: root) }
    let store = TaskRecoveryStore(root: root, write: { data, url in
        #expect(!Thread.isMainThread)
        try data.write(to: url, options: .atomic)
        let handle = try FileHandle(forWritingTo: url); defer { try? handle.close() }; try handle.synchronize()
    })
    let input = String(repeating: "Checkpoint measurement with complete source and result.\n", count: 300)
    var t = try textTask(input); var measurements: [Double] = []
    // Measure a realistic bounded plan, rather than thousands of tiny test segments.
    t.text?.segments = try LongTextSplitter.split(input)
    for index in t.text!.segments.indices {
        let result = recoveredResult(t.text!.segments[index].requestText)
        t.text?.segments[index].result = result; t.text?.segments[index].state = .completed
    }
    for i in 1...30 {
        t.revision = UInt64(i); t.updated = Date(); let start = Date()
        try await store.save(t); measurements.append(Date().timeIntervalSince(start) * 1000)
    }
    #expect(try await store.read(t.id).task.text?.count(.completed) == t.text?.segments.count)
    if let output = ProcessInfo.processInfo.environment["RECOVERY_MEASURE_DIR"] {
        let sorted = measurements.sorted()
        let metrics: [String: Double] = ["samples": 30, "source_bytes": Double(input.utf8.count),
            "segments": Double(t.text!.segments.count), "median_ms": sorted[15], "p95_ms": sorted[28], "max_ms": sorted.last!]
        try JSONOutput.write(metrics, to: URL(fileURLWithPath: output).appendingPathComponent("atomic-save-latency.json"))
    }
}
