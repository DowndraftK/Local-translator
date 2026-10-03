import Foundation
import Testing
@testable import TranslatorCore

private func audioFixture() throws -> URL {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent("archive-audio-" + UUID().uuidString)
    try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    try Data("archived audio".utf8).write(to: root.appendingPathComponent("audio.wav"))
    return root
}

@Test func copiedAudioResolvesLocallyWithSavedHashWithoutRewritingEvidence() throws {
    let root = try audioFixture(); defer { try? FileManager.default.removeItem(at: root) }
    let audio = root.appendingPathComponent("audio.wav")
    let evidence = root.appendingPathComponent("snapshot.json")
    let data = Data("historical path and recipe".utf8); try data.write(to: evidence)
    let selected = try RuntimePaths.archivedAudio(in: root, recordedPath: "/unavailable/old-task/audio.wav", expectedSHA256: RuntimePaths.digest(audio))
    #expect(selected == audio)
    #expect(try Data(contentsOf: evidence) == data)
}

@Test func copiedAudioRejectsChangedContent() throws {
    let root = try audioFixture(); defer { try? FileManager.default.removeItem(at: root) }
    #expect(throws: (any Error).self) {
        try RuntimePaths.archivedAudio(in: root, recordedPath: "/old-task/audio.wav", expectedSHA256: String(repeating: "0", count: 64))
    }
}

@Test func copiedAudioNeverFallsBackToAnotherTaskOrEscapingLink() throws {
    let root = try audioFixture(); let other = try audioFixture()
    defer { try? FileManager.default.removeItem(at: root); try? FileManager.default.removeItem(at: other) }
    let audio = root.appendingPathComponent("audio.wav")
    try FileManager.default.removeItem(at: audio)
    #expect(throws: (any Error).self) { try RuntimePaths.archivedAudio(in: root, recordedPath: other.appendingPathComponent("audio.wav").path, expectedSHA256: nil) }
    try FileManager.default.createSymbolicLink(at: audio, withDestinationURL: other.appendingPathComponent("audio.wav"))
    #expect(throws: (any Error).self) { try RuntimePaths.archivedAudio(in: root, recordedPath: audio.path, expectedSHA256: nil) }
}

@Test func legacyAudioWithoutFullHashRemainsReadableLocally() throws {
    let root = try audioFixture(); defer { try? FileManager.default.removeItem(at: root) }
    #expect(try RuntimePaths.archivedAudio(in: root, recordedPath: "/old-task/audio.wav", expectedSHA256: nil) == root.appendingPathComponent("audio.wav"))
}
