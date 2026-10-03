import Foundation
import CryptoKit
import Darwin
import Testing
import ZIPFoundation
@testable import TranslatorCore

private func runtimeFixture(_ root: URL, entries: [(String, String, Entry.EntryType)]? = nil, run: String = "#!/bin/sh\nexit 0\n") throws -> (RuntimeComponent, URL) {
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    let file = root.appendingPathComponent("run"); try Data(run.utf8).write(to: file)
    let manifest = RuntimeManifest(schema: 1, id: "speech-runtime", version: "fixture", platform: "macos-arm64",
                                   files: ["run":RuntimeFile(size: UInt64(try Data(contentsOf: file).count), sha256: try RuntimePaths.digest(file), link: nil)])
    let manifestURL = root.appendingPathComponent("component.json"); try JSONEncoder().encode(manifest).write(to: manifestURL)
    let archiveURL = root.appendingPathComponent("fixture.zip")
    let archive = try Archive(url: archiveURL, accessMode: .create)
    if let entries {
        for (name, value, type) in entries {
            let data = Data(value.utf8)
            try archive.addEntry(with: name, type: type, uncompressedSize: Int64(data.count), permissions: 0o755) { offset, count in data.subdata(in: Int(offset)..<min(data.count, Int(offset) + count)) }
        }
    } else {
        for name in ["run", "component.json"] {
            let data = try Data(contentsOf: root.appendingPathComponent(name))
            try archive.addEntry(with: "payload/" + name, type: .file, uncompressedSize: Int64(data.count), permissions: 0o755) { offset, count in data.subdata(in: Int(offset)..<min(data.count, Int(offset) + count)) }
        }
    }
    let component = RuntimeComponent(id: "speech-runtime", version: "fixture", platform: "macos-arm64", archiveSHA256: try RuntimePaths.digest(archiveURL),
                                     archiveBytes: UInt64(try Data(contentsOf: archiveURL).count), installedBytes: 1_000_000,
                                     manifestSHA256: try RuntimePaths.digest(manifestURL), downloadURL: nil, allowedDownloadHosts: [], archiveRoot: "payload",
                                     executable: "run", selfcheck: nil, licenseStatus: "reviewed")
    return (component, archiveURL)
}
private func testRoot() -> URL { FileManager.default.temporaryDirectory.appendingPathComponent("runtime-test-" + UUID().uuidString) }

@MainActor @Test func runtimeAtomicInstallRepairAndFailuresKeepOldActivation() async throws {
    let root = testRoot(); defer { try? FileManager.default.removeItem(at: root) }
    let (component, archive) = try runtimeFixture(root.appendingPathComponent("fixture"))
    let environments = root.appendingPathComponent("env")
    try await RuntimeInstaller.install(component, from: archive, root: environments, busy: { false })
    let active = try Data(contentsOf: environments.appendingPathComponent("active.json"))
    let installed = try RuntimePaths.active(component, root: environments)
    try RuntimeInstaller.validate(component, at: installed)
    var wrong = component; wrong.archiveSHA256 = String(repeating: "0", count: 64)
    await #expect(throws: (any Error).self) { try await RuntimeInstaller.install(wrong, from: archive, root: environments, busy: { false }) }
    await #expect(throws: (any Error).self) { try await RuntimeInstaller.install(component, from: archive, root: environments, busy: { true }) }
    await #expect(throws: (any Error).self) { try await RuntimeInstaller.install(component, root: environments, busy: { false }) }
    #expect(try Data(contentsOf: environments.appendingPathComponent("active.json")) == active)
    try Data("corrupt".utf8).write(to: installed.appendingPathComponent("run"))
    #expect(throws: (any Error).self) { try RuntimeInstaller.validate(component, at: installed) }
    try await RuntimeInstaller.install(component, from: archive, root: environments, busy: { false })
    #expect(try RuntimePaths.active(component, root: environments) != installed)
    #expect(try Data(contentsOf: environments.appendingPathComponent("previous.json")) == active)
    #expect(FileManager.default.fileExists(atPath: installed.path))
    let repaired = try Data(contentsOf: environments.appendingPathComponent("active.json"))
    let catalog = ComponentCatalog(schema:1, components:[component])
    await #expect(throws: (any Error).self) { try await RuntimeInstaller.rollback(catalog, root: environments, busy: { false }) }
    #expect(try Data(contentsOf: environments.appendingPathComponent("active.json")) == repaired)
    try Data("#!/bin/sh\nexit 0\n".utf8).write(to: installed.appendingPathComponent("run"))
    try await RuntimeInstaller.rollback(catalog, root: environments, busy: { false })
    #expect(try Data(contentsOf: environments.appendingPathComponent("active.json")) == active)
}

@MainActor @Test func runtimeRejectsLateBusyCancellationAndConcurrentLock() async throws {
    let root = testRoot(); defer { try? FileManager.default.removeItem(at: root) }
    let (component, archive) = try runtimeFixture(root.appendingPathComponent("fixture")); let env = root.appendingPathComponent("env")
    var checks = 0
    await #expect(throws: (any Error).self) {
        try await RuntimeInstaller.install(component, from: archive, root: env, busy: { checks += 1; return checks > 1 })
    }
    #expect(!FileManager.default.fileExists(atPath: env.appendingPathComponent("active.json").path))
    let lock = open(env.appendingPathComponent(".install.lock").path, O_RDWR)
    defer { flock(lock, LOCK_UN); close(lock) }; #expect(flock(lock, LOCK_EX | LOCK_NB) == 0)
    await #expect(throws: (any Error).self) { try await RuntimeInstaller.install(component, from: archive, root: env, busy: { false }) }
    let task = Task { try Task.checkCancellation(); try await RuntimeInstaller.install(component, from: archive, root: env, busy: { false }) }
    task.cancel(); await #expect(throws: (any Error).self) { try await task.value }
    #expect(!FileManager.default.fileExists(atPath: env.appendingPathComponent("active.json").path))
}

@Test func runtimeRejectsTraversalEscapingLinksDuplicateAndUnlistedFiles() throws {
    for entries: [(String, String, Entry.EntryType)] in [
        [("payload/../outside", "bad", .file)],
        [("payload/link", "../../outside", .symlink)],
        [("payload/link", "../target", .symlink), ("payload/link/child", "bad", .file)],
        [("payload/run", "first", .file), ("payload/run", "second", .file)],
        [("payload/run", "not recorded", .file)]
    ] {
        let root = testRoot(); defer { try? FileManager.default.removeItem(at: root) }
        let (component, archive) = try runtimeFixture(root, entries: entries)
        #expect(throws: (any Error).self) { try RuntimeInstaller.unpack(archive, component: component, to: root.appendingPathComponent("out")) }
        #expect(!FileManager.default.fileExists(atPath: root.appendingPathComponent("outside").path))
    }
}

@Test func ollamaReadinessDistinguishesMissingStoppedConflictAndVersion() {
    #expect(OllamaReadiness.assess(version:nil,tagsValid:false,portAvailable:true,executablePresent:false) == .notInstalled)
    #expect(OllamaReadiness.assess(version:nil,tagsValid:false,portAvailable:true,executablePresent:true) == .notRunning)
    #expect(OllamaReadiness.assess(version:nil,tagsValid:false,portAvailable:false,executablePresent:true) == .portOccupied)
    #expect(OllamaReadiness.assess(version:"0.35.0",tagsValid:true,portAvailable:false,executablePresent:false) == .ready("0.35.0"))
    if case .incompatible = OllamaReadiness.assess(version:"0.34.0",tagsValid:true,portAvailable:false,executablePresent:true) {} else { Issue.record("Version mismatch was accepted") }
}

@MainActor @Test func runtimeSelfcheckCannotMutatePayloadAndStillActivate() async throws {
    let root = testRoot(); defer { try? FileManager.default.removeItem(at: root) }
    var (component, archive) = try runtimeFixture(root.appendingPathComponent("fixture"), run: "#!/bin/sh\ntouch unexpected-cache\nexit 0\n")
    component.selfcheck = "component.json"
    let env = root.appendingPathComponent("env")
    await #expect(throws: (any Error).self) { try await RuntimeInstaller.install(component, from: archive, root: env, busy: { false }) }
    #expect(!FileManager.default.fileExists(atPath: env.appendingPathComponent("active.json").path))
}
