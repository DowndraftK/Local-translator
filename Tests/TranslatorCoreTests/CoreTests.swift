import AppKit
import CoreText
import Foundation
import PDFKit
import Testing
@testable import TranslatorCore
import WhisperKit

@Test func patchedTokenizerFailsLocallyForMissingAndCorruptResources() async throws {
    let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: folder) }
    for corrupt in [false, true] {
        if corrupt {
            try Data("not valid JSON".utf8).write(to: folder.appendingPathComponent("tokenizer.json"))
            try Data("not valid JSON".utf8).write(to: folder.appendingPathComponent("tokenizer_config.json"))
        }
        await #expect(throws: (any Error).self) {
            _ = try await ModelUtilities.loadTokenizer(for: .small, tokenizerFolder: folder)
        }
    }
}

@Test func officeFixturesRetainTextAndPresentationOrder() async throws {
    let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let word = try await DocumentImporter.extract(root.appendingPathComponent("fixtures/sample.docx"))
    #expect(word.blocks.contains { $0.text == "A deposit of $250 is required." })
    #expect(word.blocks.contains { $0.kind == "table-paragraph" && $0.text == "3 credits" })
    let ppt = try await DocumentImporter.extract(root.appendingPathComponent("fixtures/sample.pptx"))
    #expect(ppt.blocks.map(\.text) == ["FIRST SLIDE SENTINEL", "SECOND SLIDE SENTINEL"])
    #expect(ppt.blocks.map(\.source.page) == [1, 2])
}

@Test func onlyLiteralLoopbackEndpoints() throws {
    _ = try OllamaEngine.validateEndpoint("http://127.0.0.1:11434")
    _ = try OllamaEngine.validateEndpoint("http://[::1]:11434")
    for endpoint in ["https://example.com", "http://127.0.0.1.example.com", "http://user:pass@127.0.0.1", "http://localhost:11434", "http://127.0.0.1/path", "http://192.168.1.2:11434"] {
        #expect(throws: (any Error).self) { try OllamaEngine.validateEndpoint(endpoint) }
    }
}

@Test func streamingCompletionAndMultibyteText() throws {
    var stream = ChatAccumulator()
    try stream.consume(Data(#"{"message":{"content":"必须"},"done":false}"#.utf8))
    try stream.consume(Data(#"{"message":{"content":"提交。"},"done":false}"#.utf8))
    #expect(throws: (any Error).self) { try stream.requireComplete() }
    try stream.consume(Data(#"{"message":{"content":""},"done":true,"done_reason":"stop","eval_count":4}"#.utf8))
    try stream.requireComplete()
    #expect(stream.text == "必须提交。")
    #expect(stream.outputTokens == 4)
}

@Test func truncationAndBackendErrorsNeverBecomeSuccess() {
    for response in [
        #"{"message":{"content":"unfinished"},"done":true,"done_reason":"length"}"#,
        #"{"message":{"content":""},"done":true,"done_reason":"stop"}"#,
        #"{"error":"model missing"}"#,
        #"{"message":{"content":"translation"},"done":true}"#
    ] {
        var stream = ChatAccumulator()
        #expect(throws: (any Error).self) { try stream.consume(Data(response.utf8)) }
        #expect(!stream.finished)
    }
}

@Test func contentChecksAreHints() {
    let glossary = [GlossaryTerm(source: "credit", target: "学分")]
    #expect(ContentChecks.warnings(source: "3 credits", target: "3 学分", glossary: glossary).isEmpty)
    #expect(ContentChecks.warnings(source: "3 credits", target: "5 信用", glossary: glossary).count == 2)
}

@Test func rejectXMLExternalEntitiesAndDeepNesting() {
    #expect(throws: (any Error).self) {
        try SafeXML.parse(Data(#"<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><x>&secret;</x>"#.utf8))
    }
    #expect(throws: (any Error).self) { try SafeXML.parse(Data((String(repeating: "<x>", count: 101) + String(repeating: "</x>", count: 101)).utf8)) }
}

@Test func relationshipsCannotEscapeSlides() throws {
    #expect(try OfficeImporter.resolveRelationship("slides/slide7.xml") == "ppt/slides/slide7.xml")
    for value in ["../../etc/passwd", "https://example.com/slide.xml", "/tmp/slide.xml", "slides/../../evil.xml"] {
        #expect(throws: (any Error).self) { try OfficeImporter.resolveRelationship(value) }
    }
}

@Test func namespaceAwareXMLPreservesRelationshipID() throws {
    let root = try SafeXML.parse(Data(#"<p:sldId xmlns:p="urn:p" xmlns:r="urn:r" id="256" r:id="rId7"/>"#.utf8))
    #expect(root.name == "sldId")
    #expect(root.attributes["r:id"] == "rId7")
}

@Test func resourcesFailBeforeAnyModelLoad() throws {
    let data = Data(#"{"modelFolder":"/missing-model","tokenizerFolder":"/missing-tokenizer","files":[]}"#.utf8)
    let resources = try JSONDecoder().decode(SpeechResources.self, from: data)
    #expect(throws: (any Error).self) { try resources.validate() }
}

@Test func txtAndPDFKeepSourceLocations() async throws {
    let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: folder) }
    let text = folder.appendingPathComponent("sample.txt")
    try "The deposit is $250.\n\nIt is not refundable.".write(to: text, atomically: true, encoding: .utf8)
    let extracted = try await DocumentImporter.extract(text)
    #expect(extracted.blocks.count == 2)
    #expect(extracted.blocks[1].source.element == "paragraph:2")
    let pdfURL = folder.appendingPathComponent("sample.pdf")
    let data = NSMutableData()
    var rect = CGRect(x: 0, y: 0, width: 400, height: 400)
    let consumer = CGDataConsumer(data: data)!
    let context = CGContext(consumer: consumer, mediaBox: &rect, nil)!
    context.beginPDFPage(nil)
    let line = CTLineCreateWithAttributedString(NSAttributedString(string: "M0 SOURCE SENTINEL", attributes: [.font: NSFont.systemFont(ofSize: 16)]))
    context.textPosition = CGPoint(x: 20, y: 300); CTLineDraw(line, context)
    context.endPDFPage(); context.closePDF()
    try (data as Data).write(to: pdfURL)
    let pdf = try await DocumentImporter.extract(pdfURL, mode: "text")
    #expect(pdf.blocks.contains { $0.text.contains("M0 SOURCE SENTINEL") && $0.source.page == 1 })
}
