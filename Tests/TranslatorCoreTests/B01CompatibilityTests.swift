import Foundation
import Testing
@testable import TranslatorCore

private let fixtureID = UUID(uuidString: "11111111-2222-4333-8444-555555555555")!
private let digest7B = "202e0ccbae412a107d24c32bc08d7674c579c459a5942e7ed42e5fff631630d9"
private let digest18B = "f5ee2ebab8a605d7790eee41a0a34bbc98b4b12792ad7cc9f216dd4949fd7ed1"
private var b01FixtureRoot: URL {
    URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("fixtures/b01")
}

/// Generation uses the existing production encoder/store. No second Swift
/// serializer is created for the shared UI. All text below is self-authored.
@Test func b01FrozenSwiftRecipesAndSchemaOneRemainByteIdentical() async throws {
    let output = ProcessInfo.processInfo.environment["B01_GENERATE_FIXTURES"]
        .map { URL(fileURLWithPath: $0) }
    let root = output ?? b01FixtureRoot
    if output != nil {
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
    }
    var bindings: [String: String] = [:]
    for version in [1, 2] {
        for direction in ["en-zh", "zh-en"] {
            let recipe = TranslationConfiguration(model: version == 1 ? "hy-mt2:1.8b-q8" : "hy-mt2:7b-q8",
                direction: direction, digest: version == 1 ? digest18B : digest7B, profileVersion: version)
            let name = "swift-recipe-v\(version)-\(direction).json"
            let bytes = try JSONOutput.encode(recipe)
            if output != nil { try bytes.write(to: root.appendingPathComponent(name)) }
            let frozen = try Data(contentsOf: root.appendingPathComponent(name))
            #expect(frozen == bytes)
            let restored = try JSONDecoder().decode(TranslationConfiguration.self, from: frozen)
            #expect(restored.binding == recipe.binding)
            #expect(try JSONOutput.encode(restored) == frozen)
            if version == 1 { #expect(!String(decoding: frozen, as: UTF8.self).contains("contextVersion")) }
            bindings[name] = recipe.binding
        }
    }
    let source = "Repeat this sentence. Repeat this sentence.\r\n中文 / C:\\synthetic\\source.txt\nEND-B01"
    let recipe = TranslationConfiguration(model: "hy-mt2:1.8b-q8", direction: "en-zh", digest: digest18B, profileVersion: 1)
    if output != nil {
        let store = TaskRecoveryStore(root: root.appendingPathComponent("text-task"))
        var job = TextTranslationJob(id: fixtureID, source: source, model: recipe.model, direction: recipe.direction)
        job.segments = try LongTextSplitter.split(source, byteLimit: 48); job.phase = .stopped
        var result = TranslationRecord(source: job.segments[0].requestText, translation: "自编夹具已保存结果。",
            model: recipe.model, direction: recipe.direction, elapsedSeconds: 0.125, warnings: [])
        result.configurationBinding = recipe.binding; result.requestConfiguration = recipe
        job.segments[0].state = .completed; job.segments[0].result = result
        let data = Data(source.utf8)
        let asset = RecoveryAsset(originalPath: "/synthetic-mac/source.txt", filename: "source-\(TaskRecoveryStore.hash(data)).txt",
            fingerprint: TaskRecoveryStore.hash(data), byteCount: data.count)
        var task = RecoveryTask(id: fixtureID, revision: 1, name: "B01 自编兼容夹具", configuration: recipe,
            input: source, text: job, drafts: [2: "未应用的自编草稿"], asset: asset)
        task.created = Date(timeIntervalSince1970: 0); task.updated = task.created
        try await store.save(task, assetData: data)
        task.revision = 2; task.updated = Date(timeIntervalSince1970: 1)
        try await store.save(task)
        var files: [String: String] = [:]
        let enumeration = try #require(FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.isRegularFileKey]))
        for case let path as URL in enumeration {
            if (try path.resourceValues(forKeys: [.isRegularFileKey])).isRegularFile == true {
                let relative = String(path.path.dropFirst(root.path.count + 1))
                files[relative] = TaskRecoveryStore.hash(try Data(contentsOf: path))
            }
        }
        let manifest: [String: Any] = ["schema": 1, "origin": "production Swift JSONOutput / TaskRecoveryStore; self-authored fixture",
            "swift_binding_dialect": "foundation-pretty-sorted-unescaped-slashes-v1",
            "files": files, "recipe_bindings": bindings, "task_id": fixtureID.uuidString]
        try JSONSerialization.data(withJSONObject: manifest, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
            .write(to: root.appendingPathComponent("swift-manifest.json"))
    }
    let store = TaskRecoveryStore(root: root.appendingPathComponent("text-task"))
    let task = try await store.read(fixtureID).task
    #expect(task.revision == 2 && task.input == source)
    #expect(task.configuration == recipe && task.text?.count(.completed) == 1)
    #expect(task.drafts[2] == "未应用的自编草稿")
    #expect(try await store.sourceData(task).1 == Data(source.utf8))
    let previous = root.appendingPathComponent("text-task/\(fixtureID.uuidString)/previous.json")
    #expect(FileManager.default.fileExists(atPath: previous.path))
}
