import Foundation
import Testing
@testable import TranslatorCore

@Test func b04SchemaTwoIsExplicitlyRejectedByOldSwiftReaderWithoutFallbackOrWrite() async throws {
    let fixtures=URL(fileURLWithPath:#filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("fixtures")
    let root=FileManager.default.temporaryDirectory.appendingPathComponent("B04-schema2-"+UUID().uuidString)
    defer { try? FileManager.default.removeItem(at:root) }
    let id=UUID(uuidString:"55555555-6666-4777-8888-999999999999")!
    let dir=root.appendingPathComponent(id.uuidString)
    try FileManager.default.createDirectory(at:dir,withIntermediateDirectories:true)
    let current=try Data(contentsOf:fixtures.appendingPathComponent("b04/current.json"))
    // A valid old previous cannot be used to downgrade an explicit schema2 head.
    let previous=try Data(contentsOf:fixtures.appendingPathComponent("b03/legacy/33333333-4444-4555-8666-777777777777/current.json"))
    try current.write(to:dir.appendingPathComponent("current.json"));try previous.write(to:dir.appendingPathComponent("previous.json"))
    let store=TaskRecoveryStore(root:root)
    do { _ = try await store.read(id); Issue.record("schema2 silently downgraded") }
    catch { #expect(error.localizedDescription.contains("版本 2")) }
    #expect(try Data(contentsOf:dir.appendingPathComponent("current.json")) == current)
    #expect(try Data(contentsOf:dir.appendingPathComponent("previous.json")) == previous)
}
